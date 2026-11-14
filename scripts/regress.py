#!/usr/bin/env python3
"""Regression runner: RTL vs ISS lockstep trace comparison over many configurations.

  .venv/bin/python scripts/regress.py --suite riscv-tests --bp all --stall 0,30 --seeds 2 -j 8
  .venv/bin/python scripts/regress.py --random 200 --bp GSHARE,RANDOM --stall 0,50 -j 8
  .venv/bin/python scripts/regress.py --suite riscv-tests --iss-only

For every ELF the ISS runs once (its trace is cached). Then for every predictor type
x stall percentage x seed the RTL simulator runs and its trace is compared with the
ISS one (scripts/cmp_trace.py). A run passes only if the RTL result line equals the
ISS result line, the traces match, and instret agrees. Seeds only matter with stalls
or with the RANDOM predictor, so with --stall 0 and a deterministic predictor one
seed is run.

RTL binary: build/sim/<TYPE>/Vtb_top if it exists, else build/sim/Vtb_top with
+bp=<TYPE> (docs/SPEC.md section 7); --sim overrides.

Writes build/regress/summary.md (and prints the same tables). Failing runs keep
their traces and logs under build/regress/runs/; passing runs delete them unless
--keep. Exit status 0 only if every run passed.
"""

import argparse
import concurrent.futures as cf
import glob
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(BUILD, "regress")
ISS = os.path.join(ROOT, "model", "iss.py")
GEN = os.path.join(ROOT, "scripts", "rand_prog.py")
BP_TYPES = ["STATIC_NT", "BIMODAL", "GSHARE", "TOURNAMENT", "RANDOM"]
SEEDED_BP = {"RANDOM", "STUB_RANDOM"}  # predictors whose behaviour depends on +seed

RV_CC = os.environ.get("RV_CC", "/opt/homebrew/bin/riscv64-elf-gcc")
RV_ARCH = ["-march=rv32i_zicsr_zifencei", "-mabi=ilp32"]

RESULT_RE = re.compile(r"^(PASS|FAIL \d+|TIMEOUT|TRAP|ERROR.*)$")
STATS_RE = re.compile(r"cycles=(\d+) instret=(\d+) cpi=([\d.]+) branches=(\d+) br_mispred=(\d+) "
                      r"jumps=(\d+) jump_mispred=(\d+)")

SUITES = {
    "riscv-tests": os.path.join(BUILD, "sw", "riscv-tests", "rv32ui-p-*.elf"),
    "bench": os.path.join(BUILD, "sw", "bench", "*.elf"),
    "random": os.path.join(BUILD, "sw", "random", "*.elf"),
}


def parse_output(text):
    """Last result line and stats of a simulator's stdout."""
    result, stats = None, None
    for line in text.splitlines():
        line = line.strip()
        if RESULT_RE.match(line):
            result = line
        m = STATS_RE.search(line)
        if m:
            stats = dict(zip(("cycles", "instret", "cpi", "branches", "br_mispred", "jumps",
                              "jump_mispred"), m.groups()))
    return result, stats


def build_random(seed, budget, blocks):
    """Generate and assemble one random program; return the ELF path."""
    d = os.path.join(BUILD, "sw", "random")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, f"rand_{seed}.S")
    elf = os.path.join(d, f"rand_{seed}.elf")
    stamp = f"--seed {seed} --budget {budget} --blocks {blocks}"
    if os.path.exists(elf) and os.path.exists(src):
        with open(src) as f:
            if stamp in f.readline() and os.path.getmtime(elf) >= os.path.getmtime(GEN):
                return elf
    subprocess.run([PY, GEN, "--seed", str(seed), "--budget", str(budget), "--blocks", str(blocks),
                    "-o", src], check=True)
    subprocess.run([RV_CC, *RV_ARCH, "-nostdlib", "-nostartfiles", "-static",
                    "-Wl,--no-warn-rwx-segments", "-Wl,--no-relax",
                    "-T", os.path.join(ROOT, "sw", "common", "link.ld"), src, "-o", elf], check=True)
    return elf


def run_iss(elf, keep_trace=True):
    name = os.path.splitext(os.path.basename(elf))[0]
    d = os.path.join(OUT, "iss")
    os.makedirs(d, exist_ok=True)
    trace = os.path.join(d, f"{name}.trace")
    t0 = time.time()
    p = subprocess.run([PY, ISS, elf, "--quiet", "--trace", trace], capture_output=True, text=True)
    result, stats = parse_output(p.stdout)
    if not keep_trace and os.path.exists(trace):
        os.remove(trace)
    return dict(elf=elf, name=name, trace=trace, result=result, stats=stats,
                stderr=p.stderr.strip(), secs=time.time() - t0)


def sim_binary(bp, override):
    """(binary, extra plusargs) for a predictor type."""
    if override:
        if "{bp}" in override:
            return override.replace("{bp}", bp), []
        return override, [f"+bp={bp}"]
    per_type = os.path.join(BUILD, "sim", bp, "Vtb_top")
    if os.path.exists(per_type):
        return per_type, []
    return os.path.join(BUILD, "sim", "Vtb_top"), [f"+bp={bp}"]


def run_rtl(job, iss, args):
    elf, bp, stall, seed = job
    name = iss["name"]
    tag = f"{name}.{bp}.s{stall}.r{seed}"
    d = os.path.join(OUT, "runs")
    os.makedirs(d, exist_ok=True)
    trace = os.path.join(d, f"{tag}.trace")
    log = os.path.join(d, f"{tag}.log")
    binary, extra = sim_binary(bp, args.sim)
    instret = int(iss["stats"]["instret"]) if iss["stats"] else 100000
    max_cycles = args.max_cycles or (instret * 60 + 20000)
    cmd = [binary, f"+elf={elf}", f"+trace={trace}", f"+max_cycles={max_cycles}",
           f"+stall={stall}", f"+seed={seed}"] + extra
    rec = dict(name=name, elf=elf, bp=bp, stall=stall, seed=seed, cmd=" ".join(cmd), trace=trace,
               log=log, ok=False, why="", stats=None)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        rec["why"] = f"wall-clock timeout after {args.timeout}s"
    except OSError as e:
        out = ""
        rec["why"] = f"cannot run simulator: {e}"
    rec["secs"] = time.time() - t0
    with open(log, "w") as f:
        f.write(rec["cmd"] + "\n" + out)
    if rec["why"]:
        return rec
    result, stats = parse_output(out)
    rec["result"], rec["stats"] = result, stats
    if result is None:
        rec["why"] = "no result line from the simulator"
        return rec
    if result != iss["result"]:
        rec["why"] = f"result {result} vs ISS {iss['result']}"
    if not os.path.exists(trace):
        rec["why"] = (rec["why"] + "; " if rec["why"] else "") + "no trace written"
        return rec
    msg = _compare(trace, iss["trace"], elf)
    if msg:
        rec["why"] = (rec["why"] + "; " if rec["why"] else "") + msg
    elif stats and iss["stats"] and stats["instret"] != iss["stats"]["instret"]:
        rec["why"] = (rec["why"] + "; " if rec["why"] else "") + \
            f"instret {stats['instret']} vs ISS {iss['stats']['instret']}"
    rec["ok"] = not rec["why"]
    if rec["ok"] and not args.keep:
        for p in (trace, log):
            if os.path.exists(p):
                os.remove(p)
    return rec


def _compare(dut, ref, elf):
    """First-mismatch message, or '' if the traces match. Runs cmp_trace.py in its own
    process so that comparisons run in parallel (threads would share the GIL)."""
    p = subprocess.run([PY, os.path.join(ROOT, "scripts", "cmp_trace.py"), dut, ref, "--elf", elf,
                        "--context", "8"], capture_output=True, text=True)
    if p.returncode == 0:
        return ""
    with open(dut + ".cmp", "w") as f:
        f.write(p.stdout + p.stderr)
    lines = (p.stdout + p.stderr).strip().splitlines()
    return lines[0] if lines else f"cmp_trace exit {p.returncode}"


def table(rows, header):
    w = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header))]
    line = lambda r: "| " + " | ".join(str(c).ljust(w[i]) for i, c in enumerate(r)) + " |"
    return "\n".join([line(header), "|" + "|".join("-" * (x + 2) for x in w) + "|"] + [line(r) for r in rows])


def main(argv=None):
    ap = argparse.ArgumentParser(description="RTL vs ISS regression")
    ap.add_argument("--suite", action="append", default=[], choices=sorted(SUITES),
                    help="named ELF set (repeatable)")
    ap.add_argument("--elf", action="append", default=[], help="ELF file or glob (repeatable)")
    ap.add_argument("--random", type=int, default=0, help="generate and run N random programs")
    ap.add_argument("--random-seed0", type=int, default=1, help="first random program seed")
    ap.add_argument("--random-budget", type=int, default=30000)
    ap.add_argument("--random-blocks", type=int, default=60)
    ap.add_argument("--bp", default="GSHARE", help="comma list of simulator variants (build/sim/<V>), or 'all'")
    ap.add_argument("--stall", default="0", help="comma list of stall percentages (0..100)")
    ap.add_argument("--seeds", type=int, default=1, help="simulator seeds per stalled/random config")
    ap.add_argument("--seed0", type=int, default=1)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--sim", help="simulator binary (may contain {bp}); default from build/sim")
    ap.add_argument("--max-cycles", type=int, default=0, help="default: 60 x ISS instret + 20000")
    ap.add_argument("--timeout", type=int, default=600, help="wall-clock seconds per RTL run")
    ap.add_argument("--iss-only", action="store_true", help="only run the ISS and check PASS")
    ap.add_argument("--keep", action="store_true", help="keep traces of passing runs")
    ap.add_argument("--summary", default=os.path.join(OUT, "summary.md"))
    args = ap.parse_args(argv)

    os.makedirs(OUT, exist_ok=True)
    elfs = []
    for s in args.suite:
        elfs += sorted(glob.glob(SUITES[s]))
    for e in args.elf:
        elfs += sorted(glob.glob(e)) or [e]
    t_start = time.time()
    if args.random:
        seeds = range(args.random_seed0, args.random_seed0 + args.random)
        with cf.ThreadPoolExecutor(args.jobs) as ex:
            elfs += list(ex.map(lambda s: build_random(s, args.random_budget, args.random_blocks), seeds))
    elfs = list(dict.fromkeys(os.path.abspath(e) for e in elfs))
    if not elfs:
        print("regress: no ELF selected (build them first: make sw / riscv-tests-elf)", file=sys.stderr)
        return 2

    # 1. ISS on every ELF (golden traces).
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        iss = {r["elf"]: r for r in ex.map(lambda e: run_iss(e, keep_trace=not args.iss_only), elfs)}
    iss_bad = [r for r in iss.values() if r["result"] != "PASS"]

    lines = [f"# Regression summary", "",
             f"{time.strftime('%Y-%m-%d %H:%M')} — {len(elfs)} programs"]
    if args.iss_only:
        rows = [(r["name"], r["result"], r["stats"]["instret"] if r["stats"] else "-",
                 f"{r['secs']:.1f}") for r in sorted(iss.values(), key=lambda r: r["name"])]
        lines += ["", "## ISS only", "", table(rows, ("program", "ISS result", "instret", "s")), "",
                  f"**{len(elfs) - len(iss_bad)}/{len(elfs)} PASS on the ISS**"]
        text = "\n".join(lines) + "\n"
        print(text)
        with open(args.summary, "w") as f:
            f.write(text)
        return 0 if not iss_bad else 1

    # 2. RTL runs.
    if args.bp == "all":
        # Every variant mk/sim.mk has built (real predictors and the stubs), else the SPEC list.
        built = sorted(os.path.basename(os.path.dirname(p))
                       for p in glob.glob(os.path.join(BUILD, "sim", "*", "Vtb_top")))
        bps = built or BP_TYPES
    else:
        bps = [b.strip() for b in args.bp.split(",") if b.strip()]
    stalls = [int(s) for s in args.stall.split(",")]
    jobs = []
    for e in elfs:
        if iss[e]["result"] is None:
            continue
        for bp in bps:
            for st in stalls:
                n = args.seeds if (st > 0 or bp in SEEDED_BP) else 1
                for k in range(n):
                    jobs.append((e, bp, st, args.seed0 + k))
    missing = sorted({sim_binary(bp, args.sim)[0] for bp in bps
                      if not os.path.exists(sim_binary(bp, args.sim)[0])})
    if missing:
        print("regress: simulator binary not found: " + ", ".join(missing) +
              "\n  build it first (mk/sim.mk), or pass --sim, or use --iss-only", file=sys.stderr)
        return 2
    results = []
    done = 0
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(run_rtl, j, iss[j[0]], args) for j in jobs]
        for fu in cf.as_completed(futs):
            r = fu.result()
            results.append(r)
            done += 1
            if not r["ok"]:
                print(f"FAIL {r['name']} bp={r['bp']} stall={r['stall']} seed={r['seed']}: {r['why']}",
                      file=sys.stderr)
            if done % 50 == 0:
                print(f"  {done}/{len(jobs)} runs", file=sys.stderr)

    # 3. Summary.
    groups = {}
    for r in results:
        suite = os.path.basename(os.path.dirname(r["elf"]))  # riscv-tests, bench, random, ...
        key = (suite, r["bp"], r["stall"])
        g = groups.setdefault(key, [0, 0])
        g[0] += r["ok"]
        g[1] += 1
    rows = [(s, bp, st, f"{p}/{n}", "ok" if p == n else "FAIL") for (s, bp, st), (p, n) in sorted(groups.items())]
    npass = sum(r["ok"] for r in results)
    lines += ["", f"Predictors: {', '.join(bps)}; stall %: {args.stall}; seeds per seeded config: "
              f"{args.seeds}; wall time {time.time() - t_start:.0f} s", "",
              "## Pass rate", "", table(rows, ("suite", "predictor", "stall %", "pass", "")), "",
              f"**{npass}/{len(results)} runs passed**"]
    if iss_bad:
        lines += ["", "## Programs that do not PASS on the ISS", ""]
        lines += [f"- {r['name']}: {r['result']} {r['stderr']}" for r in iss_bad]
    fails = sorted((r for r in results if not r["ok"]), key=lambda r: (r["name"], r["bp"], r["stall"], r["seed"]))
    if fails:
        lines += ["", "## Failures (first 50)", ""]
        for r in fails[:50]:
            lines += [f"- `{r['name']}` bp={r['bp']} stall={r['stall']} seed={r['seed']}: {r['why']}",
                      f"  - rerun: `{r['cmd']}`",
                      f"  - compare: `{PY} scripts/cmp_trace.py {r['trace']} {iss[r['elf']]['trace']} --elf {r['elf']}`"]
    # Performance table for the predictor study (stall 0 only: stalls distort CPI).
    perf = [r for r in results if r["ok"] and r["stall"] == 0 and r["stats"]]
    if perf:
        # Only real programs: riscv-tests and random programs are too short to say anything.
        names = sorted({r["name"] for r in perf if not r["name"].startswith(("rv32ui-", "rand_"))})
        if names:
            prow = []
            for n in names:
                for bp in bps:
                    rs = [r for r in perf if r["name"] == n and r["bp"] == bp]
                    if rs:
                        s = rs[0]["stats"]
                        br = int(s["branches"]) or 1
                        jm = int(s["jumps"]) or 1
                        prow.append((n, bp, s["cycles"], s["instret"], s["cpi"],
                                     f"{100 * int(s['br_mispred']) / br:.1f}",
                                     f"{100 * int(s['jump_mispred']) / jm:.1f}"))
            lines += ["", "## Performance at stall 0", "",
                      table(prow, ("program", "predictor", "cycles", "instret", "CPI", "br mispred %",
                                   "jump mispred %"))]
    text = "\n".join(lines) + "\n"
    print(text)
    with open(args.summary, "w") as f:
        f.write(text)
    return 0 if npass == len(results) and not iss_bad else 1


if __name__ == "__main__":
    sys.exit(main())
