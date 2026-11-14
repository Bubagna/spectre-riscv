"""cocotb tests for rtl/bp/bpu.sv.

Every test runs the golden model (bp_model.py) in lockstep: in every cycle the three prediction
outputs of the DUT are compared with the model, before and after the update/flush inputs are
driven (the second check proves there is no combinational path from them to the outputs).
On top of that, the directed tests assert the behaviour they are named after.

The configuration comes from the environment variable BP_CFG (JSON, set by run_tests.py) and
must match the parameters the DUT was built with.
"""

from __future__ import annotations

import json
import os
import random
import sys
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bp_model import (  # noqa: E402
    CF_BRANCH, CF_CALL, CF_JAL, CF_JALR, CF_NONE, CF_RET,
    BranchPredictorModel, Config, Prediction, Update,
)

CFG = Config(**json.loads(os.environ.get("BP_CFG", "{}")))
RANDOM_CYCLES = int(os.environ.get("BP_RANDOM_CYCLES", "50000"))
SEED = int(os.environ.get("BP_SEED", "1"))
BASE = 0x8000_0000


def _u(handle) -> int:
    return int(handle.value)


class Harness:
    """Drives the DUT one cycle at a time and checks it against the model."""

    def __init__(self, dut):
        self.dut = dut
        self.m = BranchPredictorModel(CFG)
        self.cycle_no = 0
        self._pred: Prediction | None = None
        self._look_args = (0, 0)

    async def start(self) -> None:
        d = self.dut
        cocotb.start_soon(Clock(d.clk_i, 10, unit="ns").start())
        for name in ("pred_valid_i", "pred_pc_i", "pred_fire_i", "upd_valid_i", "upd_pc_i",
                     "upd_kind_i", "upd_taken_i", "upd_target_i", "upd_mispredict_i",
                     "upd_meta_i", "flush_i", "flush_meta_i"):
            getattr(d, name).value = 0
        d.rst_ni.value = 0
        for _ in range(2):
            await RisingEdge(d.clk_i)
        await FallingEdge(d.clk_i)
        d.rst_ni.value = 1
        self.m.reset()

    def _check(self, where: str) -> None:
        d, p = self.dut, self._pred
        got = (_u(d.pred_taken_o), _u(d.pred_target_o), _u(d.pred_meta_o))
        exp = (p.taken, p.target, p.meta)
        if got != exp:
            raise AssertionError(
                f"cycle {self.cycle_no} ({where}) pc={self._look_args[1]:08x} "
                f"valid={self._look_args[0]}: DUT taken/target/meta="
                f"{got[0]}/{got[1]:08x}/{got[2]:x}, model={exp[0]}/{exp[1]:08x}/{exp[2]:x}")

    async def look(self, valid: int, pc: int) -> Prediction:
        """Drive the predict inputs for this cycle and return the (checked) prediction."""
        d = self.dut
        await FallingEdge(d.clk_i)
        d.pred_valid_i.value = valid
        d.pred_pc_i.value = pc
        d.pred_fire_i.value = 0
        d.upd_valid_i.value = 0
        d.flush_i.value = 0
        await Timer(1, unit="ns")
        self._look_args = (valid, pc)
        self._pred = self.m.predict(pc, valid)
        self._check("predict")
        return self._pred

    async def commit(self, fire: int = 0, upd: Update | None = None,
                     flush_meta: int | None = None) -> None:
        """Drive fire/update/flush, check the outputs did not move, clock the DUT and model."""
        d = self.dut
        d.pred_fire_i.value = fire
        if upd is not None:
            d.upd_valid_i.value = 1
            d.upd_pc_i.value = upd.pc
            d.upd_kind_i.value = upd.kind
            d.upd_taken_i.value = upd.taken
            d.upd_target_i.value = upd.target
            d.upd_mispredict_i.value = upd.mispredict
            d.upd_meta_i.value = upd.meta
        if flush_meta is not None:
            d.flush_i.value = 1
            d.flush_meta_i.value = flush_meta
        await Timer(1, unit="ns")
        self._check("after update/flush inputs")
        await RisingEdge(d.clk_i)
        valid, pc = self._look_args
        self.m.tick(valid, pc, fire, upd=upd, flush_meta=flush_meta)
        self.cycle_no += 1

    # Transaction-level helpers for the directed tests.

    async def fire(self, pc: int) -> Prediction:
        p = await self.look(1, pc)
        await self.commit(fire=1)
        return p

    async def resolve(self, pc: int, kind: int, taken: int, target: int, pred: Prediction) -> bool:
        """Send the update for an instruction predicted as `pred`. Returns True if mispredicted."""
        nxt = target if taken else (pc + 4) & 0xFFFF_FFFF
        mis = int(pred.target != nxt)
        await self.look(0, 0)
        await self.commit(upd=Update(pc, kind, taken, target, mis, pred.meta))
        return bool(mis)

    async def run(self, pc: int, kind: int, taken: int, target: int) -> tuple[Prediction, bool]:
        """Fetch one instruction and resolve it right away (no other instruction in flight)."""
        p = await self.fire(pc)
        mis = await self.resolve(pc, kind, taken, target, p)
        return p, mis

    async def flush(self, meta: int) -> None:
        await self.look(0, 0)
        await self.commit(flush_meta=meta)


def at(idx: int, region: int = 1) -> int:
    """PC with BTB index `idx` (for any BTB_ENTRIES > idx); regions differ only in the tag.
    The directed tests place their instructions with this so that they do not evict each other
    even in the 4-entry BTB configuration."""
    return BASE + 0x10000 * region + 4 * idx


def has_dir() -> bool:
    return CFG.bp_type in ("BIMODAL", "GSHARE", "TOURNAMENT")


async def setup(dut) -> Harness:
    h = Harness(dut)
    await h.start()
    return h


# ------------------------------------------------------------------------------- directed tests

@cocotb.test()
async def test_reset_state(dut):
    """After reset nothing hits: every PC is predicted not taken to pc+4."""
    h = await setup(dut)
    for i in range(16):
        p = await h.look(1, BASE + 4 * i)
        await h.commit()
        assert p.taken == 0 or CFG.bp_type == "RANDOM"


@cocotb.test()
async def test_loop_branch_learns_taken(dut):
    """A backward loop branch taken over and over ends up predicted taken (not by STATIC_NT)."""
    h = await setup(dut)
    br, tgt = BASE + 0x40, BASE + 0x20
    preds = []
    for _ in range(CFG.ghr_bits + 12):
        p, _ = await h.run(br, CF_BRANCH, 1, tgt)
        preds.append(p)
    tail = preds[-5:]
    if has_dir():
        # gshare needs GHR_BITS taken outcomes before its history stops changing.
        assert all(p.taken and p.target == tgt for p in tail), "loop branch not learned"
    elif CFG.bp_type == "STATIC_NT":
        assert not any(p.taken for p in preds), "STATIC_NT predicted a branch taken"
        assert all(p.btb_hit for p in tail)


@cocotb.test()
async def test_alternating_pattern(dut):
    """T,N,T,N: learned by global history (gshare, tournament), not by bimodal."""
    h = await setup(dut)
    br, tgt = BASE + 0x80, BASE + 0x10
    mis = []
    for i in range(200):
        _, m = await h.run(br, CF_BRANCH, int(i % 2 == 0), tgt)
        mis.append(m)
    late = sum(mis[-50:])
    dut._log.info(f"{CFG.bp_type}: mispredictions in the last 50 of 200 = {late}")
    if CFG.bp_type in ("GSHARE", "TOURNAMENT"):
        assert late == 0
    elif CFG.bp_type == "BIMODAL":
        assert late >= 20, "bimodal should not be able to learn an alternating branch"


@cocotb.test()
async def test_btb_aliasing(dut):
    """Two jumps with the same BTB index and different tags evict each other."""
    h = await setup(dut)
    a = BASE + 8 * CFG.btb_entries       # index 0, even tag
    b = a + 4 * CFG.btb_entries          # same index, tag differs only in its lowest bit
    c = a + 4                            # different index
    ta, tb, tc = BASE + 0x800, BASE + 0x900, BASE + 0xA00
    await h.run(a, CF_JAL, 1, ta)
    await h.run(c, CF_JAL, 1, tc)
    pa = await h.fire(a)
    await h.resolve(a, CF_JAL, 1, ta, pa)
    await h.run(b, CF_JAL, 1, tb)        # evicts a
    pa2 = await h.fire(a)
    await h.resolve(a, CF_JAL, 1, ta, pa2)
    pc_ = await h.fire(c)
    await h.resolve(c, CF_JAL, 1, tc, pc_)
    pb = await h.fire(b)                 # a was written back by the update above: b misses
    await h.resolve(b, CF_JAL, 1, tb, pb)
    if CFG.bp_type != "RANDOM":
        assert pa.taken and pa.target == ta
        assert not pa2.taken, "b should have evicted a"
        assert pc_.taken and pc_.target == tc, "c (other index) must not be disturbed"
        assert not pb.taken, "a should have evicted b"
    assert pa.btb_hit and not pa2.btb_hit and pc_.btb_hit and not pb.btb_hit


@cocotb.test()
async def test_not_taken_branch_not_allocated(dut):
    """A not-taken branch that misses is not written into the BTB."""
    h = await setup(dut)
    br = BASE + 0x200
    await h.run(br, CF_BRANCH, 0, BASE + 0x300)
    p = await h.look(1, br)
    await h.commit()
    assert not p.btb_hit


@cocotb.test()
async def test_kind_none_invalidates(dut):
    """An update with kind 0 (PC is not control flow any more) removes the BTB entry."""
    h = await setup(dut)
    pc, tgt = BASE + 0x340, BASE + 0x500
    await h.run(pc, CF_JAL, 1, tgt)
    p = await h.fire(pc)
    assert p.btb_hit
    await h.resolve(pc, CF_NONE, 0, pc + 4, p)   # e.g. the code was rewritten
    p2 = await h.look(1, pc)
    await h.commit()
    assert not p2.btb_hit
    if CFG.bp_type != "RANDOM":
        assert p.taken and not p2.taken


async def warm_call(h: Harness, call_pc: int, fn: int) -> None:
    await h.run(call_pc, CF_CALL, 1, fn)


@cocotb.test()
async def test_call_ret_ras(dut):
    """One function called from two sites: the RAS predicts both returns, the BTB alone cannot."""
    h = await setup(dut)
    fn, ret = BASE + 0x1000, at(2, 2)
    c1, c2 = at(0), at(1)
    # Warm the BTB: both calls and the return are known.
    await h.run(c1, CF_CALL, 1, fn)
    await h.run(ret, CF_RET, 1, c1 + 4)
    await h.run(c2, CF_CALL, 1, fn)
    await h.run(ret, CF_RET, 1, c2 + 4)
    ok = 0
    for site in (c1, c2, c1, c2):
        p, _ = await h.run(site, CF_CALL, 1, fn)
        pr, mis = await h.run(ret, CF_RET, 1, site + 4)
        ok += not mis
    if CFG.bp_type != "RANDOM":
        if CFG.ras_depth:
            assert ok == 4, "RAS should predict every return"
        else:
            assert ok == 0, "without RAS the BTB holds the previous return target"


@cocotb.test()
async def test_nested_calls(dut):
    """Calls nested up to the RAS depth return in the right order."""
    h = await setup(dut)
    depth = min(max(CFG.ras_depth, 1), 4)
    calls = [at(2 * i, 1 + i) for i in range(depth)]       # call i is inside function i-1
    rets = [at(2 * i + 1, 8 + i) for i in range(depth)]
    fns = [r - 0x40 for r in rets]
    # warm the BTB
    for i in range(depth):
        await h.run(calls[i], CF_CALL, 1, fns[i])
    for i in reversed(range(depth)):
        await h.run(rets[i], CF_RET, 1, calls[i] + 4)
    # measured pass
    for i in range(depth):
        await h.run(calls[i], CF_CALL, 1, fns[i])
    results = []
    for i in reversed(range(depth)):
        p, mis = await h.run(rets[i], CF_RET, 1, calls[i] + 4)
        results.append(mis)
    if CFG.bp_type != "RANDOM" and CFG.ras_depth:
        assert not any(results), f"nested returns mispredicted: {results}"


@cocotb.test()
async def test_ras_overflow(dut):
    """Nesting deeper than RAS_DEPTH: the innermost RAS_DEPTH returns hit, the outer ones miss."""
    if CFG.ras_depth == 0:
        return
    h = await setup(dut)
    d = CFG.ras_depth
    n = d + 2
    calls = [at(2 * i, 1 + i) for i in range(n)]
    rets = [at(2 * i + 1, 32 + i) for i in range(n)]
    fns = [r - 0x20 for r in rets]
    for i in range(n):
        await h.run(calls[i], CF_CALL, 1, fns[i])
    for i in reversed(range(n)):
        await h.run(rets[i], CF_RET, 1, calls[i] + 4)
    for i in range(n):
        await h.run(calls[i], CF_CALL, 1, fns[i])
    mis = []
    for i in reversed(range(n)):
        _, m = await h.run(rets[i], CF_RET, 1, calls[i] + 4)
        mis.append(m)
    dut._log.info(f"RAS_DEPTH={d}, {n} nested returns, mispredicted: {mis}")
    if CFG.bp_type != "RANDOM" and 2 * n <= CFG.btb_entries:
        assert mis == [False] * d + [True] * 2, f"overflow pattern {mis}"


@cocotb.test()
async def test_indirect_jump_ind(dut):
    """A JALR that alternates between two targets: the BTB alone (last target) is always wrong,
    the indirect table learns the pattern from the path history. A JAL never uses the table."""
    h = await setup(dut)
    jr = at(5, 6)
    ta, tb = BASE + 0x2000, BASE + 0x2004       # differ in target bit 2: any path width sees it
    mis = []
    for i in range(40):
        _, m = await h.run(jr, CF_JALR, 1, (ta, tb)[i % 2])
        mis.append(m)
    late = sum(mis[-16:])
    dut._log.info(f"IND_ENTRIES={CFG.ind_entries}: mispredictions in the last 16 of 40 = {late}")
    if CFG.bp_type != "RANDOM":
        assert late == (0 if CFG.ind_entries else 16), f"pattern {mis}"
    p = await h.look(1, jr)
    await h.commit()
    assert p.btb_hit
    # A jump with one target stays monomorphic and keeps its BTB target, even though its table
    # index may hold the other jump's target.
    j = at(6, 6)
    await h.run(j, CF_JAL, 1, BASE + 0x3000)
    for _ in range(4):
        p, m = await h.run(j, CF_JAL, 1, BASE + 0x3000)
        if CFG.bp_type != "RANDOM":
            assert not m and p.target == BASE + 0x3000


@cocotb.test()
async def test_mispredict_repairs_ghr(dut):
    """Predictions in flight shift the GHR; a misprediction restores it and adds the real outcome."""
    if CFG.bp_type not in ("GSHARE", "TOURNAMENT"):
        return
    h = await setup(dut)
    mask = (1 << CFG.ghr_bits) - 1
    b1, b2, b3 = at(1), at(2), at(3)
    for b in (b1, b2, b3):             # get them into the BTB (taken once)
        await h.run(b, CF_BRANCH, 1, b + 0x400)
    p1 = await h.fire(b1)
    p2 = await h.fire(b2)              # younger, on the path predicted for b1
    p3 = await h.fire(b3)
    g0 = p1.meta & mask
    assert p2.meta & mask == ((g0 << 1) | p1.taken) & mask, "GHR not shifted at fire"
    assert p3.meta & mask == ((g0 << 2) | (p1.taken << 1) | p2.taken) & mask
    actual = 1 - p1.taken              # b1 resolves opposite to its prediction
    assert await h.resolve(b1, CF_BRANCH, actual, b1 + 0x400, p1)
    after = await h.look(1, b3)
    await h.commit()
    assert after.meta & mask == ((g0 << 1) | actual) & mask, "GHR not repaired exactly"

    # A jump that missed in the BTB is not in the history: restore without a shift.
    await h.fire(b3)
    j = at(0, 2)
    pj = await h.fire(j)
    await h.fire(b2)
    assert await h.resolve(j, CF_JAL, 1, BASE + 0x3800, pj)
    after2 = await h.look(1, b3)
    await h.commit()
    assert after2.meta & mask == pj.meta & mask

    # A branch that missed in the BTB (so it was not in the history) but turns out taken:
    # the repair adds it.
    b4 = at(0, 3)
    p4 = await h.fire(b4)
    assert not p4.btb_hit
    await h.fire(b1)
    assert await h.resolve(b4, CF_BRANCH, 1, b4 + 0x400, p4)
    after3 = await h.look(1, b3)
    await h.commit()
    assert after3.meta & mask == (((p4.meta & mask) << 1) | 1) & mask


@cocotb.test()
async def test_mispredict_repairs_ras(dut):
    """Wrong path pops then pushes over the TOS; the repair restores pointer and TOS entry."""
    if CFG.ras_depth == 0:
        return
    h = await setup(dut)
    fn = BASE + 0x1000
    c1, c2, ret, br = at(0), at(1), at(2, 2), at(3, 2)
    await h.run(c1, CF_CALL, 1, fn)          # warm BTB: call c1, ret, call c2, branch
    await h.run(ret, CF_RET, 1, c1 + 4)
    await h.run(c2, CF_CALL, 1, fn)
    await h.run(br, CF_BRANCH, 1, br + 0x40)
    # Real state: inside fn called from c1, so TOS = c1+4.
    await h.run(c1, CF_CALL, 1, fn)
    pb = await h.fire(br)                    # the branch whose prediction will be wrong
    pr = await h.fire(ret)                   # wrong path: pop
    pc2 = await h.fire(c2)                   # wrong path: push c2+4 over the old TOS slot
    await h.resolve(br, CF_BRANCH, 1 - pb.taken, br + 0x40, pb)
    p = await h.look(1, ret)
    await h.commit()
    # The GHR part moved (the branch's real outcome was added), the RAS part must be the checkpoint.
    assert h.m.unpack_meta(p.meta)[1:] == h.m.unpack_meta(pb.meta)[1:], \
        "RAS pointer/TOS not restored to the branch checkpoint"
    if CFG.bp_type != "RANDOM":
        assert p.taken and p.target == c1 + 4, "return after repair must go back to c1+4"
    del pr, pc2


@cocotb.test()
async def test_mispredicted_call_and_ret_replayed(dut):
    """A mispredicted call/ret is replayed on the restored RAS (push/pop of its own effect)."""
    if CFG.ras_depth == 0:
        return
    h = await setup(dut)
    fn = BASE + 0x1800
    call, ret = at(0), at(1, 2)
    pcall = await h.fire(call)               # BTB miss: predicted not taken -> mispredict
    await h.fire(call + 4)                   # wrong path
    assert await h.resolve(call, CF_CALL, 1, fn, pcall)
    # The RAS must now hold call+4 on top, although the call was never pushed at fetch.
    p = await h.look(1, fn)
    await h.commit()
    _, ptr, top = h.m.unpack_meta(p.meta)
    _, ptr0, _ = h.m.unpack_meta(pcall.meta)
    assert top == (call + 4) >> 2 and ptr == (ptr0 + 1) % CFG.ras_depth
    await h.run(ret, CF_RET, 1, call + 4)    # allocate the ret in the BTB (mispredicted)
    await h.run(call, CF_CALL, 1, fn)        # predicted, pushes
    pret = await h.fire(ret)
    if CFG.bp_type != "RANDOM":
        assert pret.taken and pret.target == call + 4
    await h.resolve(ret, CF_RET, 1, call + 4, pret)


@cocotb.test()
async def test_flush_restores(dut):
    """flush_i puts GHR and RAS back to the state before the flushing instruction."""
    h = await setup(dut)
    fn = BASE + 0x1000
    c1, ret, br = at(0), at(1, 2), at(2, 3)
    await h.run(c1, CF_CALL, 1, fn)
    await h.run(ret, CF_RET, 1, c1 + 4)
    await h.run(br, CF_BRANCH, 1, br + 0x40)
    fence = at(3, 4)
    pf = await h.fire(fence)                 # e.g. FENCE.I
    for pc in (c1, br, c1, br, ret):         # younger predictions change GHR and RAS
        await h.fire(pc)
    await h.flush(pf.meta)
    p = await h.look(1, fence + 4)
    await h.commit()
    assert p.meta == pf.meta, "flush did not restore the speculative state"


@cocotb.test()
async def test_fire_ignored_during_repair(dut):
    """In a repair cycle the fetched instruction is wrong path, so its fire is ignored."""
    h = await setup(dut)
    c1, fn = at(0), BASE + 0x1000
    await h.run(c1, CF_CALL, 1, fn)
    j = at(1, 2)
    pj = await h.fire(j)                     # BTB miss: will mispredict
    await h.look(1, c1)                      # a call is being fetched in the repair cycle
    await h.commit(fire=1, upd=Update(j, CF_JAL, 1, BASE + 0x900, 1, pj.meta))
    p = await h.look(1, BASE + 0x900)
    await h.commit()
    assert p.meta == pj.meta, "the fire in the repair cycle changed the speculative state"


@cocotb.test()
async def test_stall_keeps_prediction(dut):
    """Without fire the speculative state does not move, however long fetch is stalled."""
    h = await setup(dut)
    c1, fn, br = at(0), BASE + 0x1000, at(1, 2)
    await h.run(c1, CF_CALL, 1, fn)
    await h.run(br, CF_BRANCH, 1, br + 0x40)
    first = None
    for _ in range(10):
        p = await h.look(1, c1)
        await h.commit(fire=0)
        first = first or p
        assert p == first
    for _ in range(5):
        p = await h.look(0, br)
        await h.commit(fire=1)            # fire without valid has no effect
        assert p.meta == first.meta


# --------------------------------------------------------------------------------- random test

@dataclass
class Slot:
    kind: int
    target: int = 0
    targets: tuple[int, ...] = ()
    behav: str = "bias"
    p: float = 0.5
    pattern: tuple[int, ...] = ()
    trip: int = 1
    count: int = 0


class RandomProgram:
    """A random static program with ground-truth control flow, executed in program order."""

    def __init__(self, rng: random.Random, n: int):
        self.rng = rng
        # Two regions whose PCs share BTB and table indices but not tags.
        alias = max(4 * CFG.btb_entries, 4 * CFG.bht_entries) * 4
        self.addrs = [BASE + 4 * i for i in range(n)] + [BASE + alias + 4 * i for i in range(n)]
        self.slots = {a: self._rand_slot() for a in self.addrs}
        for region_end in (self.addrs[n - 1], self.addrs[-1]):
            self.slots[region_end] = Slot(CF_JAL, target=BASE)
        self.stack: list[int] = []
        self.pc = BASE

    def _rand_slot(self) -> Slot:
        r = self.rng
        k = r.choices([CF_NONE, CF_BRANCH, CF_JAL, CF_JALR, CF_CALL, CF_RET],
                      [50, 25, 5, 5, 8, 7])[0]
        tgt = r.choice(self.addrs)
        if k == CF_BRANCH:
            behav = r.choice(["bias", "bias", "pattern", "loop", "random"])
            return Slot(k, target=tgt, behav=behav, p=r.choice([0.05, 0.3, 0.7, 0.95]),
                        pattern=tuple(r.randint(0, 1) for _ in range(r.randint(2, 5))),
                        trip=r.randint(2, 9))
        if k == CF_JALR:
            # "cycle" goes through its targets in order (learnable by the indirect table),
            # "random" picks one at random every time.
            return Slot(k, targets=tuple(r.choice(self.addrs) for _ in range(r.randint(1, 4))),
                        behav=r.choice(["cycle", "random"]))
        return Slot(k, target=tgt)

    def mutate(self) -> None:
        a = self.rng.choice(self.addrs)
        if self.slots[a].kind != CF_JAL or self.slots[a].target != BASE:
            self.slots[a] = self._rand_slot()

    def execute(self) -> tuple[int, int, int, int, int]:
        """Run the instruction at self.pc. Returns (pc, kind, taken, target, next_pc)."""
        pc, s = self.pc, self.slots.get(self.pc)
        if s is None:
            s = Slot(CF_NONE)
        k, taken, target = s.kind, 0, (pc + 4) & 0xFFFF_FFFF
        if k == CF_BRANCH:
            if s.behav == "bias":
                taken = int(self.rng.random() < s.p)
            elif s.behav == "pattern":
                taken = s.pattern[s.count % len(s.pattern)]
            elif s.behav == "loop":
                taken = int(s.count % s.trip != s.trip - 1)
            else:
                taken = self.rng.randint(0, 1)
            s.count += 1
            target = s.target
        elif k in (CF_JAL, CF_CALL):
            taken, target = 1, s.target
        elif k == CF_JALR:
            taken = 1
            if s.behav == "cycle":
                target = s.targets[s.count % len(s.targets)]
                s.count += 1
            else:
                target = self.rng.choice(s.targets)
        elif k == CF_RET:
            taken = 1
            target = self.stack.pop() if self.stack else self.rng.choice(self.addrs)
        if k == CF_CALL:
            self.stack.append((pc + 4) & 0xFFFF_FFFF)
            del self.stack[:-64]
        nxt = target if taken else (pc + 4) & 0xFFFF_FFFF
        self.pc = nxt
        return pc, k, taken, target, nxt


@cocotb.test()
async def test_random_pipeline(dut):
    """Long random stream in pipeline order: fetch runs ahead with 0-4 predictions in flight,
    updates come in program order, mispredictions squash and redirect, plus random stalls,
    random flushes and code that changes under the predictor."""
    h = await setup(dut)
    rng = random.Random(SEED * 1_000_003 + zlib.crc32(CFG.short().encode()))
    prog = RandomProgram(rng, n=rng.choice([24, 48, 96]))
    inflight: deque[tuple[int, Prediction]] = deque()
    fetch_pc = BASE
    max_inflight = 4
    st = dict(fires=0, updates=0, mispredicts=0, flushes=0, repair_fires=0, kind_none=0,
              max_q=0, cf=0)
    for _ in range(RANDOM_CYCLES):
        valid = int(rng.random() > 0.05)
        pred = await h.look(valid, fetch_pc)
        upd, flush, redirect = None, None, None
        if inflight and rng.random() < 0.55:
            head_pc, head_pred = inflight.popleft()
            assert head_pc == prog.pc, "testbench bug: head is not on the correct path"
            slot = prog.slots.get(head_pc)
            if slot is not None and slot.kind == CF_NONE and rng.random() < 0.03:
                # FENCE.I (refetch pc+4) or a trap-like jump anywhere; the jump also stops the
                # stream from settling into one easy loop for the whole run.
                prog.execute()
                if rng.random() < 0.5:
                    prog.pc = rng.choice(prog.addrs)
                flush = head_pred.meta
                redirect = prog.pc
                st["flushes"] += 1
            else:
                pc, kind, taken, target, nxt = prog.execute()
                mis = int(head_pred.target != nxt)
                if kind != CF_NONE or mis:
                    upd = Update(pc, kind, taken, target, mis, head_pred.meta)
                    st["updates"] += 1
                    st["cf"] += kind != CF_NONE
                    st["kind_none"] += kind == CF_NONE
                if mis:
                    redirect = nxt
                    st["mispredicts"] += 1
            if redirect is not None:
                inflight.clear()
            if rng.random() < 0.005:
                prog.mutate()
        fire = int(valid and len(inflight) < max_inflight and rng.random() < 0.85)
        await h.commit(fire=fire, upd=upd, flush_meta=flush)
        if redirect is not None:
            st["repair_fires"] += fire
            fetch_pc = redirect
        elif fire and valid:
            inflight.append((fetch_pc, pred))
            st["fires"] += 1
            fetch_pc = pred.target
        st["max_q"] = max(st["max_q"], len(inflight))
    dut._log.info(f"{CFG.short()} random stream: {RANDOM_CYCLES} cycles, {st}")
    assert st["mispredicts"] > 0 and st["updates"] > 0 and st["flushes"] > 0
    assert st["max_q"] == max_inflight
