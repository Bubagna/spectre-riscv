#!/usr/bin/env python3
"""Compare two commit traces (docs/SPEC.md section 6), normally RTL vs ISS.

  .venv/bin/python scripts/cmp_trace.py <dut.trace> <ref.trace> [--elf prog.elf] [--context 6]

Rules:
  - lines are compared field by field: pc, insn, rd + value, store, load, trap;
  - on lines with the " csr" suffix (counter reads) the rd value is ignored, the
    register number is not;
  - store data is compared only on the byte lanes enabled by the mask (the bytes
    outside the mask are don't-care in RVFI); --strict compares the whole word.

Prints "MATCH <n> instructions" and exits 0, or prints the first mismatch with the
preceding lines for context (disassembled if --elf is given) and exits 1.
Exit 2 on a malformed trace or a usage error.
"""

import argparse
import collections
import os
import re
import subprocess
import sys

LINE_RE = re.compile(
    r"^(?P<pc>[0-9a-f]{8}) (?P<insn>[0-9a-f]{8})"
    r"(?: x(?P<rd>\d\d) (?P<wd>[0-9a-f]{8}))?"
    r"(?: st (?P<sa>[0-9a-f]{8}) (?P<sm>[0-9a-f]) (?P<sd>[0-9a-f]{8}))?"
    r"(?: ld (?P<la>[0-9a-f]{8}))?"
    r"(?P<csr> csr)?"
    r"(?P<trap> trap)?$")


class TraceError(Exception):
    pass


def parse(line, where):
    m = LINE_RE.match(line)
    if not m:
        raise TraceError(f"{where}: malformed trace line: {line!r}")
    return m.groupdict()


def lane_mask(m):
    v = 0
    for i in range(4):
        if m >> i & 1:
            v |= 0xFF << (8 * i)
    return v


def diff(a, b, strict):
    """Return a list of human-readable differences between two parsed lines."""
    out = []
    for key, name in (("pc", "pc"), ("insn", "instruction"), ("trap", "trap flag"),
                      ("csr", "csr suffix"), ("rd", "destination register")):
        if a[key] != b[key]:
            out.append(f"{name}: {a[key] or '-'} vs {b[key] or '-'}")
    if a["rd"] == b["rd"] and a["wd"] != b["wd"] and not (a["csr"] and b["csr"]):
        out.append(f"x{a['rd']} value: {a['wd']} vs {b['wd']}")
    if (a["sa"] is None) != (b["sa"] is None):
        out.append(f"store: {'yes' if a['sa'] else 'no'} vs {'yes' if b['sa'] else 'no'}")
    elif a["sa"] is not None:
        if a["sa"] != b["sa"]:
            out.append(f"store address: {a['sa']} vs {b['sa']}")
        if a["sm"] != b["sm"]:
            out.append(f"store mask: {a['sm']} vs {b['sm']}")
        else:
            lm = 0xFFFFFFFF if strict else lane_mask(int(a["sm"], 16))
            if int(a["sd"], 16) & lm != int(b["sd"], 16) & lm:
                out.append(f"store data (mask {a['sm']}): {a['sd']} vs {b['sd']}")
    if a["la"] != b["la"]:
        out.append(f"load address: {a['la'] or '-'} vs {b['la'] or '-'}")
    return out


def disassembly(elf):
    """pc -> 'mnemonic operands' from objdump, or {} if it cannot be run."""
    objdump = os.environ.get("RV_OBJDUMP", "/opt/homebrew/bin/riscv64-elf-objdump")
    try:
        text = subprocess.run([objdump, "-d", elf], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    dis = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-f]+):\s+[0-9a-f]{8}\s+(.*)$", line)
        if m:
            dis[int(m.group(1), 16)] = m.group(2).split("#")[0].strip().replace("\t", " ")
    return dis


def annotate(line, dis):
    if not dis or not line:
        return line
    try:
        pc = int(line[:8], 16)
    except ValueError:
        return line
    d = dis.get(pc)
    return f"{line:<52}  # {d}" if d else line


def compare(dut_path, ref_path, context=6, elf=None, strict=False, out=sys.stdout,
            names=("DUT", "REF")):
    hist = collections.deque(maxlen=context)
    n = 0
    with open(dut_path) as fa, open(ref_path) as fb:
        while True:
            la = fa.readline()
            lb = fb.readline()
            if not la and not lb:
                print(f"MATCH {n} instructions", file=out)
                return 0
            la = la.rstrip("\n")
            lb = lb.rstrip("\n")
            problems = []
            if la == lb:
                pass  # fast path: almost every line is identical, parse only the others
            elif not la or not lb:
                short = names[0] if not la else names[1]
                problems = [f"{short} trace ends here"]
            else:
                pa = parse(la, f"{dut_path}:{n + 1}")
                pb = parse(lb, f"{ref_path}:{n + 1}")
                problems = diff(pa, pb, strict)
            if problems:
                dis = disassembly(elf) if elf else {}
                print(f"MISMATCH at instruction {n + 1} (line {n + 1}): " + "; ".join(problems),
                      file=out)
                if hist:
                    print(f"  last {len(hist)} matching lines:", file=out)
                    for h in hist:
                        print(f"    {annotate(h, dis)}", file=out)
                w = max(len(x) for x in names)
                print(f"  {names[0]:<{w}}: {annotate(la, dis) if la else '<end of trace>'}", file=out)
                print(f"  {names[1]:<{w}}: {annotate(lb, dis) if lb else '<end of trace>'}", file=out)
                # One more line of each side helps to see where the paths diverge.
                na, nb = fa.readline().rstrip("\n"), fb.readline().rstrip("\n")
                if na or nb:
                    print(f"  next {names[0]:<{w}}: {annotate(na, dis) or '<end>'}", file=out)
                    print(f"  next {names[1]:<{w}}: {annotate(nb, dis) or '<end>'}", file=out)
                return 1
            hist.append(lb)
            n += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("dut", help="trace under test (RTL)")
    ap.add_argument("ref", help="reference trace (ISS)")
    ap.add_argument("--elf", help="program, to disassemble the context lines")
    ap.add_argument("--context", type=int, default=6, help="matching lines shown before a mismatch")
    ap.add_argument("--strict", action="store_true", help="compare store data outside the mask too")
    args = ap.parse_args(argv)
    try:
        return compare(args.dut, args.ref, args.context, args.elf, args.strict,
                       names=("RTL", "ISS"))
    except (OSError, TraceError) as exc:
        print(f"cmp_trace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
