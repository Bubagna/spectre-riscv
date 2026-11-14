"""Unit tests for the golden model (model/iss.py).

Run: .venv/bin/python -m pytest model/test_iss.py -q

The instruction semantics are checked against a separate, deliberately naive
reference written here with Python signed integers, so a bug has to be made
twice in two different styles to slip through. The riscv-tests ELFs are run
too when they have been built (make riscv-tests-elf).
"""

import glob
import io
import os
import random
import shutil
import struct
import subprocess

import pytest

import iss as I

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = I.RAM_BASE
TOHOST = 0x10000000

# --------------------------------------------------------------------------- encoders


def r_type(f7, rs2, rs1, f3, rd, op=0x33):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def i_type(imm, rs1, f3, rd, op=0x13):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def s_type(imm, rs2, rs1, f3, op=0x23):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 0x1F) << 7) | op


def b_type(imm, rs2, rs1, f3, op=0x63):
    imm &= 0x1FFF
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) \
        | (f3 << 12) | (((imm >> 1) & 0xF) << 8) | (((imm >> 11) & 1) << 7) | op


def u_type(imm, rd, op):
    return (imm & 0xFFFFF000) | (rd << 7) | op


def j_type(imm, rd, op=0x6F):
    imm &= 0x1FFFFF
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) | (((imm >> 11) & 1) << 20) \
        | (((imm >> 12) & 0xFF) << 12) | (rd << 7) | op


def addi(rd, rs1, imm):
    return i_type(imm, rs1, 0, rd)


def lui(rd, imm):
    return u_type(imm, rd, 0x37)


def li32(rd, v):
    """lui + addi for any 32-bit constant."""
    v &= 0xFFFFFFFF
    lo = v & 0xFFF
    if lo & 0x800:
        lo -= 0x1000
    hi = (v - lo) & 0xFFFFFFFF
    return [lui(rd, hi), addi(rd, rd, lo)]


def sw(rs2, rs1, imm):
    return s_type(imm, rs2, rs1, 2)


PASS_SEQ = li32(31, TOHOST) + [addi(30, 0, 1), sw(30, 31, 0)]


def csr(f3, rd, rs1, addr):
    return (addr << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | 0x73


ECALL = 0x00000073
EBREAK = 0x00100073

# --------------------------------------------------------------------------- helpers


def run_words(words, regs=None, max_instr=10_000, data=None):
    out = io.StringIO()
    sim = I.ISS(out=out)
    sim.load_words(BASE, words)
    if data:
        for addr, blob in data.items():
            sim.load_bytes(addr, blob)
    if regs:
        for r, v in regs.items():
            sim.x[r] = v & 0xFFFFFFFF
    tr = io.StringIO()
    bt = io.StringIO()
    res = sim.run(max_instr, tr, bt)
    return sim, res, tr.getvalue().splitlines(), bt.getvalue().splitlines(), out.getvalue()


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


EDGE = [0, 1, 2, 3, 31, 32, 0x7FF, 0x800, 0xFFF, 0x7FFFFFFF, 0x80000000, 0x80000001,
        0xFFFFFFFF, 0xFFFFFFFE, 0x12345678, 0xDEADBEEF, 0x0000FFFF, 0xFFFF0000]

# Naive reference semantics.
REF_R = {
    "add": (0, 0, lambda a, b: a + b),
    "sub": (0, 0x20, lambda a, b: a - b),
    "sll": (1, 0, lambda a, b: a << (b & 31)),
    "slt": (2, 0, lambda a, b: int(s32(a) < s32(b))),
    "sltu": (3, 0, lambda a, b: int((a & 0xFFFFFFFF) < (b & 0xFFFFFFFF))),
    "xor": (4, 0, lambda a, b: a ^ b),
    "srl": (5, 0, lambda a, b: (a & 0xFFFFFFFF) >> (b & 31)),
    "sra": (5, 0x20, lambda a, b: s32(a) >> (b & 31)),
    "or": (6, 0, lambda a, b: a | b),
    "and": (7, 0, lambda a, b: a & b),
}

REF_I = {
    "addi": (0, lambda a, i: a + i),
    "slti": (2, lambda a, i: int(s32(a) < i)),
    "sltiu": (3, lambda a, i: int((a & 0xFFFFFFFF) < (i & 0xFFFFFFFF))),
    "xori": (4, lambda a, i: a ^ (i & 0xFFFFFFFF)),
    "ori": (6, lambda a, i: a | (i & 0xFFFFFFFF)),
    "andi": (7, lambda a, i: a & (i & 0xFFFFFFFF)),
}


# --------------------------------------------------------------------------- ALU


@pytest.mark.parametrize("name", sorted(REF_R))
def test_r_type_against_reference(name):
    f3, f7, ref = REF_R[name]
    rng = random.Random(name)
    vals = EDGE + [rng.getrandbits(32) for _ in range(40)]
    for a in vals:
        for b in rng.sample(vals, 12):
            sim, res, tr, _, _ = run_words([r_type(f7, 2, 1, f3, 3)] + PASS_SEQ, {1: a, 2: b})
            assert res.status == "PASS"
            assert sim.x[3] == ref(a, b) & 0xFFFFFFFF, (name, hex(a), hex(b))


@pytest.mark.parametrize("name", sorted(REF_I))
def test_i_type_against_reference(name):
    f3, ref = REF_I[name]
    rng = random.Random(name)
    imms = [0, 1, -1, 0x7FF, -0x800, 5, -5, 0x555, -0x2AB]
    for a in EDGE + [rng.getrandbits(32) for _ in range(20)]:
        for imm in imms:
            sim, res, _, _, _ = run_words([i_type(imm, 1, f3, 3)] + PASS_SEQ, {1: a})
            assert sim.x[3] == ref(a, imm) & 0xFFFFFFFF, (name, hex(a), imm)


@pytest.mark.parametrize("f3,f7,ref", [
    (1, 0, lambda a, s: a << s),
    (5, 0, lambda a, s: (a & 0xFFFFFFFF) >> s),
    (5, 0x20, lambda a, s: s32(a) >> s),
])
def test_shift_immediates(f3, f7, ref):
    for a in EDGE:
        for sh in range(32):
            insn = (f7 << 25) | (sh << 20) | (1 << 15) | (f3 << 12) | (3 << 7) | 0x13
            sim, _, _, _, _ = run_words([insn] + PASS_SEQ, {1: a})
            assert sim.x[3] == ref(a, sh) & 0xFFFFFFFF


def test_lui_auipc():
    words = [lui(1, 0xABCDE000), u_type(0xFFFFF000, 2, 0x17), u_type(0x00001000, 3, 0x17)] + PASS_SEQ
    sim, res, tr, _, _ = run_words(words)
    assert sim.x[1] == 0xABCDE000
    assert sim.x[2] == (BASE + 4 - 0x1000) & 0xFFFFFFFF
    assert sim.x[3] == BASE + 8 + 0x1000


def test_x0_is_never_written():
    words = [addi(0, 0, 5), lui(0, 0x12345000), r_type(0, 1, 1, 0, 0),
             i_type(0, 2, 2, 0, op=0x03),  # lw x0, 0(x2)
             j_type(8, 0), 0, addi(4, 0, 9)] + PASS_SEQ
    sim, res, tr, _, _ = run_words(words, {1: 7, 2: BASE + 0x800})
    assert res.status == "PASS"
    assert sim.x[0] == 0 and sim.x[4] == 9
    # rd field only when rd != 0, but the load still reports its address.
    assert tr[0] == f"{BASE:08x} {words[0]:08x}"
    assert tr[3] == f"{BASE + 12:08x} {words[3]:08x} ld {BASE + 0x800:08x}"


# --------------------------------------------------------------------------- memory


def test_loads_extend_and_store_lanes():
    pattern = bytes([0x81, 0x7F, 0xFF, 0x00, 0x34, 0x92, 0x56, 0xF8])
    dbase = BASE + 0x1000
    cases = [  # f3, size, signed
        (0, 1, True), (4, 1, False), (1, 2, True), (5, 2, False), (2, 4, True)]
    for f3, size, signed in cases:
        for off in range(0, 8, size):
            sim, res, tr, _, _ = run_words([i_type(off, 1, f3, 3, op=0x03)] + PASS_SEQ,
                                           {1: dbase}, data={dbase: pattern})
            raw = int.from_bytes(pattern[off:off + size], "little")
            exp = raw - (1 << (8 * size)) if signed and raw >> (8 * size - 1) else raw
            assert sim.x[3] == exp & 0xFFFFFFFF, (f3, off)
            assert tr[0].endswith(f" ld {(dbase + off) & ~3:08x}")


@pytest.mark.parametrize("f3,size", [(0, 1), (1, 2), (2, 4)])
def test_store_trace_byte_lanes(f3, size):
    dbase = BASE + 0x1000
    for off in range(0, 4, size):
        val = 0xA1B2C3D4
        sim, res, tr, _, _ = run_words([s_type(off, 2, 1, f3)] + PASS_SEQ, {1: dbase, 2: val})
        mask = ((1 << size) - 1) << off
        data = ((val & ((1 << (8 * size)) - 1)) << (8 * off)) & 0xFFFFFFFF
        assert tr[0] == f"{BASE:08x} {s_type(off, 2, 1, f3):08x} st {dbase:08x} {mask:x} {data:08x}"
        word = sim.read_word(dbase)
        assert word == data


def test_spec_trace_examples():
    # SPEC section 6: "80000004 00100093 x01 00000001" and a sw of 0x10000 to 0x80001ffc.
    sim, res, tr, _, _ = run_words([addi(0, 0, 0), 0x00100093] + PASS_SEQ)
    assert tr[1] == "80000004 00100093 x01 00000001"
    # 0x00112623 = sw x1, 12(x2); with x2 = 0x80001ff0 the word is at 0x80001ffc.
    sim, res, tr, _, _ = run_words([0x00112623] + PASS_SEQ, {1: 0x10000, 2: 0x80001FF0})
    assert tr[0] == "80000000 00112623 st 80001ffc f 00010000"


@pytest.mark.parametrize("insn,what", [
    (i_type(1, 1, 1, 3, op=0x03), "lh"), (i_type(2, 1, 2, 3, op=0x03), "lw"),
    (i_type(3, 1, 5, 3, op=0x03), "lhu"), (s_type(1, 2, 1, 1), "sh"), (s_type(2, 2, 1, 2), "sw"),
])
def test_misaligned_access_traps(insn, what):
    sim, res, tr, _, _ = run_words([insn] + PASS_SEQ, {1: BASE + 0x1000, 2: 5})
    assert res.status == "TRAP", what
    assert tr == [f"{BASE:08x} {insn:08x} trap"]
    assert sim.x[3] == 0
    assert sim.read_word(BASE + 0x1000) == 0


def test_mmio_loads_read_zero_and_putchar():
    words = li32(1, 0x10000004) + [i_type(0, 1, 2, 3, op=0x03)]  # lw x3, 0(x1)
    for ch in "hi\n":
        words += [addi(2, 0, ord(ch)), s_type(0, 2, 1, 0)]
    sim, res, tr, _, out = run_words(words + PASS_SEQ, {3: 99})
    assert res.status == "PASS"
    assert sim.x[3] == 0
    assert out == "hi\n"


@pytest.mark.parametrize("addr,val,status,code", [
    (0x10000000, 1, "PASS", 0), (0x10000000, 7, "FAIL", 3), (0x10000000, (42 << 1) | 1, "FAIL", 42),
    (0x10000008, 0, "PASS", 0), (0x10000008, 5, "FAIL", 5),
])
def test_tohost_and_halt(addr, val, status, code):
    words = li32(1, addr) + li32(2, val) + [sw(2, 1, 0), addi(5, 0, 1)]
    sim, res, tr, _, _ = run_words(words)
    assert (res.status, res.code) == (status, code)
    assert res.line() == (status if status == "PASS" else f"FAIL {code}")
    # The halting store is the last retired instruction and is traced.
    assert tr[-1].endswith(f" st {addr:08x} f {val:08x}")
    assert sim.instret == len(tr) == 5


def test_unmapped_access_is_a_simulator_error():
    sim, res, tr, _, _ = run_words(li32(1, 0x20000000) + [i_type(0, 1, 2, 3, op=0x03)])
    assert res.status == "ERROR"
    sim, res, tr, _, _ = run_words(li32(1, 0x1000000C) + [sw(1, 1, 0)])
    assert res.status == "ERROR"
    # Running off the end of RAM is a fetch error too.
    sim, res, _, _, _ = run_words(li32(1, BASE + I.RAM_SIZE) + [i_type(0, 1, 0, 0, op=0x67)])
    assert res.status == "ERROR"


def test_self_modifying_code_is_seen():
    # Word 8 runs once, is overwritten by the store at word 6, and must run patched.
    target = BASE + 4 * 8
    words = (li32(1, target) + li32(2, addi(5, 0, 2))   # 0..3
             + [addi(6, 0, 0), j_type(12, 0)]          # 4: x6 = 0; 5: jump to 8
             + [sw(2, 1, 0), 0x0000100F]               # 6: patch word 8; 7: fence.i
             + [addi(5, 0, 1)]                         # 8: patched to "addi x5, x0, 2"
             + [addi(6, 6, 1), addi(7, 0, 2),          # 9, 10
                b_type(-20, 7, 6, 1)]                  # 11: bne x6, x7 -> 6
             + PASS_SEQ)
    sim, res, tr, _, _ = run_words(words)
    assert res.status == "PASS"
    assert sim.x[5] == 2
    assert [l.split()[1] for l in tr if l.startswith(f"{target:08x}")] == \
        [f"{addi(5, 0, 1):08x}", f"{addi(5, 0, 2):08x}"]


# --------------------------------------------------------------------------- control flow


@pytest.mark.parametrize("f3,ref", [
    (0, lambda a, b: a == b), (1, lambda a, b: a != b),
    (4, lambda a, b: s32(a) < s32(b)), (5, lambda a, b: s32(a) >= s32(b)),
    (6, lambda a, b: a < b), (7, lambda a, b: a >= b),
])
def test_branches(f3, ref):
    for a in EDGE[:12]:
        for b in EDGE[:12]:
            words = [b_type(8, 2, 1, f3), addi(3, 0, 1), addi(4, 0, 1)] + PASS_SEQ
            sim, res, tr, bt, _ = run_words(words, {1: a, 2: b})
            taken = ref(a, b)
            assert sim.x[3] == (0 if taken else 1), (f3, hex(a), hex(b))
            assert bt[0] == f"{BASE:08x} 1 {int(taken)} {BASE + 8:08x}"
            assert sim.branches == 1


def test_branch_to_next_instruction_reports_taken():
    sim, res, tr, bt, _ = run_words([b_type(4, 0, 0, 0)] + PASS_SEQ)
    assert bt[0] == f"{BASE:08x} 1 1 {BASE + 4:08x}"


def test_misaligned_branch_traps_only_when_taken():
    sim, res, tr, _, _ = run_words([b_type(6, 0, 0, 1)] + PASS_SEQ)  # bne x0,x0: not taken
    assert res.status == "PASS"
    sim, res, tr, _, _ = run_words([b_type(6, 0, 0, 0)] + PASS_SEQ)  # beq x0,x0: taken
    assert res.status == "TRAP" and tr == [f"{BASE:08x} {b_type(6, 0, 0, 0):08x} trap"]


def test_jal_jalr_link_and_kinds():
    words = [j_type(8, 1),                      # 0: call -> 2
             j_type(12, 0),                     # 1: jal (rd=0) -> 4
             i_type(0, 1, 0, 0, op=0x67),       # 2: ret -> 1
             0,                                 # 3: never reached
             li32(6, BASE + 28)[0], li32(6, BASE + 28)[1],  # 4, 5
             i_type(1, 6, 0, 7, op=0x67),       # 6: jalr x7, 1(x6): bit 0 cleared -> 7
             i_type(8, 7, 0, 5, op=0x67),       # 7: jalr x5, 8(x7): call (rd = x5) -> 9
             0,                                 # 8: never reached
             ] + PASS_SEQ
    sim, res, tr, bt, _ = run_words(words)
    assert res.status == "PASS", res.reason
    kinds = [line.split()[1] for line in bt]
    assert kinds == ["4", "5", "2", "3", "4"]
    assert sim.x[1] == BASE + 4
    assert sim.x[7] == BASE + 28
    assert sim.x[5] == BASE + 32
    assert bt[3] == f"{BASE + 24:08x} 3 1 {BASE + 28:08x}"
    assert sim.jumps == 5


def test_jalr_rd_equals_rs1():
    words = li32(1, BASE + 16) + [i_type(0, 1, 0, 1, op=0x67), 0] + PASS_SEQ
    sim, res, _, _, _ = run_words(words)
    assert res.status == "PASS" and sim.x[1] == BASE + 12


def test_misaligned_jumps_trap_without_writing_rd():
    sim, res, tr, _, _ = run_words([j_type(6, 1)] + PASS_SEQ)
    assert res.status == "TRAP" and sim.x[1] == 0
    sim, res, tr, _, _ = run_words([i_type(2, 0, 0, 1, op=0x67)] + PASS_SEQ, {})
    assert res.status == "TRAP" and sim.x[1] == 0
    assert tr == [f"{BASE:08x} {i_type(2, 0, 0, 1, op=0x67):08x} trap"]


def test_ret_classification_rules():
    assert I.jump_kind(0, 1, True) == I.K_RET
    assert I.jump_kind(0, 5, True) == I.K_RET
    assert I.jump_kind(1, 5, True) == I.K_CALL
    assert I.jump_kind(5, 1, True) == I.K_CALL
    assert I.jump_kind(0, 2, True) == I.K_JALR
    assert I.jump_kind(2, 1, True) == I.K_JALR
    assert I.jump_kind(0, None, False) == I.K_JAL
    assert I.jump_kind(5, None, False) == I.K_CALL


# --------------------------------------------------------------------------- system


@pytest.mark.parametrize("insn,why", [
    (ECALL, "ecall"), (EBREAK, "ebreak"), (0x00000000, "all zero"), (0xFFFFFFFF, "all ones"),
    (0x30200073, "mret"), (0x10500073, "wfi"),
    (r_type(1, 2, 1, 0, 3), "mul (no M)"), (r_type(0x20, 2, 1, 1, 3), "sub-like sll"),
    ((1 << 25) | i_type(1, 1, 1, 3), "slli shamt[5]"), ((0x10 << 25) | i_type(1, 1, 5, 3), "bad srai"),
    (i_type(0, 1, 3, 3, op=0x03), "ld"), (i_type(0, 1, 6, 3, op=0x03), "lwu"),
    (s_type(0, 2, 1, 3), "sd"), (b_type(8, 0, 0, 2), "branch f3=2"), (i_type(0, 1, 1, 3, op=0x67), "jalr f3"),
    (i_type(0, 0, 2, 0, op=0x0F), "misc-mem f3=2"),
    (csr(1, 3, 0, 0xC00), "csrrw cycle"), (csr(2, 3, 1, 0xC00), "csrrs cycle, x1"),
    (csr(3, 3, 1, 0xC02), "csrrc instret, x1"), (csr(5, 3, 0, 0xC00), "csrrwi cycle, 0"),
    (csr(6, 3, 1, 0xC00), "csrrsi cycle, 1"), (csr(2, 3, 0, 0x300), "mstatus"),
    (csr(2, 3, 0, 0xB00), "mcycle"), (csr(1, 0, 0, 0xF14), "write mhartid"), (csr(4, 3, 0, 0xC00), "f3=4"),
    (0xC0001073, "unimp"), (0x0000000B, "custom-0"), (0x00000077, "opcode 0x77"),
])
def test_traps_and_illegal(insn, why):
    sim, res, tr, _, _ = run_words([addi(3, 0, 7), insn, addi(4, 0, 1)] + PASS_SEQ)
    assert res.status == "TRAP", why
    assert tr[-1] == f"{BASE + 4:08x} {insn:08x} trap"
    assert sim.instret == 2 and sim.x[4] == 0


@pytest.mark.parametrize("insn", [
    0x0FF0000F, 0x0000100F, 0x8330000F,          # fence, fence.i, fence.tso
    csr(2, 3, 0, 0xC00), csr(3, 3, 0, 0xC01), csr(6, 3, 0, 0xC02), csr(7, 3, 0, 0xC80),
    csr(2, 0, 0, 0xC82), csr(2, 3, 0, 0xF14), i_type(5, 0, 0, 0),  # nop-like hint
])
def test_legal_system_and_fence(insn):
    sim, res, tr, _, _ = run_words([insn] + PASS_SEQ)
    assert res.status == "PASS", hex(insn)


def test_counter_reads():
    words = [addi(0, 0, 0)] * 5 + [csr(2, 3, 0, 0xC00), csr(2, 4, 0, 0xC02), csr(2, 5, 0, 0xC81),
                                   csr(2, 0, 0, 0xC01), csr(2, 6, 0, 0xF14)] + PASS_SEQ
    sim, res, tr, _, _ = run_words(words, {6: 3})
    assert sim.x[3] == 5 and sim.x[4] == 6 and sim.x[5] == 0 and sim.x[6] == 0
    assert tr[5] == f"{BASE + 20:08x} {words[5]:08x} x03 00000005 csr"
    assert tr[8] == f"{BASE + 32:08x} {words[8]:08x} csr"
    assert tr[9] == f"{BASE + 36:08x} {words[9]:08x} x06 00000000"  # mhartid is not a counter


def test_timeout():
    sim, res, tr, _, _ = run_words([j_type(0, 0)], max_instr=100)
    assert res.status == "TIMEOUT" and sim.instret == 100 and len(tr) == 100


def test_stats_line_format():
    sim, res, _, _, _ = run_words([b_type(8, 0, 0, 0), 0, j_type(4, 0)] + PASS_SEQ)
    assert sim.stats_line() == ("cycles=6 instret=6 cpi=1.000 branches=1 br_mispred=0 "
                                "jumps=1 jump_mispred=0")


# --------------------------------------------------------------------------- decoder vs binutils

OBJDUMP = shutil.which("riscv64-elf-objdump") or "/opt/homebrew/bin/riscv64-elf-objdump"


@pytest.mark.skipif(not os.path.exists(OBJDUMP), reason="riscv64-elf-objdump not found")
def test_legality_matches_objdump(tmp_path):
    """For every opcode except SYSTEM (where the CSR rules are ours) and MISC-MEM,
    random encodings must be legal in the ISS exactly when GNU objdump decodes them as
    RV32I. MISC-MEM is left out because the ISA says base implementations shall ignore
    the reserved fields of FENCE and FENCE.I, while objdump prints them as .insn."""
    rng = random.Random(1)
    opcodes = [0x03, 0x13, 0x17, 0x23, 0x33, 0x37, 0x63, 0x67, 0x6F]
    words = []
    for _ in range(6000):
        w = rng.getrandbits(32)
        words.append((w & ~0x7F) | rng.choice(opcodes))
    blob = tmp_path / "w.bin"
    blob.write_bytes(b"".join(struct.pack("<I", w) for w in words))
    out = subprocess.run([OBJDUMP, "-D", "-b", "binary", "-m", "riscv:rv32",
                          "-M", "no-aliases",
                          str(blob)], capture_output=True, text=True, check=True).stdout
    # objdump disassembles with every extension enabled; restrict to what RV32I knows.
    rv32i = {"lb", "lh", "lw", "lbu", "lhu", "sb", "sh", "sw", "addi", "slti", "sltiu", "xori", "ori",
             "andi", "slli", "srli", "srai", "add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra",
             "or", "and", "lui", "auipc", "jal", "jalr", "beq", "bne", "blt", "bge", "bltu", "bgeu",
             "fence", "fence.i", "fence.tso", "pause"}
    seen = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip().endswith(":"):
            off = int(parts[0].strip()[:-1], 16)
            seen[off // 4] = parts[2].split()[0] if parts[2].split() else ""
    assert len(seen) == len(words)
    sim = I.ISS(out=io.StringIO())
    mismatches = []
    for i, w in enumerate(words):
        mn = seen[i]
        legal_gnu = mn in rv32i
        if mn in ("slli", "srli", "srai") and (w >> 25) & 1:
            legal_gnu = False  # objdump accepts shamt >= 32 on rv32; RV32I says illegal
        sim.x[:] = [0] * 32
        legal_iss = not _is_illegal(sim, w)
        if legal_gnu != legal_iss:
            mismatches.append((hex(w), mn, legal_iss))
    assert not mismatches, mismatches[:10]


def _is_illegal(sim, w):
    fn = sim.decode(BASE, w)[0]
    try:
        fn(BASE)
    except I.Trap as t:
        return str(t) == "illegal instruction"
    except I.SimError:
        return False
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- riscv-tests

RT_ELFS = sorted(glob.glob(os.path.join(ROOT, "build", "sw", "riscv-tests", "rv32ui-p-*.elf")))


@pytest.mark.skipif(not RT_ELFS, reason="riscv-tests not built (make riscv-tests-elf)")
@pytest.mark.parametrize("elf", RT_ELFS, ids=[os.path.basename(e)[:-4] for e in RT_ELFS])
def test_riscv_tests(elf):
    sim = I.ISS(out=io.StringIO())
    sim.load_elf(elf)
    tr = io.StringIO()
    res = sim.run(1_000_000, tr)
    assert res.status == "PASS", (res.line(), res.reason)
    lines = tr.getvalue().splitlines()
    assert len(lines) == sim.instret
    assert lines[-1].endswith(" st 10000000 f 00000001")


@pytest.mark.skipif(not RT_ELFS, reason="riscv-tests not built")
def test_riscv_tests_detect_a_broken_model(monkeypatch):
    """A test suite that cannot fail proves nothing: break SUB and check the test fails."""
    orig = I.ISS.decode

    def broken(self, pc, insn):
        if insn & 0xFE00707F == 0x40000033:  # sub -> add
            insn &= ~0x40000000
        return orig(self, pc, insn)

    monkeypatch.setattr(I.ISS, "decode", broken)
    elf = os.path.join(ROOT, "build", "sw", "riscv-tests", "rv32ui-p-sub.elf")
    sim = I.ISS(out=io.StringIO())
    sim.load_elf(elf)
    res = sim.run(1_000_000)
    assert res.status == "FAIL" and res.code > 1
