#!/usr/bin/env python3
"""Constrained-random RV32I program generator for lockstep RTL-vs-ISS comparison.

  .venv/bin/python scripts/rand_prog.py --seed 7 -o build/sw/random/rand_7.S [--budget 40000]

The programs are not self-checking: every retired instruction is compared with the
ISS through the commit trace, so the generator only has to produce legal,
deterministic, terminating code that stresses the pipeline and the predictor:

  - dependency chains (EX/MEM and MEM/WB forwarding), writes to x0 then reads of x0;
  - load-use into ALU ops, address generation (pointer chasing), branches and JALR; all load/store
    widths at random aligned addresses; store followed by an overlapping load;
  - every branch type with data-dependent outcomes, branch-to-next, branch chains,
    if/else, loops with random trip counts (up to three nested), early loop exits;
  - calls and returns at random depth through ra and t0, leaf and non-leaf
    functions, bounded recursion deeper than the RAS, indirect calls, jump tables
    with JALR offsets (including the bit-0 clear), unreachable code with traps in
    it after unconditional jumps (fetched on the wrong path, must never commit);
  - FENCE, FENCE.I, self-modifying code (store + FENCE.I + execute), counter reads.

Invariants that keep the program legal (they are what makes it safe to compare):
  - registers x1 (ra), x2 (sp), x5 (t0), x8 (s0 = middle of the data area) and
    x18..x21 (loop counters, one per nesting level) are never written by random
    instructions; functions that loop or call save them on the stack;
  - every backward branch closes a counted loop whose counter only the loop touches;
  - every load/store address is s0 + offset with offset forced into [-2048, 2047]
    and aligned to the access size, by construction (andi mask + aligned immediate);
  - counter CSR values differ between RTL and ISS, so the destination is zeroed by
    the very next instruction and never used otherwise;
  - the program ends with a word store of 1 to TOHOST.
"""

import argparse
import os
import random
import sys

RA, SP, T0, S0 = 1, 2, 5, 8
COUNTERS = [18, 19, 20, 21]
FREE = [3, 4, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]
READ_ONLY = [RA, SP, T0, S0] + COUNTERS

R_OPS = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"]
I_OPS = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
SH_OPS = ["slli", "srli", "srai"]
BR_OPS = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]
LOADS = [("lb", 1), ("lbu", 1), ("lh", 2), ("lhu", 2), ("lw", 4)]
STORES = [("sb", 1), ("sh", 2), ("sw", 4)]
EDGE_IMM = [0, 1, -1, 2, 0x7FF, -0x800, 0x400, -0x400, 0x555, -0x556, 31, 32, 0xFF, -0x100]
EDGE_VAL = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0x80000001, 0xFFFF, 0xFFFF0000, 0x55555555,
            0xAAAAAAAA, 2, 0xFFFFFFFE]

DATA_BYTES = 4096  # s0 points to the middle, so every 12-bit offset is in range
PTRS = 32          # pointer table: 2*PTRS entries, each pointing to one of the first PTRS
STACK_BYTES = 4096


def x(r):
    return f"x{r}"


def enc_addi(rd, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


class Gen:
    def __init__(self, seed, budget, blocks, wrong_path_traps=True):
        self.r = random.Random(seed)
        self.seed = seed
        self.budget = budget
        self.blocks = blocks
        self.wrong_path_traps = wrong_path_traps
        self.nlabel = 0
        self.recent = []
        self.data = []      # lines for .data (jump tables)
        self.funcs = []     # dicts: name, link, cost, leaf
        self.rec = None     # recursive function: name, cost per level
        # Set while generating a leaf function or rec(): nested bodies inherit them.
        self.allow_calls = True
        self.allow_loops = True

    # ------------------------------------------------------------- helpers
    def label(self, stem="L"):
        self.nlabel += 1
        return f"{stem}{self.nlabel}"

    def chance(self, p):
        return self.r.random() < p

    def dst(self, allow_x0=True):
        if allow_x0 and self.chance(0.03):
            return 0
        rd = self.r.choice(FREE)
        self.recent.append(rd)
        if len(self.recent) > 6:
            self.recent.pop(0)
        return rd

    def src(self):
        p = self.r.random()
        if p < 0.55 and self.recent:
            return self.r.choice(self.recent[-3:])
        if p < 0.62:
            return 0
        if p < 0.70:
            return self.r.choice(READ_ONLY)
        return self.r.choice(FREE)

    def imm12(self):
        if self.chance(0.4):
            return self.r.choice(EDGE_IMM)
        return self.r.randint(-2048, 2047)

    def free_tmp(self, avoid=()):
        return self.r.choice([r for r in FREE if r not in avoid])

    # ------------------------------------------------------------- straight-line pieces
    def alu(self, out, rd=None, rs1=None):
        """One random ALU instruction. Returns cost (1, or 2 for li-like pairs)."""
        k = self.r.random()
        a = self.src() if rs1 is None else rs1
        if k < 0.40:
            op = self.r.choice(R_OPS)
            b = self.src()
            d = self.dst() if rd is None else rd
            out.append(f"  {op} {x(d)}, {x(a)}, {x(b)}")
        elif k < 0.75:
            op = self.r.choice(I_OPS)
            d = self.dst() if rd is None else rd
            out.append(f"  {op} {x(d)}, {x(a)}, {self.imm12()}")
        elif k < 0.90:
            op = self.r.choice(SH_OPS)
            sh = self.r.choice([0, 1, 31, 16]) if self.chance(0.3) else self.r.randint(0, 31)
            d = self.dst() if rd is None else rd
            out.append(f"  {op} {x(d)}, {x(a)}, {sh}")
        elif k < 0.96:
            d = self.dst() if rd is None else rd
            out.append(f"  lui {x(d)}, {self.r.randint(0, 0xFFFFF):#x}")
        else:
            d = self.dst() if rd is None else rd
            out.append(f"  auipc {x(d)}, {self.r.randint(0, 0xFFFFF):#x}")
        return 1

    def alu_seq(self, out, n=None):
        n = n or self.r.randint(2, 8)
        for _ in range(n):
            self.alu(out)
        return n

    def chain(self, out):
        """Each instruction consumes the previous result: back-to-back forwarding."""
        n = self.r.randint(3, 8)
        prev = self.src()
        for _ in range(n):
            d = self.dst(allow_x0=False)
            if self.chance(0.5):
                out.append(f"  {self.r.choice(R_OPS)} {x(d)}, {x(prev)}, {x(prev if self.chance(0.5) else self.src())}")
            else:
                out.append(f"  {self.r.choice(I_OPS)} {x(d)}, {x(prev)}, {self.imm12()}")
            prev = d
        return n

    def x0_use(self, out):
        """Write x0, then read it at once: forwarding must never forward x0."""
        out.append(f"  {self.r.choice(R_OPS)} x0, {x(self.src())}, {x(self.src())}")
        d = self.dst(allow_x0=False)
        out.append(f"  add {x(d)}, x0, {x(self.src())}")
        return 2

    def addr(self, out, size):
        """Emit address computation; return the 'off(base)' operand string and cost."""
        if self.chance(0.4):
            off = self.r.randrange(-2048, 2048 - size + 1, size)
            off -= off % size
            return f"{off}({x(S0)})", 0
        mask = 0x7FF & ~(size - 1)
        t = self.dst(allow_x0=False)
        out.append(f"  andi {x(t)}, {x(self.src())}, {mask:#x}")
        out.append(f"  add {x(t)}, {x(t)}, {x(S0)}")
        off = -self.r.randrange(0, 2049, size)
        return f"{off}({x(t)})", 2

    def mem(self, out):
        cost = 0
        for _ in range(self.r.randint(2, 6)):
            if self.chance(0.55):
                op, size = self.r.choice(LOADS)
                operand, c = self.addr(out, size)
                d = self.dst()
                out.append(f"  {op} {x(d)}, {operand}")
                cost += c + 1
                if self.chance(0.4) and d:   # load-use
                    self.alu(out, rs1=d)
                    cost += 1
            else:
                op, size = self.r.choice(STORES)
                operand, c = self.addr(out, size)
                out.append(f"  {op} {x(self.src())}, {operand}")
                cost += c + 1
        return cost

    def st_ld(self, out):
        """Store then an overlapping load of a random width: memory read-after-write."""
        sop, ssize = self.r.choice(STORES)
        lop, lsize = self.r.choice(LOADS)
        big = max(ssize, lsize)
        base = self.r.randrange(-2048, 2048 - 4 + 1, 4)
        soff = base + self.r.randrange(0, 4, ssize)
        loff = base + self.r.randrange(0, 4, lsize)
        del big
        out.append(f"  {sop} {x(self.src())}, {soff}({x(S0)})")
        d = self.dst(allow_x0=False)
        out.append(f"  {lop} {x(d)}, {loff}({x(S0)})")
        n = 2
        if self.chance(0.5):
            out.append(f"  {self.r.choice(R_OPS)} {x(self.dst())}, {x(d)}, {x(d)}")
            n += 1
        return n

    def load_use_addr(self, out):
        """Pointer-chasing shape: a loaded value becomes the next address."""
        operand, c = self.addr(out, 4)
        d = self.dst(allow_x0=False)
        out.append(f"  lw {x(d)}, {operand}")
        t = self.dst(allow_x0=False)
        out.append(f"  andi {x(t)}, {x(d)}, 0x7fc")
        out.append(f"  add {x(t)}, {x(t)}, {x(S0)}")
        d2 = self.dst()
        op, size = self.r.choice(LOADS)
        out.append(f"  {op} {x(d2)}, {-self.r.randrange(0, 2049, 4)}({x(t)})")
        return c + 4

    def pointer_chase(self, out):
        """lw t, k(t) chains through a table of pointers: load-use straight into the
        address of the next load (distance 1), or with one instruction between."""
        t = self.dst(allow_x0=False)
        out.append(f"  la {x(t)}, ptrs")
        n = self.r.randint(2, 6)
        for _ in range(n):
            out.append(f"  lw {x(t)}, {4 * self.r.randrange(PTRS)}({x(t)})")
            if self.chance(0.3):
                self.alu(out, rd=self.free_tmp(avoid=(t,)))  # t must stay a pointer
        return 2 + n * 2

    def csr_read(self, out):
        csr = self.r.choice(["cycle", "time", "instret", "cycleh", "timeh", "instreth"])
        if self.chance(0.15):
            d = self.dst(allow_x0=False)
            out.append(f"  csrr {x(d)}, mhartid")   # reads 0 on both sides: usable
            return 1
        if self.chance(0.1):
            out.append(f"  csrr x0, {csr}")
            return 1
        d = self.r.choice(FREE)                       # not added to recent: value differs
        form = self.r.random()
        if form < 0.6:
            out.append(f"  csrr {x(d)}, {csr}")
        elif form < 0.73:
            out.append(f"  csrrsi {x(d)}, {csr}, 0")
        elif form < 0.86:
            out.append(f"  csrrci {x(d)}, {csr}, 0")
        else:
            out.append(f"  csrrc {x(d)}, {csr}, x0")
        kill = self.r.choice([f"andi {x(d)}, {x(d)}, 0", f"sub {x(d)}, {x(d)}, {x(d)}",
                              f"xor {x(d)}, {x(d)}, {x(d)}", f"and {x(d)}, {x(d)}, x0",
                              f"sltu {x(d)}, {x(d)}, x0"])
        out.append(f"  {kill}")
        return 2

    def fence(self, out):
        out.append("  " + self.r.choice(["fence", "fence rw, rw", "fence.i", "fence.i", "fence r, r"]))
        return 1

    def smc(self, out):
        """Patch an instruction ahead of us, FENCE.I, then execute the patched copy."""
        slot = self.label("smc")
        d = self.dst(allow_x0=False)
        s = self.r.choice(FREE)
        new_imm = self.r.randint(-2048, 2047)
        ta = self.free_tmp(avoid=(d,))
        tb = self.free_tmp(avoid=(d, ta))
        out.append(f"  la {x(ta)}, {slot}")
        out.append(f"  li {x(tb)}, {enc_addi(d, s, new_imm):#010x}")
        out.append(f"  sw {x(tb)}, 0({x(ta)})")
        if self.chance(0.5):
            self.alu(out)
        out.append("  fence.i")
        out.append(f"{slot}:")
        out.append(f"  addi {x(d)}, {x(s)}, {self.r.randint(-2048, 2047)}")
        return 8

    # ------------------------------------------------------------- control flow
    def cond(self, out, target):
        """A conditional branch to target with a data-dependent (or fixed) outcome."""
        k = self.r.random()
        op = self.r.choice(BR_OPS)
        if k < 0.45:
            a, b = self.src(), self.src()
            out.append(f"  {op} {x(a)}, {x(b)}, {target}")
            return 1
        if k < 0.80:
            t = self.dst(allow_x0=False)
            bit = self.r.randint(0, 31)
            out.append(f"  andi {x(t)}, {x(self.src())}, {1 << bit if bit < 11 else 1}")
            out.append(f"  {self.r.choice(['beqz', 'bnez'])} {x(t)}, {target}")
            return 2
        if k < 0.9:
            a = self.src()
            out.append(f"  {op} {x(a)}, {x(a)}, {target}")
            return 1
        a = self.src()
        out.append(f"  {op} {x(a)}, x0, {target}")
        return 1

    def if_then(self, out, depth, budget, brk):
        end = self.label()
        c = self.cond(out, end)
        body = []
        cb = self.body(body, depth, max(1, budget // 2), self.r.randint(1, 3), brk)
        out += body
        out.append(f"{end}:")
        return c + cb

    def if_else(self, out, depth, budget, brk):
        els, end = self.label(), self.label()
        c = self.cond(out, els)
        a, b = [], []
        ca = self.body(a, depth, max(1, budget // 2), self.r.randint(1, 2), brk)
        cb = self.body(b, depth, max(1, budget // 2), self.r.randint(1, 2), brk)
        out += a
        out.append(f"  j {end}")
        out.append(f"{els}:")
        out += b
        out.append(f"{end}:")
        return c + 1 + max(ca, cb)

    def branch_chain(self, out):
        """Back-to-back branches, including branch-to-next and branches onto branches."""
        n = self.r.randint(2, 4)
        labels = [self.label() for _ in range(n)]
        cost = 0
        for i in range(n):
            cost += self.cond(out, labels[self.r.randint(i, n - 1)])
            out.append(f"{labels[i]}:")
            if self.chance(0.3):
                cost += self.alu(out)
        return cost

    def dead_code(self, out):
        """Unconditional jump over code that must never commit (fetched on the wrong path)."""
        end = self.label()
        out.append(f"  j {end}")
        for _ in range(self.r.randint(1, 4)):
            k = self.r.random()
            if self.wrong_path_traps and k < 0.35:
                out.append("  " + self.r.choice(["ecall", "ebreak", ".word 0", "unimp", ".word 0xffffffff"]))
            elif k < 0.6:
                out.append(f"  sw {x(self.r.choice(FREE))}, 0({x(S0)})")
            else:
                self.alu(out)
        out.append(f"{end}:")
        return 1

    def loop(self, out, depth, budget, _brk):
        c = COUNTERS[depth]
        top, end = self.label("loop"), self.label("endloop")
        small = self.chance(0.3)
        trips = self.r.randint(1, 40 if small else 10)
        body_budget = max(4, budget // (trips + 1))
        nblk = 1 if small else self.r.randint(1, 4)
        out.append(f"  li {x(c)}, {trips}")
        body = []
        cb = self.body(body, depth + 1, body_budget, nblk, end)
        if self.chance(0.75):  # do-while, closing branch at the bottom
            out.append(f"{top}:")
            out += body
            out.append(f"  addi {x(c)}, {x(c)}, -1")
            close = self.r.choice([f"bnez {x(c)}, {top}", f"bgtz {x(c)}, {top}",
                                   f"bltu x0, {x(c)}, {top}", f"bne {x(c)}, x0, {top}"])
            out.append(f"  {close}")
            out.append(f"{end}:")
            return 1 + trips * (cb + 2)
        out.append(f"{top}:")
        out.append(f"  {self.r.choice(['beqz', 'blez'])} {x(c)}, {end}")
        out += body
        out.append(f"  addi {x(c)}, {x(c)}, -1")
        out.append(f"  j {top}")
        out.append(f"{end}:")
        return 2 + trips * (cb + 3)

    def call(self, out, budget):
        fs = [f for f in self.funcs if f["cost"] + 4 <= budget]
        if not fs:
            return None
        f = self.r.choice(fs)
        link = f["link"]
        k = self.r.random()
        if k < 0.6:
            out.append(f"  jal {x(link)}, {f['name']}")
            return f["cost"] + 1
        if k < 0.8:
            t = self.free_tmp()
            off = self.r.choice([0, 4, -8, 12, 1])
            out.append(f"  la {x(t)}, {f['name']}{-off if off and off != 1 else 0:+d}")
            out.append(f"  jalr {x(link)}, {off}({x(t)})")
            return f["cost"] + 3
        # Call through the link register itself: rs1 must be read before rd is written.
        out.append(f"  la {x(link)}, {f['name']}")
        out.append(f"  jalr {x(link)}, 0({x(link)})")
        return f["cost"] + 3

    def call_table(self, out, budget):
        fs = [f for f in self.funcs if f["link"] == RA and f["cost"] + 8 <= budget]
        if len(fs) < 2:
            return None
        n = 4 if len(fs) >= 4 and self.chance(0.5) else 2
        pick = [self.r.choice(fs) for _ in range(n)]
        tab = self.label("ftab")
        self.data.append(f"  .balign 4\n{tab}:\n" + "\n".join(f"  .word {f['name']}" for f in pick))
        ti, tt = self.free_tmp(), None
        tt = self.free_tmp(avoid=(ti,))
        out.append(f"  andi {x(ti)}, {x(self.src())}, {n - 1}")
        out.append(f"  slli {x(ti)}, {x(ti)}, 2")
        out.append(f"  la {x(tt)}, {tab}")
        out.append(f"  add {x(tt)}, {x(tt)}, {x(ti)}")
        out.append(f"  lw {x(tt)}, 0({x(tt)})")
        out.append(f"  jalr ra, 0({x(tt)})")
        return 7 + max(f["cost"] for f in pick)

    def call_rec(self, out, budget):
        if self.rec is None:
            return None
        depth = self.r.randint(1, 12)
        cost = depth * self.rec["cost"] + self.rec["cost"] + 6
        if cost > budget:
            return None
        c = COUNTERS[0]
        out.append(f"  addi sp, sp, -16")
        out.append(f"  sw {x(c)}, 12(sp)")
        out.append(f"  li {x(c)}, {depth}")
        out.append(f"  jal ra, {self.rec['name']}")
        out.append(f"  lw {x(c)}, 12(sp)")
        out.append(f"  addi sp, sp, 16")
        return cost

    def switch(self, out, depth, budget, brk):
        n = self.r.choice([2, 4, 4, 8])
        tab, end = self.label("jtab"), self.label()
        cases = [self.label("case") for _ in range(n)]
        off = self.r.choice([0, 0, 4, -8, 12, 1])
        ent = [f"{c}{-off:+d}" if off not in (0, 1) else c for c in cases]
        self.data.append(f"  .balign 4\n{tab}:\n" + "\n".join(f"  .word {e}" for e in ent))
        ti = self.free_tmp()
        tt = self.free_tmp(avoid=(ti,))
        out.append(f"  andi {x(ti)}, {x(self.src())}, {n - 1}")
        out.append(f"  slli {x(ti)}, {x(ti)}, 2")
        out.append(f"  la {x(tt)}, {tab}")
        out.append(f"  add {x(tt)}, {x(tt)}, {x(ti)}")
        out.append(f"  lw {x(tt)}, 0({x(tt)})")
        out.append(f"  jalr x0, {off}({x(tt)})")
        worst = 0
        for i, c in enumerate(cases):
            out.append(f"{c}:")
            body = []
            worst = max(worst, self.body(body, depth, max(1, budget // n), 1, brk))
            out += body
            if i != n - 1 or self.chance(0.5):
                out.append(f"  j {end}")
        out.append(f"{end}:")
        return 8 + worst

    # ------------------------------------------------------------- bodies
    def body(self, out, depth, budget, nblocks, brk=None):
        """Emit nblocks random blocks; return the worst-case dynamic cost."""
        allow_calls, allow_loops = self.allow_calls, self.allow_loops
        cost = 0
        for _ in range(nblocks):
            left = budget - cost
            choices = [
                (14, lambda: self.alu_seq(out)),
                (6, lambda: self.chain(out)),
                (10, lambda: self.mem(out)),
                (4, lambda: self.st_ld(out)),
                (3, lambda: self.load_use_addr(out)),
                (2, lambda: self.pointer_chase(out)),
                (2, lambda: self.x0_use(out)),
                (2, lambda: self.csr_read(out)),
                (2, lambda: self.fence(out)),
                (1, lambda: self.smc(out)),
                (8, lambda: self.if_then(out, depth, left, brk)),
                (5, lambda: self.if_else(out, depth, left, brk)),
                (4, lambda: self.branch_chain(out)),
                (2, lambda: self.dead_code(out)),
            ]
            if left > 40:
                choices.append((3, lambda: self.switch(out, depth, left, brk)))
            if allow_loops and depth < 3 and left > 30:
                choices.append((9, lambda: self.loop(out, depth, left, brk)))
            if allow_calls:
                choices += [(6, lambda: self.call(out, left)), (2, lambda: self.call_table(out, left)),
                            (2, lambda: self.call_rec(out, left))]
            if brk is not None:
                choices.append((2, lambda: self.cond(out, brk)))  # early loop exit
            total = sum(w for w, _ in choices)
            while True:
                pick = self.r.uniform(0, total)
                for w, fn in choices:
                    pick -= w
                    if pick <= 0:
                        break
                c = fn()
                if c is not None:
                    break
            cost += c
            if cost >= budget:
                break
        return cost

    def function(self, idx):
        name = f"func{idx}"
        link = RA if self.chance(0.7) else T0
        body = []
        budget = self.r.choice([20, 60, 150, 400, 900])
        leaf = self.chance(0.4)
        self.allow_calls = self.allow_loops = not leaf
        cost = self.body(body, 0, budget, self.r.randint(1, 5))
        self.allow_calls = self.allow_loops = True
        out = [f"  .balign 4", f"{name}:"]
        if leaf:
            out += body
            out.append(f"  jalr x0, 0({x(link)})")
            self.funcs.append(dict(name=name, link=link, cost=cost + 1))
            return out
        saved = [RA, T0] + COUNTERS
        out.append("  addi sp, sp, -32")
        for i, r in enumerate(saved):
            out.append(f"  sw {x(r)}, {28 - 4 * i}(sp)")
        out += body
        for i, r in enumerate(saved):
            if r != link:
                out.append(f"  lw {x(r)}, {28 - 4 * i}(sp)")
        li = saved.index(link)
        if self.chance(0.5):  # load-use straight into the return
            out.append("  addi sp, sp, 32")
            out.append(f"  lw {x(link)}, {28 - 4 * li - 32}(sp)")
        else:
            out.append(f"  lw {x(link)}, {28 - 4 * li}(sp)")
            out.append("  addi sp, sp, 32")
        out.append(f"  jalr x0, 0({x(link)})")
        self.funcs.append(dict(name=name, link=link, cost=cost + 16))
        return out

    def rec_function(self):
        """rec(x18): recursion of depth x18, no loops (x18 is its argument)."""
        name = "rec"
        c = COUNTERS[0]
        pre, post = [], []
        self.allow_calls = self.allow_loops = False
        cp = self.body(pre, 0, 12, self.r.randint(1, 2))
        cq = self.body(post, 0, 12, self.r.randint(1, 2))
        self.allow_calls = self.allow_loops = True
        done = self.label("recdone")
        out = ["  .balign 4", f"{name}:", "  addi sp, sp, -16", "  sw ra, 12(sp)",
               f"  sw {x(c)}, 8(sp)"] + pre + [
            f"  beqz {x(c)}, {done}", f"  addi {x(c)}, {x(c)}, -1", f"  jal ra, {name}",
            f"{done}:"] + post + [f"  lw {x(c)}, 8(sp)", "  lw ra, 12(sp)", "  addi sp, sp, 16",
                                  "  ret"]
        self.rec = dict(name=name, cost=cp + cq + 10)
        return out

    def program(self):
        r = self.r
        text = []
        # Functions first, highest index first, so funcN only calls funcM with M > N
        # (a DAG: bounded depth, the only recursion is rec()).
        nfun = r.randint(2, 8)
        fun_text = [self.rec_function()]
        for i in reversed(range(nfun)):
            fun_text.append(self.function(i))
        self.recent = []

        text += [f"# Generated by scripts/rand_prog.py --seed {self.seed} --budget {self.budget}"
                 f" --blocks {self.blocks}",
                 "  .option norelax",
                 "  .option arch, +zicsr, +zifencei",
                 "  .section .text.init, \"ax\"",
                 "  .globl _start",
                 "_start:",
                 "  la sp, stack_top",
                 "  la s0, data_mid"]
        for reg in FREE:
            v = r.choice(EDGE_VAL) if r.random() < 0.3 else r.getrandbits(32)
            text.append(f"  li {x(reg)}, {v:#x}")
        # Every register is written before use: the RTL register file reset value
        # is not part of the contract.
        for reg in [RA, T0] + COUNTERS:
            text.append(f"  li {x(reg)}, 0")
        main = []
        self.body(main, 0, self.budget, self.blocks)
        text += main
        text += ["  # end of test: PASS",
                 "  li x31, 0x10000000",
                 "  li x30, 1",
                 "  sw x30, 0(x31)",
                 "  j ."]
        for f in fun_text:
            text += f
        text += ["", "  .data", "  .balign 16", "data_lo:"]
        words = [r.choice(EDGE_VAL) if r.random() < 0.2 else r.getrandbits(32)
                 for _ in range(DATA_BYTES // 4)]
        for i in range(0, len(words), 8):
            if i == len(words) // 2:
                text.append("data_mid:")
            text.append("  .word " + ", ".join(f"{w:#010x}" for w in words[i:i + 8]))
        # Pointer table: every entry holds the address of one of the first PTRS entries,
        # so with t = such an address, "lw t, 4*k(t)" (k < PTRS) stays inside the table and
        # loads another valid pointer. Stores never target it (they all go to s0 +- 2 KiB).
        text += ["  .balign 4", "ptrs:"]
        text += [f"  .word ptrs + {4 * r.randrange(PTRS)}" for _ in range(2 * PTRS)]
        text += self.data
        text += ["  .balign 16", f"  .space {STACK_BYTES}", "stack_top:", ""]
        return "\n".join(text) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="constrained-random RV32I program generator")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("-o", "--output", help="output .S file (default: stdout)")
    ap.add_argument("--budget", type=int, default=30000,
                    help="upper bound on dynamic instructions of main (estimate)")
    ap.add_argument("--blocks", type=int, default=60, help="top-level blocks in main")
    ap.add_argument("--no-wrong-path-traps", action="store_true",
                    help="no ecall/ebreak/illegal words in unreachable code")
    args = ap.parse_args(argv)
    g = Gen(args.seed, args.budget, args.blocks, not args.no_wrong_path_traps)
    src = g.program()
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as f:
            f.write(src)
    else:
        sys.stdout.write(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
