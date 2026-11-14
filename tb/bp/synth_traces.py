"""Synthetic branch traces for the accuracy study (tb/bp/trace_sim.py).

    .venv/bin/python tb/bp/synth_traces.py --out build/bp/traces [--scale 1.0]

Each workload is a small abstract program written as Python control flow; running it emits one
line per control-flow instruction in the trace format of docs/BRANCH_PREDICTOR.md:

    <pc 8 hex> <kind 1 digit> <taken 0/1> <target 8 hex>

kind: 1 branch, 2 jal, 3 jalr, 4 call, 5 ret (same encoding as upd_kind_i). For a branch the
target is the branch target even when not taken. Every static PC keeps one kind and (except
jalr/ret) one target, as in real code. Fixed seeds, so the traces are reproducible.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

BR, JAL, JALR, CALL, RET = 1, 2, 3, 4, 5


class Tracer:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.lines: list[str] = []
        self.stack: list[int] = []

    def _emit(self, pc: int, kind: int, taken: int, target: int) -> None:
        self.lines.append(f"{pc:08x} {kind} {taken} {target:08x}")

    def br(self, pc: int, taken: bool, target: int) -> bool:
        self._emit(pc, BR, int(taken), target)
        return taken

    def jal(self, pc: int, target: int) -> None:
        self._emit(pc, JAL, 1, target)

    def jalr(self, pc: int, target: int) -> None:
        self._emit(pc, JALR, 1, target)

    def call(self, pc: int, target: int) -> None:
        self._emit(pc, CALL, 1, target)
        self.stack.append(pc + 4)

    def ret(self, pc: int) -> None:
        self._emit(pc, RET, 1, self.stack.pop())


F = 0x8000_0000  # code base


def w_loops(t: Tracer, n: int) -> None:
    """Nested loops: inner trip count 7, an if on the inner index parity, outer loop branch."""
    while len(t.lines) < n:
        for i in range(7):
            t.br(F + 0x10, i % 2 == 1, F + 0x20)           # if (i & 1)
            t.br(F + 0x28, i < 6, F + 0x08)                # inner loop back edge
        t.br(F + 0x30, True, F + 0x00)                     # outer loop back edge


def w_alternating(t: Tracer, n: int) -> None:
    """A branch that alternates T,N,T,N inside a loop (e.g. processing pairs)."""
    i = 0
    while len(t.lines) < n:
        t.br(F + 0x40, i % 2 == 0, F + 0x50)
        t.br(F + 0x58, True, F + 0x38)
        i += 1


def w_correlated(t: Tracer, n: int) -> None:
    """if (x) ... ; if (y) ... ; if (x) ...: the third branch repeats the first, only history
    can know it. x and y are random, so the first two are unpredictable."""
    while len(t.lines) < n:
        x = t.rng.random() < 0.5
        y = t.rng.random() < 0.5
        t.br(F + 0x100, x, F + 0x10c)
        t.br(F + 0x110, y, F + 0x11c)
        t.br(F + 0x120, x, F + 0x12c)
        t.br(F + 0x130, True, F + 0x0fc)


def w_random(t: Tracer, n: int) -> None:
    """Data-dependent branch on random data: nobody can predict it (about 50% for all)."""
    while len(t.lines) < n:
        t.br(F + 0x200, t.rng.random() < 0.5, F + 0x20c)
        t.br(F + 0x210, True, F + 0x1fc)


def w_biased(t: Tracer, n: int) -> None:
    """32 branches with fixed random biases (error checks, rare cases): bimodal territory."""
    bias = [t.rng.choice([0.02, 0.1, 0.9, 0.98]) for _ in range(32)]
    while len(t.lines) < n:
        for k in range(32):
            t.br(F + 0x400 + 8 * k, t.rng.random() < bias[k], F + 0x404 + 8 * k + 8)
        t.br(F + 0x500, True, F + 0x3fc)


def w_calls(t: Tracer, n: int) -> None:
    """One helper called from 4 sites in turn; the helper has a short loop. The return address
    changes every call, so the BTB alone mispredicts every return and the RAS gets all."""
    fn = F + 0x2000
    sites = [F + 0x600 + 0x40 * k for k in range(4)]
    while len(t.lines) < n:
        for s in sites:
            t.call(s, fn)
            for i in range(3):
                t.br(fn + 0x10, i < 2, fn + 0x04)
            t.ret(fn + 0x20)
        t.jal(F + 0x704, F + 0x5fc)


def _fib(t: Tracer, k: int) -> None:
    fn = F + 0x3000
    if t.br(fn + 0x08, k < 2, fn + 0x40):                  # base case
        t.ret(fn + 0x44)
        return
    t.call(fn + 0x10, fn)
    _fib(t, k - 1)
    t.call(fn + 0x18, fn)
    _fib(t, k - 2)
    t.ret(fn + 0x30)


def w_recursion(t: Tracer, n: int) -> None:
    """Recursive fib(10): call depth up to 10, deeper than a 4- or 8-entry RAS."""
    while len(t.lines) < n:
        t.call(F + 0x800, F + 0x3000)
        _fib(t, 10)
        t.jal(F + 0x804, F + 0x7fc)


def w_many(t: Tracer, n: int) -> None:
    """2048 static branches, one every 5 instructions, visited in order (big code): far more
    than the BTB and the counter tables hold, so capacity and aliasing dominate."""
    bias = [t.rng.choice([0.05, 0.95]) for _ in range(2048)]
    while len(t.lines) < n:
        for k in range(2048):
            pc = F + 0x10000 + 20 * k
            t.br(pc, t.rng.random() < bias[k], pc + 12)
        t.jal(F + 0x1a000, F + 0x0fffc)


def w_interp(t: Tracer, n: int) -> None:
    """Bytecode interpreter: an indirect jump (jalr) through a dispatch table to one of 8
    handlers, opcode sequence with a repeating structure plus noise."""
    # Handlers 0x104 apart: with a power-of-two spacing all of them would land on the same
    # BTB and counter index, which measures aliasing instead of indirect jumps.
    handlers = [F + 0x4000 + 0x104 * k for k in range(8)]
    prog = [t.rng.randrange(8) for _ in range(24)]
    while len(t.lines) < n:
        for op in prog:
            if t.rng.random() < 0.1:
                op = t.rng.randrange(8)
            t.jalr(F + 0x900, handlers[op])
            h = handlers[op]
            t.br(h + 0x10, op % 3 == 0, h + 0x20)
            t.jal(h + 0x30, F + 0x8f0)
        t.br(F + 0x910, True, F + 0x8e0)


def w_mix(t: Tracer, n: int) -> None:
    """A main loop that calls small kernels: loops, a correlated pair, a biased check, a helper
    from two sites. Closest to a small real program."""
    i = 0
    kfn = F + 0x5000
    hfn = F + 0x5800
    while len(t.lines) < n:
        t.call(F + 0xa00, kfn)                             # kernel: inner loop of 5
        for j in range(5):
            t.br(kfn + 0x10, (i + j) % 3 == 0, kfn + 0x18)
            t.br(kfn + 0x20, j < 4, kfn + 0x04)
        t.ret(kfn + 0x28)
        x = t.rng.random() < 0.3
        t.br(F + 0xa10, x, F + 0xa20)
        t.call(F + 0xa24, hfn)
        t.br(hfn + 0x08, t.rng.random() < 0.95, hfn + 0x10)
        t.ret(hfn + 0x14)
        t.br(F + 0xa30, x, F + 0xa40)                      # same condition as 0xa10
        t.call(F + 0xa44, hfn)
        t.br(hfn + 0x08, t.rng.random() < 0.95, hfn + 0x10)
        t.ret(hfn + 0x14)
        t.br(F + 0xa50, True, F + 0x9fc)
        i += 1


WORKLOADS = {
    "loops": w_loops, "alternating": w_alternating, "correlated": w_correlated,
    "random": w_random, "biased": w_biased, "calls": w_calls, "recursion": w_recursion,
    "many_branches": w_many, "interpreter": w_interp, "mix": w_mix,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--records", type=int, default=30000, help="control-flow records per trace")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for seed, (name, fn) in enumerate(WORKLOADS.items(), start=1):
        t = Tracer(seed)
        fn(t, a.records)
        doc = (fn.__doc__ or "").split("\n")[0]
        (a.out / f"{name}.trace").write_text(f"# {name}: {doc}\n" + "\n".join(t.lines) + "\n")
        print(f"wrote {a.out / (name + '.trace')} ({len(t.lines)} records)")


if __name__ == "__main__":
    main()
