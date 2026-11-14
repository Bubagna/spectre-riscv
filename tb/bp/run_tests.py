"""Build bpu with Verilator for every BP_TYPE x parameter set and run tb/bp/test_bpu.py on each.

    .venv/bin/python tb/bp/run_tests.py [--build DIR] [-j N] [--only TYPE ...] [--cycles N]
                                        [--seed N] [--params default,tiny,...]

Each configuration gets its own build directory, so builds run in parallel. Exit code 0 only if
every test of every configuration passed. The summary table is also written to
<build>/summary.txt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from bp_model import TYPES, Config  # noqa: E402

SRC_FILES = ("bp_pkg.sv", "bp_ram.sv", "bp_btb.sv", "bp_bimodal.sv", "bp_gshare.sv",
             "bp_tournament.sv", "bp_ras.sv", "bp_ind.sv", "bpu.sv")

# name: (BTB_ENTRIES, BHT_ENTRIES, GHR_BITS, RAS_DEPTH, IND_ENTRIES)
PARAM_SETS = {
    "default": (64, 512, 9, 4, 32),  # the defaults
    "tiny":    (4, 16, 4, 2, 2),     # everything aliases, the RAS overflows, 1-bit path history
    "noras":   (16, 64, 3, 0, 0),    # GHR shorter than the index, no RAS, no indirect table
    "ghr1":    (8, 8, 1, 8, 4),      # 1-bit history, RAS deeper than the BTB is wide
    "mid":     (32, 128, 7, 8, 16),  # big enough for the RAS overflow test with depth 8
}


def run_one(cfg: Config, build_root: Path, cycles: int, seed: int,
            rtl_dir: Path = ROOT / "rtl/bp") -> dict:
    # Imported here so the parent process does not need cocotb's environment.
    from cocotb_tools.runner import get_runner

    bdir = build_root / cfg.short()
    bdir.mkdir(parents=True, exist_ok=True)
    params = {"BP_TYPE": f'"{cfg.bp_type}"', "BTB_ENTRIES": cfg.btb_entries,
              "BHT_ENTRIES": cfg.bht_entries, "GHR_BITS": cfg.ghr_bits,
              "RAS_DEPTH": cfg.ras_depth, "IND_ENTRIES": cfg.ind_entries}
    runner = get_runner("verilator")
    log = bdir / "run.log"
    try:
        runner.build(sources=[rtl_dir / f for f in SRC_FILES], hdl_toplevel="bpu",
                     parameters=params, build_dir=bdir, build_args=["-Wall", "-Wno-fatal"],
                     always=True, log_file=bdir / "build.log")
        env = {"BP_CFG": json.dumps(cfg.__dict__), "BP_RANDOM_CYCLES": str(cycles),
               "BP_SEED": str(seed), "PYTHONPATH": str(HERE)}
        results = runner.test(test_module="test_bpu", hdl_toplevel="bpu", build_dir=bdir,
                              test_dir=bdir, extra_env=env, results_xml="results.xml",
                              log_file=log)
    except Exception as e:  # build error or simulator crash
        return {"cfg": cfg.short(), "tests": 0, "failed": -1, "error": str(e), "log": str(log)}
    tree = ET.parse(results)
    cases = list(tree.iter("testcase"))
    failed = [c.get("name") for c in cases if c.find("failure") is not None
              or c.find("error") is not None]
    return {"cfg": cfg.short(), "tests": len(cases), "failed": len(failed), "names": failed,
            "log": str(log)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--build", type=Path, default=ROOT / "build/bp/cocotb")
    ap.add_argument("-j", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--only", nargs="*", choices=TYPES, help="restrict to these BP_TYPEs")
    ap.add_argument("--params", default=",".join(PARAM_SETS), help="comma-separated set names")
    ap.add_argument("--cycles", type=int, default=50000, help="cycles of the random test")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    types = a.only or list(TYPES)
    cfgs = [Config(t, *PARAM_SETS[p]) for p in a.params.split(",") for t in types]
    a.build.mkdir(parents=True, exist_ok=True)
    results = []
    with ProcessPoolExecutor(max_workers=a.j) as ex:
        futs = [ex.submit(run_one, c, a.build, a.cycles, a.seed) for c in cfgs]
        for f in as_completed(futs):
            r = f.result()
            status = "PASS" if r["failed"] == 0 and r["tests"] > 0 else "FAIL"
            print(f"{status}  {r['cfg']:<40} {r['tests']:>3} tests"
                  + (f"  failed: {r.get('names') or r.get('error')}  log: {r['log']}"
                     if status == "FAIL" else ""), flush=True)
            results.append((status, r))
    results.sort(key=lambda x: x[1]["cfg"])
    lines = [f"{s}  {r['cfg']:<40} {r['tests']:>3} tests, {max(r['failed'], 0)} failed"
             for s, r in results]
    bad = sum(s == "FAIL" for s, _ in results)
    lines.append(f"{len(results) - bad}/{len(results)} configurations passed "
                 f"(random test: {a.cycles} cycles, seed {a.seed})")
    (a.build / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
