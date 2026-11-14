#!/usr/bin/env python3
"""Instruction-set simulator for spectre-riscv: the golden reference model.

Implements exactly what docs/SPEC.md asks of the core:
  - RV32I (37 instructions), FENCE as a NOP, FENCE.I (a NOP here: stores
    invalidate the decode cache immediately, which is what a flush gives the RTL);
  - Zicntr read-only counters (cycle/time/instret and the high halves) and
    mhartid; every write attempt or any other CSR is an illegal instruction;
  - ECALL, EBREAK, illegal instructions, misaligned loads/stores and misaligned
    taken jump/branch targets trap: the instruction retires with the trap flag
    and the run ends with TRAP;
  - memory map of section 2 (1 MiB RAM at 0x8000_0000, TOHOST/PUTCHAR/HALT MMIO);
  - commit trace in the format of section 6, final result and stats lines of
    section 7 (cycles = instret here, no mispredictions).

Usage:
  .venv/bin/python model/iss.py <elf> [--trace FILE] [--btrace FILE] [--max-instr N]

Exit status: 0 PASS, 1 FAIL, 2 TRAP, 3 TIMEOUT, 4 simulator error (bad access, bad ELF).
"""

import argparse
import struct
import sys

M32 = 0xFFFFFFFF
RAM_BASE = 0x80000000
RAM_SIZE = 1 << 20
RESET_PC = 0x80000000

MMIO_TOHOST = 0x10000000
MMIO_PUTCHAR = 0x10000004
MMIO_HALT = 0x10000008
MMIO_BASE = 0x10000000
MMIO_END = 0x1000000C  # exclusive

# Control-flow kinds, same encoding as upd_kind_i of the predictor (SPEC section 5).
K_NONE, K_BRANCH, K_JAL, K_JALR, K_CALL, K_RET = range(6)

# Counter CSRs whose value differs between RTL and ISS: trace lines get " csr".
CSR_CYCLE, CSR_TIME, CSR_INSTRET = 0xC00, 0xC01, 0xC02
CSR_CYCLEH, CSR_TIMEH, CSR_INSTRETH = 0xC80, 0xC81, 0xC82
CSR_MHARTID = 0xF14
COUNTER_CSRS = (CSR_CYCLE, CSR_TIME, CSR_INSTRET, CSR_CYCLEH, CSR_TIMEH, CSR_INSTRETH)

LINK_REGS = (1, 5)


class Trap(Exception):
    """Architectural trap: the instruction retires with rvfi_trap = 1, then the run stops."""


class Halt(Exception):
    """Store to TOHOST or HALT: the store retires, then the run stops."""

    def __init__(self, result, code):
        super().__init__(result)
        self.result = result  # "PASS" or "FAIL"
        self.code = code


class SimError(Exception):
    """Not an architectural event: access outside RAM/MMIO, bad ELF. The simulator gives up."""


def sext(v, bits):
    sign = 1 << (bits - 1)
    return (v & (sign - 1)) - (v & sign)


def imm_i(insn):
    return sext(insn >> 20, 12)


def imm_s(insn):
    return sext(((insn >> 25) << 5) | ((insn >> 7) & 0x1F), 12)


def imm_b(insn):
    v = (((insn >> 31) & 1) << 12) | (((insn >> 7) & 1) << 11) \
        | (((insn >> 25) & 0x3F) << 5) | (((insn >> 8) & 0xF) << 1)
    return sext(v, 13)


def imm_u(insn):
    return insn & 0xFFFFF000


def imm_j(insn):
    v = (((insn >> 31) & 1) << 20) | (((insn >> 12) & 0xFF) << 12) \
        | (((insn >> 20) & 1) << 11) | (((insn >> 21) & 0x3FF) << 1)
    return sext(v, 21)


def jump_kind(rd, rs1, is_jalr):
    """Classify a jump as the predictor does (SPEC section 5)."""
    if rd in LINK_REGS:
        return K_CALL
    if is_jalr and rd == 0 and rs1 in LINK_REGS:
        return K_RET
    return K_JALR if is_jalr else K_JAL


class Result:
    def __init__(self, status, code=0, reason=""):
        self.status = status  # PASS, FAIL, TRAP, TIMEOUT, ERROR
        self.code = code
        self.reason = reason

    def line(self):
        if self.status == "FAIL":
            return f"FAIL {self.code}"
        if self.status == "ERROR":
            return f"ERROR {self.reason}"
        return self.status

    def exit_status(self):
        return {"PASS": 0, "FAIL": 1, "TRAP": 2, "TIMEOUT": 3}.get(self.status, 4)


class ISS:
    def __init__(self, out=None):
        self.x = [0] * 32
        self.pc = RESET_PC
        self.ram = bytearray(RAM_SIZE)
        self.instret = 0
        self.branches = 0
        self.jumps = 0
        self.out = out if out is not None else sys.stdout  # PUTCHAR destination
        self.last_char = "\n"
        # Decode cache: pc -> (fn, insn, rd_traced, kind, is_counter_csr, static_target).
        # A store to RAM drops the entry of the word it writes (self-modifying code).
        self.cache = {}
        self._mem = None    # memory access of the current instruction, for the trace
        self._taken = [0]   # outcome of the last conditional branch
        self.trap_reason = ""

    # ------------------------------------------------------------------ memory
    def load_elf(self, path):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"\x7fELF":
            raise SimError(f"{path}: not an ELF file")
        if data[4] != 1 or data[5] != 1:
            raise SimError(f"{path}: not a 32-bit little-endian ELF")
        (e_type, e_machine, _ver, e_entry, e_phoff, _shoff, _flags, _ehsize,
         e_phentsize, e_phnum) = struct.unpack_from("<HHIIIIIHHH", data, 16)
        if e_machine != 243:
            raise SimError(f"{path}: not a RISC-V ELF (e_machine={e_machine})")
        for i in range(e_phnum):
            p_type, p_offset, _vaddr, p_paddr, p_filesz, p_memsz, _fl, _al = \
                struct.unpack_from("<IIIIIIII", data, e_phoff + i * e_phentsize)
            if p_type != 1 or p_memsz == 0:  # PT_LOAD only
                continue
            off = p_paddr - RAM_BASE
            if off < 0 or off + p_memsz > RAM_SIZE:
                raise SimError(f"{path}: segment at {p_paddr:#x} (+{p_memsz:#x}) is outside RAM")
            self.ram[off:off + p_filesz] = data[p_offset:p_offset + p_filesz]
            self.ram[off + p_filesz:off + p_memsz] = bytes(p_memsz - p_filesz)
        self.entry = e_entry
        self.cache.clear()

    def load_bytes(self, addr, blob):
        off = addr - RAM_BASE
        if off < 0 or off + len(blob) > RAM_SIZE:
            raise SimError(f"load_bytes outside RAM at {addr:#x}")
        self.ram[off:off + len(blob)] = blob
        self.cache.clear()

    def load_words(self, addr, words):
        self.load_bytes(addr, b"".join(struct.pack("<I", w & M32) for w in words))

    def read_word(self, addr):
        off = addr - RAM_BASE
        return int.from_bytes(self.ram[off:off + 4], "little")

    def _load(self, addr, size):
        """Raw little-endian load of an aligned access; MMIO reads 0."""
        self._mem = ("ld", addr & ~3)
        off = addr - RAM_BASE
        if 0 <= off <= RAM_SIZE - size:
            return int.from_bytes(self.ram[off:off + size], "little")
        if MMIO_BASE <= addr < MMIO_END:
            return 0
        raise SimError(f"load from unmapped address {addr:#010x}")

    def _store(self, addr, size, val):
        sh = (addr & 3) * 8
        mask = ((1 << size) - 1) << (addr & 3)
        val &= (1 << (8 * size)) - 1
        self._mem = ("st", addr & ~3, mask, (val << sh) & M32)
        off = addr - RAM_BASE
        if 0 <= off <= RAM_SIZE - size:
            self.ram[off:off + size] = val.to_bytes(size, "little")
            self.cache.pop(addr & ~3, None)
            return
        if MMIO_BASE <= addr < MMIO_END:
            reg = addr & ~3
            if reg == MMIO_PUTCHAR:
                c = chr(val & 0xFF)
                self.out.write(c)
                self.last_char = c
            elif reg == MMIO_TOHOST:
                if val == 1:
                    raise Halt("PASS", 0)
                raise Halt("FAIL", val >> 1)
            elif reg == MMIO_HALT:
                if val == 0:
                    raise Halt("PASS", 0)
                raise Halt("FAIL", val)
            return
        raise SimError(f"store to unmapped address {addr:#010x}")

    # ------------------------------------------------------------------ decode
    def _decode_at(self, pc):
        off = pc - RAM_BASE
        if off < 0 or off > RAM_SIZE - 4:
            raise SimError(f"fetch from unmapped address {pc:#010x}")
        insn = int.from_bytes(self.ram[off:off + 4], "little")
        entry = self.decode(pc, insn)
        self.cache[pc] = entry
        return entry

    def _trap(self, reason):
        def fn(pc):
            self.trap_reason = reason
            raise Trap(reason)
        return fn

    def decode(self, pc, insn):
        """Return (fn, insn, rd_traced, kind, is_counter_csr, static_target).

        fn(pc) executes the instruction and returns the next pc. rd_traced is the
        destination register printed in the trace (0 if none or x0)."""
        x = self.x
        op = insn & 0x7F
        rd = (insn >> 7) & 0x1F
        f3 = (insn >> 12) & 7
        rs1 = (insn >> 15) & 0x1F
        rs2 = (insn >> 20) & 0x1F
        f7 = insn >> 25
        illegal = (self._trap("illegal instruction"), insn, 0, K_NONE, False, None)

        if op == 0x13:  # OP-IMM
            imm = imm_i(insn)
            shamt = rs2
            if f3 == 1 and f7 != 0:
                return illegal
            if f3 == 5 and f7 not in (0, 0x20):
                return illegal
            if rd == 0:  # HINT space: legal, no effect
                return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)
            immu = imm & M32
            if f3 == 0:
                def fn(pc):
                    x[rd] = (x[rs1] + imm) & M32
                    return pc + 4
            elif f3 == 2:
                simm = imm
                def fn(pc):
                    a = x[rs1]
                    x[rd] = 1 if (a - ((a & 0x80000000) << 1)) < simm else 0
                    return pc + 4
            elif f3 == 3:
                def fn(pc):
                    x[rd] = 1 if x[rs1] < immu else 0
                    return pc + 4
            elif f3 == 4:
                def fn(pc):
                    x[rd] = x[rs1] ^ immu
                    return pc + 4
            elif f3 == 6:
                def fn(pc):
                    x[rd] = x[rs1] | immu
                    return pc + 4
            elif f3 == 7:
                def fn(pc):
                    x[rd] = x[rs1] & immu
                    return pc + 4
            elif f3 == 1:
                def fn(pc):
                    x[rd] = (x[rs1] << shamt) & M32
                    return pc + 4
            elif f7 == 0:  # SRLI
                def fn(pc):
                    x[rd] = x[rs1] >> shamt
                    return pc + 4
            else:  # SRAI
                def fn(pc):
                    a = x[rs1]
                    x[rd] = ((a - ((a & 0x80000000) << 1)) >> shamt) & M32
                    return pc + 4
            return (fn, insn, rd, K_NONE, False, None)

        if op == 0x33:  # OP
            if f7 == 0x20:
                if f3 not in (0, 5):
                    return illegal
            elif f7 != 0:
                return illegal
            if rd == 0:
                return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)
            key = (f3, f7)
            if key == (0, 0):
                def fn(pc):
                    x[rd] = (x[rs1] + x[rs2]) & M32
                    return pc + 4
            elif key == (0, 0x20):
                def fn(pc):
                    x[rd] = (x[rs1] - x[rs2]) & M32
                    return pc + 4
            elif f3 == 1:
                def fn(pc):
                    x[rd] = (x[rs1] << (x[rs2] & 31)) & M32
                    return pc + 4
            elif f3 == 2:
                def fn(pc):
                    x[rd] = 1 if (x[rs1] ^ 0x80000000) < (x[rs2] ^ 0x80000000) else 0
                    return pc + 4
            elif f3 == 3:
                def fn(pc):
                    x[rd] = 1 if x[rs1] < x[rs2] else 0
                    return pc + 4
            elif f3 == 4:
                def fn(pc):
                    x[rd] = x[rs1] ^ x[rs2]
                    return pc + 4
            elif key == (5, 0):
                def fn(pc):
                    x[rd] = x[rs1] >> (x[rs2] & 31)
                    return pc + 4
            elif key == (5, 0x20):
                def fn(pc):
                    a = x[rs1]
                    x[rd] = ((a - ((a & 0x80000000) << 1)) >> (x[rs2] & 31)) & M32
                    return pc + 4
            elif f3 == 6:
                def fn(pc):
                    x[rd] = x[rs1] | x[rs2]
                    return pc + 4
            else:
                def fn(pc):
                    x[rd] = x[rs1] & x[rs2]
                    return pc + 4
            return (fn, insn, rd, K_NONE, False, None)

        if op == 0x37:  # LUI
            if rd == 0:
                return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)
            v = imm_u(insn)
            def fn(pc):
                x[rd] = v
                return pc + 4
            return (fn, insn, rd, K_NONE, False, None)

        if op == 0x17:  # AUIPC: the value is known at decode, the pc is fixed
            if rd == 0:
                return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)
            v = (pc + imm_u(insn)) & M32
            def fn(pc):
                x[rd] = v
                return pc + 4
            return (fn, insn, rd, K_NONE, False, None)

        if op == 0x6F:  # JAL
            tgt = (pc + imm_j(insn)) & M32
            kind = jump_kind(rd, None, False)
            if tgt & 3:
                return (self._trap("misaligned jump target"), insn, 0, kind, False, tgt)
            link = (pc + 4) & M32
            if rd == 0:
                def fn(pc):
                    return tgt
            else:
                def fn(pc):
                    x[rd] = link
                    return tgt
            return (fn, insn, rd, kind, False, tgt)

        if op == 0x67:  # JALR
            if f3 != 0:
                return illegal
            imm = imm_i(insn)
            kind = jump_kind(rd, rs1, True)
            link = (pc + 4) & M32
            def fn(pc):
                t = (x[rs1] + imm) & 0xFFFFFFFE
                if t & 2:
                    self.trap_reason = "misaligned jump target"
                    raise Trap(self.trap_reason)
                if rd:
                    x[rd] = link
                return t
            return (fn, insn, rd, kind, False, None)

        if op == 0x63:  # BRANCH
            if f3 in (2, 3):
                return illegal
            tgt = (pc + imm_b(insn)) & M32
            nxt = (pc + 4) & M32
            taken = self._taken
            if f3 == 0:
                cond = lambda: x[rs1] == x[rs2]
            elif f3 == 1:
                cond = lambda: x[rs1] != x[rs2]
            elif f3 == 4:
                cond = lambda: (x[rs1] ^ 0x80000000) < (x[rs2] ^ 0x80000000)
            elif f3 == 5:
                cond = lambda: (x[rs1] ^ 0x80000000) >= (x[rs2] ^ 0x80000000)
            elif f3 == 6:
                cond = lambda: x[rs1] < x[rs2]
            else:
                cond = lambda: x[rs1] >= x[rs2]
            if tgt & 3:
                def fn(pc):
                    if cond():
                        taken[0] = 1
                        self.trap_reason = "misaligned branch target"
                        raise Trap(self.trap_reason)
                    taken[0] = 0
                    return nxt
            else:
                def fn(pc):
                    if cond():
                        taken[0] = 1
                        return tgt
                    taken[0] = 0
                    return nxt
            return (fn, insn, 0, K_BRANCH, False, tgt)

        if op == 0x03:  # LOAD
            if f3 not in (0, 1, 2, 4, 5):
                return illegal
            imm = imm_i(insn)
            size = (1, 2, 4, None, 1, 2)[f3]
            signed = f3 in (0, 1)
            load = self._load
            amask = size - 1
            sbit = 1 << (8 * size - 1)
            def fn(pc):
                a = (x[rs1] + imm) & M32
                if a & amask:
                    self.trap_reason = "misaligned load"
                    raise Trap(self.trap_reason)
                v = load(a, size)
                if signed and v & sbit:
                    v = (v - (sbit << 1)) & M32
                if rd:
                    x[rd] = v
                return pc + 4
            return (fn, insn, rd, K_NONE, False, None)

        if op == 0x23:  # STORE
            if f3 > 2:
                return illegal
            imm = imm_s(insn)
            size = 1 << f3
            amask = size - 1
            store = self._store
            def fn(pc):
                a = (x[rs1] + imm) & M32
                if a & amask:
                    self.trap_reason = "misaligned store"
                    raise Trap(self.trap_reason)
                store(a, size, x[rs2])
                return pc + 4
            return (fn, insn, 0, K_NONE, False, None)

        if op == 0x0F:  # MISC-MEM: FENCE and FENCE.I
            if f3 not in (0, 1):
                return illegal
            return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)

        if op == 0x73:  # SYSTEM
            if f3 == 0:
                if insn == 0x00000073:
                    return (self._trap("ecall"), insn, 0, K_NONE, False, None)
                if insn == 0x00100073:
                    return (self._trap("ebreak"), insn, 0, K_NONE, False, None)
                return illegal
            if f3 == 4:
                return illegal
            csr = insn >> 20
            writes = f3 in (1, 5) or rs1 != 0
            if writes or (csr not in COUNTER_CSRS and csr != CSR_MHARTID):
                return illegal
            if csr == CSR_MHARTID:
                if rd == 0:
                    return (lambda pc: pc + 4, insn, 0, K_NONE, False, None)
                def fn(pc):
                    x[rd] = 0
                    return pc + 4
                return (fn, insn, rd, K_NONE, False, None)
            high = csr >= 0xC80
            def fn(pc):
                # The ISS has one cycle per instruction: cycle = time = instret,
                # counted before this instruction retires.
                v = self.instret
                if rd:
                    x[rd] = (v >> 32) & M32 if high else v & M32
                return pc + 4
            return (fn, insn, rd, K_NONE, True, None)

        return illegal

    # ------------------------------------------------------------------ run
    def run(self, max_instr=20_000_000, trace=None, btrace=None):
        """Run until TOHOST/HALT, a trap, a simulator error or max_instr retired.

        trace / btrace: writable text files (or None) for the commit trace and the
        branch trace. Returns a Result."""
        x = self.x
        cache = self.cache
        get = cache.get
        decode_at = self._decode_at
        taken = self._taken
        pc = self.pc
        tbuf = [] if trace is not None else None
        bbuf = [] if btrace is not None else None
        result = None
        e = None
        try:
            while True:
                if self.instret >= max_instr:
                    result = Result("TIMEOUT")
                    break
                e = get(pc)
                if e is None:
                    e = decode_at(pc)
                fn, insn, rd, kind, csr, tgt = e
                self._mem = None
                npc = fn(pc)
                self.instret += 1
                if kind:
                    if kind == K_BRANCH:
                        self.branches += 1
                    else:
                        self.jumps += 1
                    if bbuf is not None:
                        if kind == K_BRANCH:
                            bbuf.append(f"{pc:08x} {kind} {taken[0]} {tgt:08x}")
                        else:
                            bbuf.append(f"{pc:08x} {kind} 1 {npc:08x}")
                if tbuf is not None:
                    tbuf.append(self._trace_line(pc, insn, rd, csr))
                    if len(tbuf) >= 4096:
                        trace.write("\n".join(tbuf) + "\n")
                        tbuf.clear()
                if bbuf is not None and len(bbuf) >= 4096:
                    btrace.write("\n".join(bbuf) + "\n")
                    bbuf.clear()
                pc = npc
        except Halt as h:
            self.instret += 1
            if tbuf is not None:
                tbuf.append(self._trace_line(pc, e[1], e[2], e[4]))
            result = Result(h.result, h.code)
            pc = (pc + 4) & M32
        except Trap as t:
            self.instret += 1
            if tbuf is not None:
                tbuf.append(f"{pc:08x} {e[1]:08x} trap")
            result = Result("TRAP", reason=f"{t} at pc {pc:08x} insn {e[1]:08x}")
        except SimError as s:
            result = Result("ERROR", reason=str(s).replace(" ", "_"))
            self.error = str(s)
        if tbuf:
            trace.write("\n".join(tbuf) + "\n")
        if bbuf:
            btrace.write("\n".join(bbuf) + "\n")
        self.pc = pc
        return result

    def _trace_line(self, pc, insn, rd, csr):
        s = f"{pc:08x} {insn:08x}"
        if rd:
            s += f" x{rd:02d} {self.x[rd]:08x}"
        m = self._mem
        if m is not None:
            if m[0] == "st":
                s += f" st {m[1]:08x} {m[2]:x} {m[3]:08x}"
            else:
                s += f" ld {m[1]:08x}"
        if csr:
            s += " csr"
        return s

    def stats_line(self):
        n = self.instret
        return (f"cycles={n} instret={n} cpi={1.0 if n else 0.0:.3f} branches={self.branches} "
                f"br_mispred=0 jumps={self.jumps} jump_mispred=0")


def main(argv=None):
    ap = argparse.ArgumentParser(description="spectre-riscv RV32I golden model")
    ap.add_argument("elf")
    ap.add_argument("--trace", help="write the commit trace (SPEC section 6) to this file")
    ap.add_argument("--btrace", help="write the branch trace (docs/VERIFICATION.md) to this file")
    ap.add_argument("--max-instr", type=int, default=20_000_000)
    ap.add_argument("--quiet", action="store_true", help="do not echo PUTCHAR output")
    args = ap.parse_args(argv)

    class _Null:
        def write(self, s):
            pass

    iss = ISS(out=_Null() if args.quiet else sys.stdout)
    try:
        iss.load_elf(args.elf)
    except (OSError, SimError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        print("ERROR bad_elf")
        return 4
    if getattr(iss, "entry", RESET_PC) != RESET_PC:
        print(f"warning: ELF entry {iss.entry:#x} != reset pc {RESET_PC:#x}; starting at reset pc",
              file=sys.stderr)
    tf = open(args.trace, "w") if args.trace else None
    bf = open(args.btrace, "w") if args.btrace else None
    try:
        res = iss.run(args.max_instr, tf, bf)
    finally:
        if tf:
            tf.close()
        if bf:
            bf.close()
    if iss.last_char != "\n" and not args.quiet:
        sys.stdout.write("\n")
    if res.status in ("TRAP", "ERROR"):
        print(f"iss: {res.reason}", file=sys.stderr)
    print(res.line())
    print(iss.stats_line())
    sys.stdout.flush()
    return res.exit_status()


if __name__ == "__main__":
    sys.exit(main())
