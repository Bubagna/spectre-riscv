#!/usr/bin/env python3
"""Extract branch traces from the ISS for trace-driven predictor studies.

  .venv/bin/python scripts/branch_trace.py build/sw/bench/*.elf [-o build/btrace]

One file <out>/<program>.btrace per ELF, one line per retired control-flow
instruction, in program order (format in docs/VERIFICATION.md, "Branch traces"):

  <pc 8 hex> <kind 1 dec> <taken 0/1> <target 8 hex>

kind uses the upd_kind_i encoding of docs/SPEC.md section 5 (1 branch, 2 jal,
3 jalr, 4 call, 5 ret). For conditional branches target is the branch target
pc+imm whether or not the branch was taken; jumps are always taken and target is
where they went. A program that does not PASS on the ISS is reported and its
trace is still written up to the point where it stopped.
"""

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
ISS = os.path.join(ROOT, "model", "iss.py")


def one(elf, out, max_instr):
    name = os.path.splitext(os.path.basename(elf))[0]
    path = os.path.join(out, f"{name}.btrace")
    p = subprocess.run([PY if os.path.exists(PY) else sys.executable, ISS, elf, "--quiet",
                        "--btrace", path, "--max-instr", str(max_instr)],
                       capture_output=True, text=True)
    lines = p.stdout.strip().splitlines()
    n = sum(1 for _ in open(path)) if os.path.exists(path) else 0
    return name, path, lines[0] if lines else "?", n


def main(argv=None):
    ap = argparse.ArgumentParser(description="ISS branch-trace extraction")
    ap.add_argument("elfs", nargs="+")
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "build", "btrace"))
    ap.add_argument("--max-instr", type=int, default=20_000_000)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    bad = 0
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for name, path, res, n in ex.map(lambda e: one(e, args.out, args.max_instr), args.elfs):
            print(f"{name:<24} {res:<8} {n:>9} control-flow records  {path}")
            bad += res != "PASS"
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
