#!/usr/bin/env python3
"""Run ELF programs on the core's simulator variants and check them (owner: core).

  .venv/bin/python rtl/sim/tests/run_tests.py --sim-dir build/sim --variants "STUB_RANDOM GSHARE" \
      [--stalls 0,30,60] [--seeds 1,2] [--no-iss] [--jobs 8] <elf> ...

For every ELF x variant x stall x seed it runs build/sim/<variant>/Vtb_top and checks:
  - the result line: PASS, or TRAP for programs whose name starts with "trap_";
  - the commit trace against the ISS (model/iss.py + scripts/cmp_trace.py), when they exist.
Seeds only change something with stalls or with a RANDOM predictor, so stall 0 runs once per
variant unless the variant is RANDOM.

Prints one line per failure and a summary table; with --cpi, also the CPI and mispredictions
of every program (stall 0, first seed). Exit 0 only if everything passed.
"""

import argparse
import concurrent.futures as cf
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PY = os.path.join(ROOT, ".venv", "bin", "python")
ISS = os.path.join(ROOT, "model", "iss.py")
CMP = os.path.join(ROOT, "scripts", "cmp_trace.py")
STATS_RE = re.compile(r"^cycles=(\d+) instret=(\d+) cpi=([\d.]+) branches=(\d+) br_mispred=(\d+) "
                      r"jumps=(\d+) jump_mispred=(\d+)")


def name_of(elf):
    return os.path.splitext(os.path.basename(elf))[0]


def expected(elf):
    return "TRAP" if name_of(elf).startswith("trap_") else "PASS"


def parse_output(out):
    """Return (result line, stats dict or None)."""
    lines = [l for l in out.splitlines() if l.strip()]
    stats, result = None, "NO-RESULT"
    for i, l in enumerate(lines):
        m = STATS_RE.match(l)
        if m:
            stats = dict(zip(("cycles", "instret", "cpi", "branches", "br_mispred", "jumps",
                              "jump_mispred"), (float(x) for x in m.groups())))
            if i > 0:
                result = lines[i - 1]
    return result, stats


def run_iss(elf, outdir):
    trace = os.path.join(outdir, name_of(elf) + ".iss.trace")
    p = subprocess.run([PY, ISS, elf, "--trace", trace, "--quiet"], capture_output=True,
                       text=True, timeout=600)
    return trace, parse_output(p.stdout)[0]


def run_one(sim, elf, stall, seed, outdir, iss_trace):
    tag = f"{name_of(elf)}.{os.path.basename(os.path.dirname(sim))}.st{stall}.s{seed}"
    trace = os.path.join(outdir, tag + ".trace")
    cmd = [sim, f"+elf={elf}", f"+trace={trace}", f"+stall={stall}", f"+seed={seed}", "+quiet"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return tag, False, "host timeout", None
    result, stats = parse_output(p.stdout)
    errs = []
    if result != expected(elf):
        errs.append(f"result {result!r}, expected {expected(elf)}")
        tail = [l for l in out.splitlines() if "rror" in l][:3]
        errs.extend(tail)
    if iss_trace:
        c = subprocess.run([PY, CMP, trace, iss_trace], capture_output=True, text=True)
        if c.returncode != 0:
            errs.append("trace differs from ISS: " + " | ".join(c.stdout.strip().splitlines()[:6]))
    if not errs:
        os.remove(trace)  # keep only the traces of failing runs
    return tag, not errs, "; ".join(errs), stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("elfs", nargs="+")
    ap.add_argument("--sim-dir", default=os.path.join(ROOT, "build", "sim"))
    ap.add_argument("--variants", default="STUB_STATIC_NT STUB_RANDOM")
    ap.add_argument("--stalls", default="0,30,60")
    ap.add_argument("--seeds", default="1,2")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "core-tests", "runs"))
    ap.add_argument("--no-iss", action="store_true")
    ap.add_argument("--cpi", action="store_true")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    variants = a.variants.split()
    stalls = [int(s) for s in a.stalls.split(",")]
    seeds = [int(s) for s in a.seeds.split(",")]
    use_iss = not a.no_iss and os.path.exists(ISS) and os.path.exists(CMP)

    iss = {}
    if use_iss:
        with cf.ThreadPoolExecutor(a.jobs) as ex:
            for elf, (trace, res) in zip(a.elfs, ex.map(lambda e: run_iss(e, a.out), a.elfs)):
                if res != expected(elf):
                    print(f"WARNING ISS gives {res!r} on {name_of(elf)}, expected {expected(elf)}")
                iss[elf] = trace

    jobs = []
    for v in variants:
        sim = os.path.join(a.sim_dir, v, "Vtb_top")
        if not os.path.exists(sim):
            print(f"missing simulator {sim}")
            return 2
        for st in stalls:
            for sd in (seeds if (st > 0 or "RANDOM" in v) else seeds[:1]):
                for elf in a.elfs:
                    jobs.append((v, st, sd, elf, sim))

    results = {}
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(run_one, sim, elf, st, sd, a.out, iss.get(elf)): (v, st, sd, elf)
                for v, st, sd, elf, sim in jobs}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()

    fails = [(k, r) for k, r in results.items() if not r[1]]
    for k, r in sorted(fails):
        print(f"FAIL {r[0]}: {r[2]}")

    print(f"\n{len(a.elfs)} programs, ISS comparison {'on' if use_iss else 'off'}")
    print(f"{'variant':<16}" + "".join(f"{'stall ' + str(s):>12}" for s in stalls))
    for v in variants:
        row = f"{v:<16}"
        for st in stalls:
            rs = [r for (vv, ss, _, _), r in results.items() if vv == v and ss == st]
            row += f"{sum(r[1] for r in rs):>7}/{len(rs):<4}"
        print(row)

    if a.cpi:
        print(f"\n{'program':<24}{'variant':<16}{'instret':>9}{'cycles':>9}{'cpi':>7}"
              f"{'br':>7}{'br_mis':>7}{'jmp':>6}{'j_mis':>6}")
        for v in variants:
            for elf in a.elfs:
                r = results.get((v, stalls[0], seeds[0], elf))
                if r and r[3]:
                    s = r[3]
                    print(f"{name_of(elf):<24}{v:<16}{int(s['instret']):>9}{int(s['cycles']):>9}"
                          f"{s['cpi']:>7.3f}{int(s['branches']):>7}{int(s['br_mispred']):>7}"
                          f"{int(s['jumps']):>6}{int(s['jump_mispred']):>6}")

    print(f"\n{len(results) - len(fails)}/{len(results)} runs passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
