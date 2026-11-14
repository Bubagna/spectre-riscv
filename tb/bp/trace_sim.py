"""Trace-driven accuracy study: replay branch traces through the golden model (bp_model.py).

    .venv/bin/python tb/bp/trace_sim.py [--types T ...] [--btb 64 --bht 512 --ghr 9 --ras 4 --ind 32]
                                        [--lag 0,2] [--markdown FILE] [-j N] trace ...

Trace format (docs/BRANCH_PREDICTOR.md): one line per control-flow instruction,
"<pc hex> <kind> <taken> <target hex>", kind as on upd_kind_i (1 branch, 2 jal, 3 jalr,
4 call, 5 ret). Lines starting with '#' are comments.

Because the model is bit-exact with the RTL, these are the numbers the hardware predictor gives
for the same sequence of predictions and updates. What the trace cannot reproduce is the
pipeline timing, so it is replayed in two ways:

  lag 0  every instruction is predicted, then resolved before the next one is predicted
         (ideal: tables always up to date, speculative state never needs repair);
  lag N  up to N younger control-flow instructions are predicted before the oldest one
         resolves, as in a pipeline that resolves in EX. Training arrives late, and a
         misprediction squashes and re-predicts the younger ones after the repair (the
         squashed predictions are the correct-path ones, the trace has no wrong path).

The core's own statistics line (branches=... br_mispred=...) gives the real-pipeline number.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bp_model import (  # noqa: E402
    CF_BRANCH, CF_CALL, CF_JAL, CF_JALR, CF_RET, TYPES, BranchPredictorModel, Config, Update,
    flop_bits,
)

M32 = 0xFFFF_FFFF
GROUPS = {"branch": (CF_BRANCH,), "jump": (CF_JAL, CF_JALR, CF_CALL), "ret": (CF_RET,)}


def load_trace(path: Path) -> list[tuple[int, int, int, int]]:
    recs = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = line.split()
        if len(f) != 4:
            raise ValueError(f"{path}:{n}: expected 'pc kind taken target', got {line!r}")
        pc, kind, taken, target = int(f[0], 16), int(f[1], 16), int(f[2], 16), int(f[3], 16)
        if kind not in (1, 2, 3, 4, 5) or taken not in (0, 1):
            raise ValueError(f"{path}:{n}: bad kind/taken in {line!r}")
        recs.append((pc, kind, taken, target))
    return recs


def replay(cfg: Config, recs, lag: int) -> dict[str, list[int]]:
    """Returns {group: [count, mispredicted]} plus 'all'."""
    m = BranchPredictorModel(cfg)
    stats = {g: [0, 0] for g in (*GROUPS, "all")}
    group_of = {k: g for g, ks in GROUPS.items() for k in ks}
    q: deque = deque()
    i_fetch = 0
    n = len(recs)
    done = 0
    while done < n:
        while len(q) <= lag and i_fetch < n:
            p = m.tick(1, recs[i_fetch][0], 1)
            q.append((i_fetch, p))
            i_fetch += 1
        idx, p = q.popleft()
        pc, kind, taken, target = recs[idx]
        nxt = target if taken else (pc + 4) & M32
        mis = int(p.target != nxt)
        m.tick(upd=Update(pc, kind, taken, target, mis, p.meta))
        g = group_of[kind]
        stats[g][0] += 1
        stats[g][1] += mis
        stats["all"][0] += 1
        stats["all"][1] += mis
        if mis:
            q.clear()
            i_fetch = idx + 1
        done = idx + 1
    return stats


def _job(args):
    cfg, path, lag = args
    return cfg.bp_type, path, lag, replay(cfg, load_trace(Path(path)), lag)


SWEEP = [  # (label, BTB_ENTRIES, BHT_ENTRIES, GHR_BITS, RAS_DEPTH[, IND_ENTRIES])
    ("BTB 16", 16, 512, 9, 4), ("BTB 64 (default)", 64, 512, 9, 4), ("BTB 256", 256, 512, 9, 4),
    ("BHT 128, GHR 7", 64, 128, 7, 4), ("BHT 2048, GHR 11", 64, 2048, 11, 4),
    ("GHR 4", 64, 512, 4, 4), ("RAS 0", 64, 512, 9, 0), ("RAS 8", 64, 512, 9, 8),
    ("IND 0", 64, 512, 9, 4, 0),
]


def sweep(traces: list[str], j: int) -> list[str]:
    types = ["BIMODAL", "GSHARE", "TOURNAMENT"]
    jobs = [(Config(t, *c[1:]), p, 0) for c in SWEEP for t in types for p in traces]
    with ProcessPoolExecutor(max_workers=j) as ex:
        res = list(ex.map(_job, jobs))
    tot: dict[tuple[str, str], list[int]] = {}
    for (cfg, _, _), (_, _, _, st) in zip(jobs, res):
        key = (cfg.short(), cfg.bp_type)
        c = tot.setdefault(key, [0, 0])
        c[0] += st["all"][0]
        c[1] += st["all"][1]
    out = ["", "### table size sweep: all control flow, all traces, lag 0 (%)", "",
           "| configuration | flop bits (TOURNAMENT) | " + " | ".join(types) + " |",
           "|---|---:|" + "---:|" * len(types)]
    for label, *c in SWEEP:
        cfgs = [Config(t, *c) for t in types]
        row = [pct(tot[(x.short(), x.bp_type)]) for x in cfgs]
        out.append(f"| {label} | {flop_bits(cfgs[-1])['total']} | " + " | ".join(row) + " |")
    return out


def pct(c: list[int]) -> str:
    return "-" if c[0] == 0 else f"{100.0 * (c[0] - c[1]) / c[0]:.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("traces", nargs="+", type=Path)
    ap.add_argument("--types", nargs="*", default=list(TYPES), choices=TYPES)
    ap.add_argument("--btb", type=int, default=64)
    ap.add_argument("--bht", type=int, default=512)
    ap.add_argument("--ghr", type=int, default=9)
    ap.add_argument("--ras", type=int, default=4)
    ap.add_argument("--ind", type=int, default=Config().ind_entries)
    ap.add_argument("--lag", default="0,2", help="comma-separated in-flight depths")
    ap.add_argument("--markdown", type=Path, help="also write the tables as Markdown here")
    ap.add_argument("-j", type=int, default=8)
    ap.add_argument("--sweep", action="store_true",
                    help="also sweep table sizes (lag 0, all control flow, all traces)")
    a = ap.parse_args()

    lags = [int(x) for x in a.lag.split(",")]
    cfgs = {t: Config(t, a.btb, a.bht, a.ghr, a.ras, a.ind) for t in a.types}
    traces = [str(p) for p in a.traces]
    jobs = [(cfgs[t], p, lag) for t in a.types for p in traces for lag in lags]
    with ProcessPoolExecutor(max_workers=a.j) as ex:
        res = {(t, p, lag): s for t, p, lag, s in ex.map(_job, jobs)}

    out: list[str] = []
    c0 = next(iter(cfgs.values()))
    out.append(f"Configuration: BTB_ENTRIES={c0.btb_entries} BHT_ENTRIES={c0.bht_entries} "
               f"GHR_BITS={c0.ghr_bits} RAS_DEPTH={c0.ras_depth} IND_ENTRIES={c0.ind_entries}")
    for lag in lags:
        for title, group in (("conditional branch direction+target accuracy", "branch"),
                             ("all control flow (branches, jumps, returns) accuracy", "all")):
            out.append("")
            out.append(f"### {title} (%), lag {lag}")
            out.append("")
            out.append("| trace | " + " | ".join(a.types) + " |")
            out.append("|---|" + "---:|" * len(a.types))
            tot = {t: [0, 0] for t in a.types}
            for p in traces:
                row = []
                for t in a.types:
                    c = res[(t, p, lag)][group]
                    tot[t][0] += c[0]
                    tot[t][1] += c[1]
                    row.append(pct(c))
                out.append(f"| {Path(p).stem} | " + " | ".join(row) + " |")
            out.append("| **all traces** | " + " | ".join(f"**{pct(tot[t])}**" for t in a.types)
                       + " |")
        out.append("")
        out.append(f"### returns and jumps (%), lag {lag}")
        out.append("")
        out.append("| trace | " + " | ".join(f"{t} ret" for t in a.types) + f" | jumps ({a.types[0]}) |")
        out.append("|---|" + "---:|" * (len(a.types) + 1))
        for p in traces:
            if res[(a.types[0], p, lag)]["ret"][0] == 0 and res[(a.types[0], p, lag)]["jump"][0] == 0:
                continue
            row = [pct(res[(t, p, lag)]["ret"]) for t in a.types]
            row.append(pct(res[(a.types[0], p, lag)]["jump"]))
            out.append(f"| {Path(p).stem} | " + " | ".join(row) + " |")

    out.append("")
    out.append("### storage (flip-flop bits)")
    out.append("")
    out.append("| type | BTB | counters | GHR | RAS | IND | other | total | meta bits carried |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t, c in cfgs.items():
        b = flop_bits(c)
        out.append(f"| {t} | {b['btb']} | {b.get('counters', 0)} | {b.get('ghr', 0)} | "
                   f"{b.get('ras', 0)} | {b.get('ind', 0)} | {b.get('lfsr', 0)} | {b['total']} | "
                   f"{b['meta_carried']} |")

    if a.sweep:
        out.extend(sweep(traces, a.j))

    text = "\n".join(out) + "\n"
    print(text)
    if a.markdown:
        a.markdown.parent.mkdir(parents=True, exist_ok=True)
        a.markdown.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
