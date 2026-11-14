"""rv_decoder: legality, register fields, immediates and control-flow kind of random words
(biased towards real opcodes) and of hand-picked encodings, against an independent Python
reference written from the ISA manual. Immediates come from the ISS (model/iss.py)."""

import os
import random
import sys

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "model"))
import iss  # noqa: E402

LUI, AUIPC, JAL, JALR, BRANCH, LOAD, STORE, OPIMM, OP, FENCE, SYSTEM = (
    0x37, 0x17, 0x6F, 0x67, 0x63, 0x03, 0x23, 0x13, 0x33, 0x0F, 0x73)
COUNTERS = {0xC00, 0xC01, 0xC02, 0xC80, 0xC81, 0xC82}
MHARTID = 0xF14


def legal(insn):
    op, f3, f7 = insn & 0x7F, (insn >> 12) & 7, insn >> 25
    rs1 = (insn >> 15) & 0x1F
    if op in (LUI, AUIPC, JAL):
        return True
    if op == JALR:
        return f3 == 0
    if op == BRANCH:
        return f3 not in (2, 3)
    if op == LOAD:
        return f3 in (0, 1, 2, 4, 5)
    if op == STORE:
        return f3 in (0, 1, 2)
    if op == OPIMM:
        if f3 == 1:
            return f7 == 0
        if f3 == 5:
            return f7 in (0, 0x20)
        return True
    if op == OP:
        return f7 == 0 or (f7 == 0x20 and f3 in (0, 5))
    if op == FENCE:
        return f3 in (0, 1)
    if op == SYSTEM:
        if f3 == 0:
            return insn in (0x00000073, 0x00100073)
        if f3 == 4:
            return False
        writes = f3 in (1, 5) or rs1 != 0
        return not writes and ((insn >> 20) in COUNTERS or (insn >> 20) == MHARTID)
    return False


def expect(insn):
    op = insn & 0x7F
    rd, rs1 = (insn >> 7) & 0x1F, (insn >> 15) & 0x1F
    ok = legal(insn)
    e = {"illegal": int(not ok)}
    if not ok:
        e.update(rd_we=0, use_rs1=0, use_rs2=0, is_load=0, is_store=0, is_branch=0, is_jal=0,
                 is_jalr=0, cf_kind=0, is_fence_i=0)
        return e
    writes = op in (LUI, AUIPC, JAL, JALR, LOAD, OPIMM, OP) or (op == SYSTEM and (insn >> 12) & 7)
    e["rd_we"] = int(bool(writes) and rd != 0)
    e["use_rs1"] = int(op in (JALR, BRANCH, LOAD, STORE, OPIMM, OP))
    e["use_rs2"] = int(op in (BRANCH, STORE, OP))
    e["is_load"] = int(op == LOAD)
    e["is_store"] = int(op == STORE)
    e["is_branch"] = int(op == BRANCH)
    e["is_jal"] = int(op == JAL)
    e["is_jalr"] = int(op == JALR)
    e["is_fence_i"] = int(op == FENCE and (insn >> 12) & 7 == 1)
    e["ecall"] = int(insn == 0x73)
    e["ebreak"] = int(insn == 0x100073)
    if op == BRANCH:
        e["cf_kind"] = iss.K_BRANCH
    elif op in (JAL, JALR):
        e["cf_kind"] = iss.jump_kind(rd, rs1, op == JALR)
    else:
        e["cf_kind"] = iss.K_NONE
    imm = None
    if op in (JALR, LOAD, OPIMM):
        imm = iss.imm_i(insn)
    elif op == STORE:
        imm = iss.imm_s(insn)
    elif op == BRANCH:
        imm = iss.imm_b(insn)
    elif op in (LUI, AUIPC):
        imm = iss.imm_u(insn)
    elif op == JAL:
        imm = iss.imm_j(insn)
    if imm is not None:
        e["imm"] = imm & 0xFFFFFFFF
    return e


HAND = [
    0x00000013,  # nop
    0x00100093,  # addi x1, x0, 1
    0xFFF00093,  # addi x1, x0, -1
    0x40B50533,  # sub a0, a0, a1
    0x00B50533,  # add a0, a0, a1
    0x02B50533,  # mul a0, a0, a1 (M extension): illegal
    0x4015D513,  # srai a0, a1, 1
    0x0015D513,  # srli a0, a1, 1
    0x0215D513,  # bad funct7 on srli: illegal
    0x000080E7,  # jalr ra, 0(ra): call
    0x00008067,  # ret
    0x00028067,  # jr t0: ret (x5)
    0x000500E7,  # jalr ra, 0(a0): call
    0x00050067,  # jr a0: plain jalr
    0x008000EF,  # jal ra, +8: call
    0x0080006F,  # j +8
    0x0000100F,  # fence.i
    0x0FF0000F,  # fence
    0x00000073,  # ecall
    0x00100073,  # ebreak
    0x30200073,  # mret: illegal
    0x10500073,  # wfi: illegal
    0xC0002573,  # rdcycle a0
    0xC8202573,  # rdinstreth a0
    0xF1402573,  # csrr a0, mhartid
    0xC0001573,  # csrrw a0, cycle, x0: a write, illegal
    0xC0056573,  # csrrsi a0, cycle, 10: a write, illegal
    0x34002573,  # csrr a0, mscratch: unknown, illegal
    0x00000000,  # all zero: illegal
    0xFFFFFFFF,  # all ones: illegal
    0x0000A003,  # lw x0, 0(x1): legal, no write
    0x00003003,  # ld: illegal on RV32
    0x00003023,  # sd: illegal on RV32
]

OPCODES = [LUI, AUIPC, JAL, JALR, BRANCH, LOAD, STORE, OPIMM, OP, FENCE, SYSTEM]


@cocotb.test()
async def decoder_fields(dut):
    rng = random.Random(3)
    words = list(HAND)
    for _ in range(20000):
        w = rng.getrandbits(32)
        r = rng.random()
        if r < 0.8:
            w = (w & ~0x7F) | rng.choice(OPCODES)
        if r < 0.3:
            w &= ~(0x7F << 25) | (rng.choice([0, 0x20]) << 25)  # common funct7 values
        if rng.random() < 0.2 and (w & 0x7F) == SYSTEM:
            w = (w & 0xFFFFF) | (rng.choice(sorted(COUNTERS) + [MHARTID]) << 20)
            if rng.random() < 0.5:
                w &= ~(0x1F << 15)  # rs1 = x0: a pure read
        words.append(w & 0xFFFFFFFF)

    for w in words:
        dut.insn_i.value = w
        await Timer(1, unit="ns")
        e = expect(w)
        for k, v in e.items():
            got = int(getattr(dut, k).value)
            assert got == v, f"insn {w:08x}: {k} = {got:#x}, expected {v:#x}"
        if not e["illegal"]:
            assert int(dut.rd.value) == (w >> 7) & 0x1F
            assert int(dut.rs1.value) == (w >> 15) & 0x1F
            assert int(dut.rs2.value) == (w >> 20) & 0x1F
