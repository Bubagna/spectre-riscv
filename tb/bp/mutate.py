"""Mutation check of the bpu testbench: inject one known bug at a time into a copy of rtl/bp/ and
make sure tb/bp/test_bpu.py fails on it. A testbench that passes on buggy RTL proves nothing.

    .venv/bin/python tb/bp/mutate.py [--build DIR] [-j N] [--cycles N]

Exit code 0 only if every mutant is caught (killed) and the unmodified RTL still passes.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from bp_model import Config  # noqa: E402
from run_tests import PARAM_SETS, run_one  # noqa: E402

# (name, file, original text, mutated text, configurations to run as (BP_TYPE, param set))
MUTANTS = [
    ("ras: repair does not restore the TOS entry", "bp_ras.sv",
     "if (wa_en) stack_q[wa_idx] <= restore_top_i;", "",
     [("GSHARE", "default")]),
    ("ras: repaired return pops one entry too few", "bp_ras.sv",
     "ptr_d = restore_ptr_i - PW'(1);", "ptr_d = restore_ptr_i;",
     [("BIMODAL", "default")]),
    ("ras: call pushes pc instead of pc+4", "bpu.sv",
     ".fire_push_addr_i    (pc_plus4[31:2])", ".fire_push_addr_i    (pred_pc_i[31:2])",
     [("BIMODAL", "default")]),
    ("ras: flush also replays the stale update kind", "bpu.sv",
     ".restore_push_i      (!flush_i && upd_kind_i == CF_CALL)",
     ".restore_push_i      (upd_kind_i == CF_CALL)",
     [("GSHARE", "default"), ("BIMODAL", "tiny")]),
    ("ghr: repair forgets the real outcome", "bpu.sv",
     "ghr_d = (upd_kind_i == CF_BRANCH) ? GW'({upd_ghr, upd_taken_i}) : upd_ghr;",
     "ghr_d = upd_ghr;",
     [("GSHARE", "default")]),
    ("ghr: repair shifts for every kind, not only branches", "bpu.sv",
     "ghr_d = (upd_kind_i == CF_BRANCH) ? GW'({upd_ghr, upd_taken_i}) : upd_ghr;",
     "ghr_d = GW'({upd_ghr, upd_taken_i});",
     [("TOURNAMENT", "default")]),
    ("ghr: fire wins over a flush in the same cycle", "bpu.sv",
     "      if (flush_i) begin\n        ghr_d = flush_ghr;",
     "      if (flush_i && !fire) begin\n        ghr_d = flush_ghr;",
     [("GSHARE", "default")]),
    ("gshare: training uses the current GHR, not the checkpoint", "bp_gshare.sv",
     "assign upd_idx  = upd_pc_i[2 +: IW]  ^ IW'(upd_ghr_i);",
     "assign upd_idx  = upd_pc_i[2 +: IW]  ^ IW'(pred_ghr_i);",
     [("GSHARE", "default")]),
    ("counter: increments wrap instead of saturating", "bp_pkg.sv",
     "if (up) return (ctr == 2'd3) ? 2'd3 : ctr + 2'd1;", "if (up) return ctr + 2'd1;",
     [("BIMODAL", "default")]),
    ("tournament: chooser trained even when both agree", "bp_tournament.sv",
     "assign cho_we = upd_en_i && (bim_upd != gsh_upd);", "assign cho_we = upd_en_i;",
     [("TOURNAMENT", "default")]),
    ("btb: not-taken branches are allocated", "bp_btb.sv",
     "        we = upd_taken_i;", "        we = 1'b1;",
     [("BIMODAL", "default")]),
    ("btb: kind 0 update does not invalidate", "bp_btb.sv",
     "        we   = 1'b1;\n        wr_e = '0;", "        we   = 1'b0;\n        wr_e = '0;",
     [("RANDOM", "default")]),
    ("btb: tag compare ignores the lowest tag bit", "bp_btb.sv",
     "(pred_e.tag == pred_tag)", "(pred_e.tag[TW-1:1] == pred_tag[TW-1:1])",
     [("STATIC_NT", "default")]),
    ("bpu: prediction not gated by pred_valid_i", "bpu.sv",
     "assign pred_taken_o  = pred_valid_i && taken;", "assign pred_taken_o  = taken;",
     [("BIMODAL", "default")]),
    ("random: LFSR advances every cycle, not per fire", "bpu.sv",
     "else if (fire) lfsr_q <=", "else           lfsr_q <=",
     [("RANDOM", "default")]),
    ("ind: path history never updated", "bp_ind.sv",
     "else if (upd_en_i) path_q <=", "else if (1'b0)     path_q <=",
     [("GSHARE", "default")]),
    ("ind: training index ignores the path history", "bp_ind.sv",
     "assign upd_idx  = upd_pc_i[2 +: IW] ^ path_q;", "assign upd_idx  = upd_pc_i[2 +: IW];",
     [("BIMODAL", "default")]),
    ("btb: poly bit not sticky (cleared when the target repeats)", "bp_btb.sv",
     "(upd_e.poly || upd_e.target != upd_target_i[31:2])", "(upd_e.target != upd_target_i[31:2])",
     [("STATIC_NT", "default")]),
    ("bpu: indirect table used for monomorphic jumps too", "bpu.sv",
     "if (IND_ON && btb_poly && ind_hit &&", "if (IND_ON && ind_hit &&",
     [("TOURNAMENT", "default")]),
]


def make_mutant(i: int, file: str, old: str, new: str, root: Path) -> Path:
    d = root / f"m{i:02d}" / "rtl"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(ROOT / "rtl/bp", d)
    p = d / file
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"mutant {i}: pattern not found exactly once in {file}: {old!r}")
    p.write_text(text.replace(old, new))
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", type=Path, default=ROOT / "build/bp/mutants")
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--cycles", type=int, default=20000)
    a = ap.parse_args()

    jobs = []
    for i, (name, file, old, new, cfgs) in enumerate(MUTANTS):
        d = make_mutant(i, file, old, new, a.build)
        for t, ps in cfgs:
            jobs.append((i, name, Config(t, *PARAM_SETS[ps]), d))
    with ProcessPoolExecutor(max_workers=a.j) as ex:
        futs = [(i, name, ex.submit(run_one, cfg, a.build / f"m{i:02d}" / "sim", a.cycles, 1, d))
                for i, name, cfg, d in jobs]
        killed: dict[int, list[str]] = {}
        for i, name, f in futs:
            r = f.result()
            if r["failed"] != 0:
                killed.setdefault(i, []).extend(r.get("names") or [r.get("error", "?")])
            else:
                killed.setdefault(i, [])
    ok = True
    for i, (name, *_rest) in enumerate(MUTANTS):
        tests = sorted(set(killed[i]))
        status = "killed" if tests else "SURVIVED"
        ok &= bool(tests)
        print(f"{status:<9} m{i:02d} {name}" + (f"  <- {', '.join(tests)}" if tests else ""))
    n_killed = sum(bool(killed[i]) for i in range(len(MUTANTS)))
    print(f"{n_killed}/{len(MUTANTS)} mutants killed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
