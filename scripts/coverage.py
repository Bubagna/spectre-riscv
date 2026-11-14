#!/usr/bin/env python3
"""Functional coverage of a set of programs, measured on ISS commit traces.

  .venv/bin/python scripts/coverage.py build/sw/random/*.elf [--gaps] [-j 8]

It answers "what did these programs actually exercise?" with bins that matter for
an in-order pipeline with a predictor: every instruction, branch type x outcome,
load/store width x byte offset, read-after-write distance 1..3 by producer and
consumer kind (forwarding paths, load-use), x0 written then read, store then load
of the same word, call depth beyond the RAS, self-modifying code, counter reads.
It is architectural coverage only: it says nothing about stalls or predictor state,
which the RTL harness would have to measure.
"""

import argparse
import collections
import concurrent.futures as cf
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model"))
import iss as I  # noqa: E402

R = {(0, 0): "add", (0, 0x20): "sub", (1, 0): "sll", (2, 0): "slt", (3, 0): "sltu", (4, 0): "xor",
     (5, 0): "srl", (5, 0x20): "sra", (6, 0): "or", (7, 0): "and"}
IMM = {0: "addi", 2: "slti", 3: "sltiu", 4: "xori", 6: "ori", 7: "andi", 1: "slli"}
BR = {0: "beq", 1: "bne", 4: "blt", 5: "bge", 6: "bltu", 7: "bgeu"}
LD = {0: "lb", 1: "lh", 2: "lw", 4: "lbu", 5: "lhu"}
ST = {0: "sb", 1: "sh", 2: "sw"}
CSR = {1: "csrrw", 2: "csrrs", 3: "csrrc", 5: "csrrwi", 6: "csrrsi", 7: "csrrci"}

ALL_MNEMONICS = (list(R.values()) + list(IMM.values()) + ["srli", "srai", "lui", "auipc", "jal", "jalr"]
                 + list(BR.values()) + list(LD.values()) + list(ST.values())
                 + ["fence", "fence.i", "csrrs", "csrrc", "csrrsi", "csrrci"])


def decode(w):
    """(mnemonic, class, rd, rs1, rs2) with rd/rs = None when not used."""
    op, rd, f3, rs1, rs2, f7 = w & 0x7F, (w >> 7) & 31, (w >> 12) & 7, (w >> 15) & 31, (w >> 20) & 31, w >> 25
    if op == 0x33:
        return R.get((f3, f7), "?"), "alu", rd, rs1, rs2
    if op == 0x13:
        m = IMM.get(f3) if f3 != 5 else ("srai" if f7 == 0x20 else "srli")
        return m, "alu", rd, rs1, None
    if op == 0x37:
        return "lui", "alu", rd, None, None
    if op == 0x17:
        return "auipc", "alu", rd, None, None
    if op == 0x6F:
        return "jal", "jal", rd, None, None
    if op == 0x67:
        return "jalr", "jalr", rd, rs1, None
    if op == 0x63:
        return BR.get(f3, "?"), "branch", None, rs1, rs2
    if op == 0x03:
        return LD.get(f3, "?"), "load", rd, rs1, None
    if op == 0x23:
        return ST.get(f3, "?"), "store", None, rs1, rs2
    if op == 0x0F:
        return ("fence.i" if f3 == 1 else "fence"), "fence", None, None, None
    if op == 0x73:
        return CSR.get(f3, "system"), "csr", rd, (rs1 if f3 < 4 else None), None
    return "?", "?", None, None, None


def measure(elf, max_instr):
    sim = I.ISS(out=io.StringIO())
    sim.load_elf(elf)
    tr = io.StringIO()
    res = sim.run(max_instr, tr)
    c = collections.Counter()
    hist = collections.deque(maxlen=3)   # (rd, class) of the last 3 retired instructions
    stores = collections.deque(maxlen=3)  # word addresses of recent stores
    stored_words = set()
    depth = maxdepth = 0
    lines = tr.getvalue().splitlines()
    for i, line in enumerate(lines):
        f = line.split()
        pc, w = int(f[0], 16), int(f[1], 16)
        m, cls, rd, rs1, rs2 = decode(w)
        c[f"insn {m}"] += 1
        if "trap" in f:
            c["trap"] += 1
            continue
        if pc & ~3 in stored_words:
            c["exec of a stored word (self-modifying code)"] += 1
        # read-after-write distance by producer -> consumer kind
        srcs = [(r, role) for r, role in ((rs1, "rs1"), (rs2, "rs2")) if r]
        for dist, (prd, pcls) in enumerate(reversed(hist), 1):
            for r, role in srcs:
                if prd == r:
                    kind = cls
                    if cls in ("load", "store") and role == "rs1":
                        kind = "address"
                    elif cls == "store":
                        kind = "store data"
                    c[f"raw d{dist} {pcls}->{kind}"] += 1
        if rd == 0 and cls in ("alu", "load") and w != 0x13:
            c["x0 written (not a nop)"] += 1
        if 0 in (rs1, rs2) and hist and hist[-1][0] == 0:
            c["x0 read right after an x0 write"] += 1
        if cls == "branch":
            taken = int(lines[i + 1][:8], 16) != pc + 4 if i + 1 < len(lines) else False
            c[f"branch {m} {'taken' if taken else 'not taken'}"] += 1
        if cls == "load":
            addr = int(f[f.index("ld") + 1], 16)  # word address: the byte offset is not traced
            c[f"load {m}"] += 1
            if addr in stores:
                c["load of a word stored in the last 3 instructions"] += 1
        if cls == "store":
            k = f.index("st")
            addr, mask = int(f[k + 1], 16), int(f[k + 2], 16)
            c[f"store {m} mask {mask:x}"] += 1
            stores.append(addr)
            stored_words.add(addr)
        if cls == "csr" and line.endswith("csr"):
            c["counter read"] += 1
        if cls in ("jal", "jalr"):
            kind = I.jump_kind(rd, rs1, cls == "jalr")
            if kind == I.K_CALL:
                depth += 1
                maxdepth = max(maxdepth, depth)
                if depth > 4:
                    c["call deeper than a 4-entry RAS"] += 1
            elif kind == I.K_RET:
                depth = max(0, depth - 1)
            c[f"jump {('jal', 'jalr', 'call', 'ret')[kind - 2]}"] += 1
        writes = cls in ("alu", "load", "jal", "jalr", "csr") and w != 0x13  # nop is not an x0 write
        hist.append((rd if writes else None, cls))
    c["max call depth"] = maxdepth
    return os.path.basename(elf), res.line(), c


def main(argv=None):
    ap = argparse.ArgumentParser(description="ISS-based functional coverage")
    ap.add_argument("elfs", nargs="+")
    ap.add_argument("--gaps", action="store_true", help="list the expected bins never hit")
    ap.add_argument("--max-instr", type=int, default=5_000_000)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args(argv)
    total = collections.Counter()
    maxdepth = 0
    with cf.ProcessPoolExecutor(args.jobs) as ex:
        for name, res, c in ex.map(measure, args.elfs, [args.max_instr] * len(args.elfs)):
            if res != "PASS":
                print(f"warning: {name} ended with {res}", file=sys.stderr)
            maxdepth = max(maxdepth, c.pop("max call depth"))
            total.update(c)
    print(f"# Coverage over {len(args.elfs)} programs, max call depth {maxdepth}\n")
    for k in sorted(total):
        print(f"{total[k]:>10}  {k}")
    expected = [f"insn {m}" for m in ALL_MNEMONICS]
    expected += [f"branch {b} {t}" for b in BR.values() for t in ("taken", "not taken")]
    expected += [f"store {s} mask {m:x}" for s, ms in (("sb", (1, 2, 4, 8)), ("sh", (3, 0xc)), ("sw", (0xf,)))
                 for m in ms]
    for d in (1, 2, 3):
        for p in ("alu", "load"):
            for k in ("alu", "branch", "address", "store data", "jalr"):
                expected.append(f"raw d{d} {p}->{k}")
    expected += ["x0 read right after an x0 write", "load of a word stored in the last 3 instructions",
                 "call deeper than a 4-entry RAS", "exec of a stored word (self-modifying code)",
                 "counter read", "jump jal", "jump jalr", "jump call", "jump ret"]
    gaps = [e for e in expected if not total.get(e)]
    print(f"\n{len(expected) - len(gaps)}/{len(expected)} expected bins hit")
    if args.gaps or gaps:
        for g in gaps:
            print(f"  not hit: {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
