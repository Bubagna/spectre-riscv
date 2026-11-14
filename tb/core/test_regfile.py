"""rv_regfile: reset to zero, x0 hard-wired, random writes and reads, write-through bypass."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer


async def reset(dut):
    dut.we_i.value = 0
    dut.waddr_i.value = 0
    dut.wdata_i.value = 0
    dut.raddr_a_i.value = 0
    dut.raddr_b_i.value = 0
    dut.rst_ni.value = 0
    await Timer(15, unit="ns")
    dut.rst_ni.value = 1
    await FallingEdge(dut.clk_i)


@cocotb.test()
async def regfile_random(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())
    await reset(dut)
    rng = random.Random(2)
    ref = [0] * 32

    for r in range(32):
        dut.raddr_a_i.value = r
        await Timer(1, unit="ns")
        assert int(dut.rdata_a_o.value) == 0, f"x{r} not zero after reset"

    for _ in range(2000):
        we = rng.random() < 0.7
        wa = rng.randrange(32)
        wd = rng.getrandbits(32)
        ra = rng.randrange(32)
        rb = wa if rng.random() < 0.3 else rng.randrange(32)  # often read what is being written
        dut.we_i.value = we
        dut.waddr_i.value = wa
        dut.wdata_i.value = wd
        dut.raddr_a_i.value = ra
        dut.raddr_b_i.value = rb
        await Timer(1, unit="ns")

        # Bypass: a read of the register being written sees the new value in the same cycle.
        def expect(r):
            if r == 0:
                return 0
            return wd if (we and wa == r) else ref[r]

        assert int(dut.rdata_a_o.value) == expect(ra), f"port a x{ra}"
        assert int(dut.rdata_b_o.value) == expect(rb), f"port b x{rb}"
        await RisingEdge(dut.clk_i)
        if we and wa != 0:
            ref[wa] = wd
        await FallingEdge(dut.clk_i)
