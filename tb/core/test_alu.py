"""rv_alu: every operation on edge values and random operands, against a Python model."""

import random

import cocotb
from cocotb.triggers import Timer

M = 0xFFFFFFFF
# Same order as rv_pkg::alu_op_e.
OPS = ["ADD", "SUB", "SLL", "SLT", "SLTU", "XOR", "SRL", "SRA", "OR", "AND"]


def s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def model(op, a, b):
    sh = b & 31
    return {
        "ADD": (a + b) & M,
        "SUB": (a - b) & M,
        "SLL": (a << sh) & M,
        "SLT": int(s32(a) < s32(b)),
        "SLTU": int(a < b),
        "XOR": a ^ b,
        "SRL": a >> sh,
        "SRA": (s32(a) >> sh) & M,
        "OR": a | b,
        "AND": a & b,
    }[op]


EDGES = [0, 1, 2, 31, 32, 0x7FFFFFFF, 0x80000000, 0x80000001, 0xFFFFFFFE, 0xFFFFFFFF, 0x12345678]


@cocotb.test()
async def alu_all_ops(dut):
    rng = random.Random(1)
    pairs = [(a, b) for a in EDGES for b in EDGES]
    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(300)]
    for i, op in enumerate(OPS):
        for a, b in pairs:
            dut.op_i.value = i
            dut.a_i.value = a
            dut.b_i.value = b
            await Timer(1, unit="ns")
            got = int(dut.result_o.value)
            assert got == model(op, a, b), f"{op} {a:#x} {b:#x}: got {got:#x}"
