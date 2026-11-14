# Branch predictor (rtl/bp/)

This document explains the branch prediction unit `bpu`: what each part does and why, how the
tables are organised, how speculative state is repaired, what it costs in flops, how fast it is
and how accurate it is. The interface itself is fixed by `docs/SPEC.md` section 5; this file
adds the exact timing rules the core must follow (section 2).

Files:

| File | Contents |
|---|---|
| `rtl/bp/bp_pkg.sv` | kinds (`cf_kind_e`, `btb_kind_e`), `meta_w()`, `ras_ptr_w()`, `ctr_next()` |
| `rtl/bp/bp_ram.sv` | generic table: 2 combinational read ports, 1 write port, in flops |
| `rtl/bp/bp_btb.sv` | branch target buffer, direct mapped |
| `rtl/bp/bp_bimodal.sv` | 2-bit counters indexed by PC |
| `rtl/bp/bp_gshare.sv` | 2-bit counters indexed by PC xor history |
| `rtl/bp/bp_tournament.sv` | bimodal + gshare + chooser |
| `rtl/bp/bp_ras.sv` | return address stack with checkpoint repair |
| `rtl/bp/bpu.sv` | top: picks the predictor, owns the GHR, LFSR, meta packing |
| `tb/bp/bp_model.py` | bit-exact Python golden model |
| `tb/bp/test_bpu.py`, `run_tests.py` | cocotb tests (directed + random against the model) |
| `tb/bp/mutate.py` | mutation check: injects 15 bugs, each must make a test fail |
| `tb/bp/synth_traces.py`, `trace_sim.py` | synthetic traces and the trace-driven accuracy study |
| `mk/bp.mk` | `make bp-lint`, `make bp-test`, `make bp-accuracy`, `make bp-mutate` |

## 1. What the predictor is for

The core fetches one instruction per cycle but only learns in EX (two stages later) whether a
branch is taken and where a jump goes. Without prediction, fetch would either stall for every
control-flow instruction or always fetch pc+4 and throw away two instructions each time a branch
is taken. The predictor guesses, in the same cycle as the fetch, the next PC. A wrong guess
costs the same as no guess (the younger instructions are flushed); a right guess costs nothing.

At fetch time the instruction is not decoded yet, so the predictor has to answer three
questions from the PC alone:

1. Is this PC a control-flow instruction, and of which kind? (BTB)
2. If it is a conditional branch, is it taken? (direction predictor)
3. Where does it go? (BTB target, or the return address stack for a return)

```
                      pred_pc_i
                          |
          +---------------+-------------------+
          |               |                   |
          v               v                   v
     +---------+   +--------------+     +-----------+
     |   BTB   |   |  direction   |<----|    GHR    |  (GSHARE, TOURNAMENT)
     | hit     |   |  predictor   |     +-----------+
     | kind    |   |  (BP_TYPE)   |     +-----------+
     | target  |   +------+-------+     |    RAS    |  top entry, pointer
     +--+--+---+          | taken?      +-----+-----+
        |  |              |                   |
        |  +--- kind -----+-----> select <----+ top (for kind = ret)
        |                          |
        +--- hit ----------------> |
                                   v
           pred_taken_o, pred_target_o (= target or pc+4), pred_meta_o = {RAS top, RAS ptr, GHR}
```

## 2. Timing contract with the core

Everything below is what the RTL does; the golden model does the same, and the tests check it.

**Predict (combinational).** In every cycle, `pred_taken_o`, `pred_target_o` and `pred_meta_o`
are a function of `pred_valid_i`, `pred_pc_i` and the current state only. There is no path from
the update or flush inputs to these outputs (the testbench checks the outputs again after driving
them), so a repair is visible from the next cycle.

- `pred_target_o` is the predicted **next fetch PC**: the target when `pred_taken_o = 1`,
  otherwise `pred_pc_i + 4`. The core can use it directly as the next PC.
- `pred_valid_i = 0` forces `pred_taken_o = 0` and blocks `pred_fire_i`.

**Fire.** Assert `pred_fire_i` (with `pred_valid_i`) in exactly one cycle per instruction that
enters the pipeline: the cycle in which fetch advances past `pred_pc_i` (for the memory protocol
of SPEC section 2, `imem_req_o && imem_gnt_i`). The outputs of *that* cycle are the ones that
count: capture `pred_taken_o`, `pred_target_o` and `pred_meta_o` in the same cycle. During a fetch
stall, keep `pred_fire_i` low; the outputs may change during a stall (an update can retrain a
counter) and that is fine, because nothing was committed yet.

**Update.** `upd_valid_i` once per resolved control-flow instruction, in program order, with the
`pred_meta_o` value captured at its fire. `upd_target_i` is only used when `upd_taken_i = 1`.
`upd_mispredict_i = 1` when the next PC the instruction was fetched with (its `pred_target_o`)
differs from the real next PC.

**Flush.** `flush_i` with `flush_meta_i` = the meta of the instruction at which the flush
happens (FENCE.I, a trap): all younger predictions are discarded and the GHR and RAS go back to
the state *before* that instruction. That is correct because such an instruction is not control
flow (if the BTB wrongly thought it was, its effect is also undone).

**Priority in one cycle**, for the speculative state (GHR, RAS):
`flush_i` > `upd_valid_i && upd_mispredict_i` > `pred_fire_i`.
In a repair cycle the instruction being fetched is on the wrong path, so its `pred_fire_i` is
ignored and the core must fetch the corrected PC in the next cycle. Training (BTB, counters) from
`upd_valid_i` happens in every cycle it is asserted, even with `flush_i`; the core should not
send updates for squashed instructions.

**A non-control-flow instruction predicted taken.** It can happen (a BTB entry left over after
code was rewritten, and the `RANDOM` predictor does it on purpose on about 1/16 of BTB misses).
The core then has to redirect to pc+4 and should report it as an update with `upd_kind_i = 0`,
`upd_mispredict_i = 1`, `upd_taken_i = 0`: the GHR and RAS are restored without any effect of the
instruction, and the stale BTB entry is invalidated. Using `flush_i` with the instruction's meta
also repairs the state, but leaves the entry, so the same wrong redirect repeats. (Added to the
"Open questions" of SPEC.md.)

**Sizing the meta.** `localparam int unsigned BP_META_W = bp_pkg::meta_w(GHR_BITS, RAS_DEPTH);`
The core treats the meta as opaque bits carried IF to EX with the instruction (and kept with the
instruction in any fetch buffer). The `META_W` parameter of `bpu` may be left at its default; a
wrong value stops elaboration with an error.

## 3. BTB

Direct mapped, `BTB_ENTRIES` entries (default 64), one entry per control-flow PC.

```
pred_pc_i:   31                       8 7          2 1 0      (BTB_ENTRIES = 64, IW = 6)
            +-------------------------+------------+---+
            |       tag (24 bits)     | index (6)  |00 |
            +-------------------------+------------+---+

entry (57 bits):  | valid | tag[23:0] | kind[1:0] | target[31:2] |
kind: 0 branch, 1 jump (jal/jalr), 2 call, 3 ret
```

- PCs are word aligned (RV32I has no compressed instructions), so bits 1:0 are never stored:
  the index starts at bit 2 and the target is 30 bits.
- **Full tag.** All 30 - IW upper bits are compared, so two PCs never share an entry by
  accident. A partial tag (say 8 bits) would save 16 bits per entry (about 28% of the BTB) at the
  cost of rare false hits; with full tags a false hit can only come from rewritten code. This is
  the first place to save area if needed.
- **Kind in 2 bits.** Fetch only needs to know where the next PC comes from: the direction
  predictor (branch), the stored target (jal, jalr), the stored target plus a RAS push (call),
  or the RAS (ret). JAL and JALR behave the same here.
- **Allocation, at update time only**: a taken branch or any jump writes the entry
  (valid, tag, kind, target); a not-taken branch that misses is *not* allocated (fetching pc+4
  was already right, so it would only evict something useful); an update with kind 0 invalidates
  the entry if it hits. A JALR keeps the last target it went to, which is the simplest indirect
  predictor.
- Direct mapped means two hot PCs with the same index evict each other. Two of the synthetic
  traces first had code on power-of-two boundaries (8 interpreter handlers 256 bytes apart, a jump
  256 bytes after a call site), and those PCs fought over one entry; real code does this too
  (aligned functions). A 2-way BTB with LRU would fix it at the
  cost of a second tag comparator and a way mux on the critical path.

## 4. Direction predictors

All tables are 2-bit saturating counters:

```
          taken        taken        taken
     +---------->+---------->+---------->+
  [0 strong NT] [1 weak NT] [2 weak T] [3 strong T]     predict taken = counter[1]
     +<----------+<----------+<----------+
        not taken   not taken   not taken
```

Two wrong outcomes in a row are needed to flip a strong state, so a loop branch that is taken
N-1 times and then falls through costs one misprediction per loop, not two. Counters reset to 1
(weakly not taken).

### STATIC_NT

Every conditional branch is predicted not taken. Jumps, calls and returns found in the BTB are
still predicted taken (they are always taken, so there is nothing to guess). It is the baseline:
no counters at all.

### BIMODAL

`BHT_ENTRIES` counters (default 512), index `pc[2 +: 9]`. Learns the bias of each branch: loops,
error checks, `if (rare)`. Cannot learn a pattern (T,N,T,N gives 50%), and branches whose PCs are
512 instructions apart share a counter.

### GSHARE

The global history register (GHR) is a shift register of the directions of the last `GHR_BITS`
predicted branches (newest in bit 0). The counter index is

```
index = pc[2 +: 9] XOR GHR[8:0]            (BHT_ENTRIES = 512, GHR_BITS = 9)
```

so the same branch gets a different counter for each recent path. That makes patterns and
correlations learnable: T,N,T,N becomes two counters, one for "last was T" and one for "last was
N". The cost is that a branch spreads over many counters (slower warm-up, more aliasing), which
is why gshare loses to bimodal on purely biased branches (`biased` trace: 81% vs 88%).
`GHR_BITS` may be shorter than the index; the history then goes into the low bits.

A history is only useful if it is long enough to tell apart the situations it has to separate.
In the `loops` trace the inner back edge at iteration 4 (taken) and at iteration 6 (exit) see
exactly the same last 9 outcomes, so 9-bit gshare mispredicts both, every time: 86.6%.

### TOURNAMENT

Bimodal and gshare tables side by side, plus a chooser table of 2-bit counters indexed by PC:
chooser >= 2 means "trust gshare for this branch". At update time all three are read; both
components train on every branch, the chooser trains only when the components disagreed, towards
the one that was right. It gets the best of both: 88.1% on `biased`, 100% on `alternating`,
93.3% on `loops` (bimodal is right on the iteration-4 back edge that gshare cannot separate).
It costs three tables instead of one.

### RANDOM (simulation only)

A 16-bit LFSR (x^16 + x^14 + x^13 + x^11 + 1) that advances once per fire. It predicts BTB-hit
branches at random, and on purpose also: predicts a jump not taken (1/8), sends a jump to a wrong
target (1/8) and redirects an instruction that missed the BTB (so possibly an ordinary
instruction) to a wrong target (1/16). Wrong targets are pc+4 .. pc+64, so the wrong path stays in
RAM. Its only job is to break the core: the core must run every program correctly with it, just
slower. It cannot be synthesized: with `SYNTHESIS` defined (Yosys defines it; pass `-DSYNTHESIS`
to sv2v) `BP_TYPE = "RANDOM"` stops elaboration with an error.

## 5. Speculative global history and its repair

Why speculative: the GHR used for a prediction must contain the branches fetched just before,
but those are only resolved two or more cycles later. Waiting for them would mean predicting with
an old history. So the GHR is updated at fire with the *predicted* direction, and every
prediction carries a checkpoint of the GHR **before** its own shift in its meta.

Rules:

- fire of a BTB-hit branch: `GHR = {GHR[G-2:0], predicted direction}`;
- misprediction of instruction X: `GHR = checkpoint(X)`, then if X is a branch,
  shift in its **real** direction. All younger predictions are being squashed, so their shifts
  disappear with them;
- flush at X: `GHR = checkpoint(X)`.

Only branches that hit in the BTB at fetch are shifted in (a branch the BTB does not know looks
like any other instruction). If such a branch turns out taken it is a misprediction and the repair
adds it; if it is not taken, it stays out of the history. Hardware and model agree on this, and
the repair is exact in the sense the spec requires: after a repair the GHR is exactly what it
would have been had fetch followed the right path.

**Worked example** (GHR_BITS = 4, GHR = 0000, branches A, B, C all in the BTB):

| cycle | event | meta carried (checkpoint) | GHR after the edge |
|---|---|---|---|
| 1 | fire A, predicted T | A: 0000 | 0001 |
| 2 | fire B, predicted N | B: 0001 | 0010 |
| 3 | fire C, predicted T | C: 0010 | 0101 |
| 4 | A resolves: really N, mispredicted | (from A's meta: 0000) | 0000 then shift N = **0000** |
| 5 | fetch restarts at A's fall-through with GHR 0000 | | |

B and C were on the wrong path; their shifts are gone. If instead B had mispredicted in cycle 4
(and A was right), the GHR would become B's checkpoint 0001 shifted with B's real direction T:
0011.

## 6. Return address stack and its repair

A return goes to a different place depending on who called the function, so the BTB's "last
target" is wrong whenever the call site changes (`calls` trace: 4 call sites, so without a RAS
every return is mispredicted, with it 100% are right). The RAS is a small circular stack of
return addresses (`RAS_DEPTH`, default 4, 30 bits each):

- fire of a BTB "call": push pc+4 (pointer + 1, then write);
- fire of a BTB "ret": predict the top of stack (TOS), pop (pointer - 1);
- overflow wraps and overwrites the oldest entry, underflow wraps and returns a stale one. Both
  only cost mispredictions. The `recursion` trace (depth 10) gets 92.6% of returns right with 4
  entries.

Push and pop happen at fire, speculatively, so the wrong path can damage the stack. Each meta
carries the pointer and the TOS **value** at the checkpoint. Repair on a misprediction of X:
restore the pointer, write the saved TOS value back into the TOS slot, then replay X's own effect
(push if X is a call, pop if X is a ret). Restoring the value matters for the most common damage:
a wrong-path return (pop) followed by a wrong-path call (push), which overwrites exactly the slot
that was TOS:

| step | event | ptr | stack (slot: value) |
|---|---|---|---|
| 0 | inside f, called from c1 | 1 | 1: c1+4 |
| 1 | fire branch B (meta: ptr 1, TOS c1+4) | 1 | 1: c1+4 |
| 2 | wrong path: ret, pop | 0 | 1: c1+4 |
| 3 | wrong path: call from c2, push | 1 | 1: **c2+4** (TOS overwritten) |
| 4 | B mispredicted: ptr = 1, slot 1 = c1+4 | 1 | 1: c1+4 (repaired) |

Deeper damage (two pops then two pushes on the wrong path) is not repaired; the spec accepts that
since it only costs an occasional misprediction. The repair writes two different slots in one
cycle (TOS restore and the replayed push to TOS+1), so the RAS is plain flops with two write
ports; at 4 to 8 entries an SRAM would not be worth it.

## 7. Meta layout and parameters

```
pred_meta_o  (LSB first)         default: GHR_BITS = 9, RAS_DEPTH = 4  ->  META_W = 41
+------------------+----------------+---------------+
| ras_top [30]     | ras_ptr [P]    | ghr [G]       |
| bits 40..11      | bits 10..9     | bits 8..0     |
+------------------+----------------+---------------+
G = GHR_BITS, P = log2(RAS_DEPTH) (0 if no RAS), top = 30 bits if RAS_DEPTH > 0
META_W = max(1, G + P + 30*(RAS_DEPTH > 0))
```

`META_W` does not depend on `BP_TYPE`, so the core does not care which predictor it gets. For
types without history the GHR field is constant 0 and synthesis removes the pipeline flops that
carry it: the bits really carried are 41 for GSHARE/TOURNAMENT and 32 for the others.

| Parameter | Default | Allowed |
|---|---|---|
| `BP_TYPE` | `"GSHARE"` | `"STATIC_NT"`, `"BIMODAL"`, `"GSHARE"`, `"TOURNAMENT"`, `"RANDOM"` (sim only) |
| `BTB_ENTRIES` | 64 | power of two >= 2 |
| `BHT_ENTRIES` | 512 | power of two >= 2 (size of each counter table) |
| `GHR_BITS` | 9 | 1 .. log2(BHT_ENTRIES) for GSHARE/TOURNAMENT; any for the others |
| `RAS_DEPTH` | 4 | 0 (no RAS) or a power of two >= 2 |

Bad values stop elaboration with `$error`.

## 8. Expected critical path

The predict path is one cycle from the fetch PC register, through the predictor, to the next-PC
mux and back into the PC register: the classic fetch loop.

```
PC reg -> BTB index decode/mux 64:1 (~6 mux levels) -> 24-bit tag compare (~6 levels) -> hit
PC reg -> XOR with GHR (1) -> PHT mux 512:1 (~9 levels) -> counter MSB  (in parallel with BTB)
       -> taken = hit & (kind == branch ? dir : 1)  (~2 levels)
       -> 32-bit 2:1 mux target / pc+4 (pc+4 adder runs in parallel)  -> core next-PC mux -> PC reg
```

So roughly 15 to 18 gate levels plus the large fan-out of the PC to 64 BTB entries; the BTB tag
compare and the 512:1 counter mux are about equally long. TOURNAMENT adds one 2:1 mux after the
counters (the chooser is read in parallel). The RAS top does not depend on the PC (it is read with
the stored pointer) and is off the critical path. If this path is too slow, the usual fixes are:
partial BTB tags (shorter compare), reading the tables with the *next* PC one cycle early (which
is also what an SRAM needs, since an SRAM reads a registered address: `bp_ram` is the only place
to change), or a two-cycle predictor with a fast next-line guess.

## 9. Area: flip-flop bits per configuration (defaults)

| BP_TYPE | BTB | counters | GHR | RAS | other | total | meta bits carried |
|---|---:|---:|---:|---:|---:|---:|---:|
| STATIC_NT | 3648 | 0 | 0 | 122 | 0 | 3770 | 32 |
| BIMODAL | 3648 | 1024 | 0 | 122 | 0 | 4794 | 32 |
| GSHARE | 3648 | 1024 | 9 | 122 | 0 | 4803 | 41 |
| TOURNAMENT | 3648 | 3072 | 9 | 122 | 0 | 6851 | 41 |
| RANDOM | 3648 | 0 | 0 | 122 | 16 (LFSR) | 3786 | 32 |

BTB = 64 x (1 + 24 + 2 + 30) = 3648; counters = 512 x 2 per table; RAS = 4 x 30 + 2.
The meta also costs META_W flops per pipeline register that carries it (IF/ID and ID/EX: 82 more
for GSHARE). The BTB dominates: half of TOURNAMENT and three quarters of GSHARE. Flops, not
gates: the read muxes add logic of the same order. An SRAM version would be much smaller per bit.

## 10. Verification

**Golden model.** `tb/bp/bp_model.py` implements the same tables, indexing, counter updates,
allocation policy, GHR and RAS repair, LFSR and meta layout, cycle by cycle: `predict()` gives the
combinational outputs, `tick()` applies one clock edge with all reads before all writes.

**cocotb tests** (`tb/bp/test_bpu.py`, Verilator). Every test runs the model in lockstep and
compares `pred_taken_o`, `pred_target_o` and `pred_meta_o` with it in every cycle, twice (before
and after the update/flush inputs are driven). Directed tests on top of that:

| Test | Checks |
|---|---|
| `test_reset_state` | nothing hits after reset |
| `test_loop_branch_learns_taken` | a loop branch ends up predicted taken (never for STATIC_NT) |
| `test_alternating_pattern` | T,N learned by GSHARE/TOURNAMENT (0 misses in the last 50), not by BIMODAL (>= 20) |
| `test_btb_aliasing` | same index, tags differing in the lowest bit evict each other; other index untouched |
| `test_not_taken_branch_not_allocated` | allocation policy |
| `test_kind_none_invalidates` | kind 0 update removes the entry |
| `test_call_ret_ras` | 2 call sites: RAS gets 4/4 returns, without RAS 0/4 |
| `test_nested_calls` | nested returns in order |
| `test_ras_overflow` | depth+2 nesting: innermost RAS_DEPTH right, outer 2 wrong |
| `test_mispredict_repairs_ghr` | shift at fire, exact repair with the real outcome; jumps not shifted; BTB-miss branch added |
| `test_mispredict_repairs_ras` | the pop-then-push wrong path of section 6 |
| `test_mispredicted_call_and_ret_replayed` | a mispredicted call is pushed by the repair |
| `test_flush_restores` | flush puts back GHR and RAS exactly |
| `test_fire_ignored_during_repair` | priority rule of section 2 |
| `test_stall_keeps_prediction` | no fire, no change; fire without valid is ignored |
| `test_random_pipeline` | 50,000 cycles of random stream (below) |

**Random test.** A random program (two code regions that alias in every table, with branches
that are biased, periodic, loops or random, jal, jalr with several targets, calls and returns
with a real call stack) is executed in program order. Fetch runs ahead along the predicted path
with 0 to 4 predictions in flight, with random fetch stalls and `pred_valid_i` gaps; the oldest
instruction resolves at random times, mispredictions squash and redirect (and the repair cycle
also fires a wrong-path prediction, which must be ignored), random flushes (FENCE.I-like, or a
trap-like jump anywhere), and instructions of the program change kind now and then (rewritten
code, which produces kind 0 updates). Every output is compared with the model in every cycle.

**Matrix.** 5 types x 5 parameter sets = 25 builds (`run_tests.py`):
default 64/512/9/4, tiny 4/16/4/2, noras 16/64/3/0, ghr1 8/8/1/8, mid 32/128/7/8
(BTB/BHT/GHR/RAS). Result on 14 Nov 2026: **25/25 configurations pass, 16/16 tests each**
(random test 50,000 cycles, seed 1; about 30,000 fires, 10,000-27,000 updates and up to 10,000
repairs per configuration). Summary in `build/bp/cocotb/summary.txt`.

**Mutation check.** `tb/bp/mutate.py` copies `rtl/bp/`, injects one bug at a time (TOS not
restored, wrong pop count, call pushes pc instead of pc+4, flush replays a stale kind, GHR repair
without the real outcome, GHR repair shifting for jumps, fire beating flush, gshare training with
the wrong history, wrapping counters, chooser always trained, not-taken branches allocated, no
invalidation, tag compare missing a bit, prediction not gated by valid, LFSR advancing every
cycle) and runs the tests. Result: **15/15 mutants killed**.
The first run found a weak test: the aliasing test used two PCs whose tags differed in two bits,
so a tag compare that ignores the lowest bit passed. The test now uses tags that differ only in
the lowest bit.

**Lint.** `make bp-lint` runs `verilator --lint-only -Wall` for 5 types x 4 parameter sets.
No waivers: bits that a configuration does not use are collected in `unused_*` signals, which
Verilator ignores by name.

Commands (from the repo root):

```
make bp-lint                       # 20 lint runs, must print "bp-lint: clean"
make bp-test                       # 25 configurations, ~1.5 min with 6 jobs
make bp-test BP_TEST_ARGS="--only GSHARE --params default --cycles 200000 --seed 7"
make bp-mutate                     # 15 mutants, all must be killed (~40 s)
make bp-accuracy                   # synthetic traces + accuracy tables in build/bp/accuracy.md
make bp-accuracy BP_TRACES="build/iss/*.trace"     # add real traces from the ISS
```

## 11. Trace format and accuracy study

**Format** (input of `tb/bp/trace_sim.py`), one line per executed control-flow instruction, in
program order, lowercase or uppercase hex, single spaces, `#` starts a comment:

```
<pc> <kind> <taken> <target>
80000010 1 1 80000000      branch at 80000010, taken, to 80000000
80000014 1 0 80000040      branch not taken (target = the branch target, not pc+4)
80000020 4 1 80000400      call
80000404 5 1 80000024      ret
```

kind uses the `upd_kind_i` encoding: 1 branch, 2 jal, 3 jalr, 4 call (jal/jalr with rd in
{x1, x5}), 5 ret (jalr with rs1 in {x1, x5}, rd = x0). taken is 0/1 (always 1 except branches).
The ISS can write it directly from its commit loop.

**Method.** Each trace is replayed through the golden model with the default parameters. Lag 0:
predict, then resolve, one instruction at a time. Lag 2: up to two younger control-flow
instructions are predicted before the oldest resolves, with squash and repair on a misprediction.
A prediction counts as right when its next PC equals the real next PC (so a branch predicted
taken to a wrong target is wrong).

**Synthetic workloads** (`synth_traces.py`, 30,000 records each, fixed seeds): `loops` (nested
loops, an if on the index parity), `alternating`, `correlated` (third branch repeats the first),
`random` (50/50 data branch), `biased` (32 branches with fixed biases), `calls` (one helper from
4 sites), `recursion` (fib(10), depth 10), `many_branches` (2048 static branches, far more than the
tables), `interpreter` (jalr dispatch to 8 handlers), `mix` (main loop calling small kernels).

Results, default parameters (64/512/9/4), lag 0. The lag 2 tables are identical for every
type except RANDOM (see below); the full output is in `build/bp/accuracy.md`.

#### All control flow (branches, jumps, returns) accuracy (%), lag 0

| trace | STATIC_NT | BIMODAL | GSHARE | TOURNAMENT | RANDOM |
|---|---:|---:|---:|---:|---:|
| alternating | 25.0 | 50.0 | 100.0 | 100.0 | 49.9 |
| biased | 63.8 | 88.3 | 81.3 | 88.1 | 48.3 |
| calls | 61.9 | 80.9 | 100.0 | 100.0 | 60.2 |
| correlated | 37.5 | 63.0 | 73.0 | 73.8 | 50.1 |
| interpreter | 59.0 | 67.5 | 67.5 | 67.5 | 52.6 |
| loops | 33.3 | 73.3 | 86.6 | 93.3 | 49.8 |
| many_branches | 49.7 | 49.7 | 49.7 | 49.7 | 46.9 |
| mix | 56.3 | 82.1 | 91.9 | 92.3 | 58.1 |
| random | 24.6 | 75.4 | 74.1 | 75.0 | 50.2 |
| recursion | 80.8 | 80.8 | 94.6 | 94.2 | 64.7 |
| **all traces** | **49.2** | **71.1** | **81.8** | **83.3** | **53.1** |

#### Conditional branch direction+target accuracy (%), lag 0

| trace | STATIC_NT | BIMODAL | GSHARE | TOURNAMENT | RANDOM |
|---|---:|---:|---:|---:|---:|
| alternating | 25.0 | 50.0 | 100.0 | 100.0 | 49.9 |
| biased | 63.8 | 88.3 | 81.3 | 88.1 | 48.3 |
| calls | 33.3 | 66.7 | 100.0 | 100.0 | 49.4 |
| correlated | 37.5 | 63.0 | 73.0 | 73.8 | 50.1 |
| interpreter | 67.3 | 92.0 | 91.9 | 92.0 | 75.2 |
| loops | 33.3 | 73.3 | 86.6 | 93.3 | 49.8 |
| many_branches | 49.7 | 49.7 | 49.7 | 49.7 | 46.9 |
| mix | 38.9 | 75.0 | 88.6 | 89.2 | 51.2 |
| random | 24.6 | 75.4 | 74.1 | 75.0 | 50.2 |
| recursion | 49.7 | 49.7 | 91.3 | 90.1 | 50.1 |
| **all traces** | **40.3** | **67.7** | **81.2** | **83.1** | **50.6** |

#### Returns and jumps (%), lag 0

| trace | STATIC_NT ret | BIMODAL ret | GSHARE ret | TOURNAMENT ret | RANDOM ret | jumps (STATIC_NT) |
|---|---:|---:|---:|---:|---:|---:|
| calls | 100.0 | 100.0 | 100.0 | 100.0 | 74.2 | 99.9 |
| interpreter | - | - | - | - | - | 54.7 |
| many_branches | - | - | - | - | - | 0.0 |
| mix | 100.0 | 100.0 | 100.0 | 100.0 | 75.4 | 99.9 |
| recursion | 92.6 | 92.6 | 92.6 | 92.6 | 69.5 | 100.0 |

(jumps = jal, jalr and call, the same for every type except RANDOM.)

#### Table size sweep: all control flow, all traces, lag 0 (%)

| configuration | flop bits (TOURNAMENT) | BIMODAL | GSHARE | TOURNAMENT |
|---|---:|---:|---:|---:|
| BTB 16 | 4147 | 66.3 | 77.1 | 78.2 |
| BTB 64 (default) | 6851 | 71.1 | 81.8 | 83.3 |
| BTB 256 | 17283 | 71.8 | 82.6 | 84.2 |
| BHT 128, GHR 7 | 4545 | 71.1 | 80.2 | 83.0 |
| BHT 2048, GHR 11 | 16069 | 71.1 | 83.7 | 84.4 |
| GHR 4 | 6846 | 71.1 | 80.4 | 82.2 |
| RAS 0 | 6729 | 66.4 | 77.1 | 78.6 |
| RAS 8 | 6972 | 71.3 | 82.0 | 83.5 |

**What the numbers say.**

- Dynamic prediction matters most: STATIC_NT to BIMODAL is +22 points on all control flow,
  BIMODAL to GSHARE +10.7, GSHARE to TOURNAMENT +1.5.
- GSHARE wins on patterns and correlation (`alternating` 100%, `correlated` 73%: the first two
  branches there are coin flips, the third is learned) and loses on plain bias (`biased`).
  TOURNAMENT is never much worse than the better of the two.
- `random` and `many_branches` are the floors: no predictor can learn a coin flip, and when the
  BTB is far too small nothing is ever predicted taken (all types give 49.7%).
- `interpreter`: about half the dispatch jumps go wrong whatever the direction predictor; a JALR
  keeps only its last target. An indirect target predictor (target indexed by history) is the fix.
- The RAS is worth about 5 points overall (sweep: RAS 0 vs 4) for 122 flops, the best value per
  bit in the unit. 8 entries instead of 4 add little on these traces.
- **Lag 0 and lag 2 give the same numbers** for every deterministic predictor. This is not a bug
  in the replay (RANDOM, whose LFSR sees the extra fires, does change): a correct prediction only
  pushes its counter further from the threshold, so a late update never flips a prediction made in
  between, a wrong one squashes all younger predictions and they are redone after the repair, and
  the GHR repair is exact. Late training could only matter through the inexact RAS repair or the
  tournament's unselected component; with deliberately tiny tables (8-entry BTB, 4-entry BHT,
  `mix` trace) that shows up as less than 0.4% difference. In other words, the exact checkpoint repair is what
  makes the speculative history cost nothing in accuracy.
- Bigger tables: a 256-entry BTB (+10,400 flops) gives under +1 point; a 2048-entry BHT with
  11 history bits (+9,200 flops for TOURNAMENT) +1.9 for GSHARE and +1.1 for TOURNAMENT. A
  16-entry BTB loses about 5 points. The default sizes are on the flat part of the
  curve for programs of this size; real benchmarks from the ISS will tell more.

These are synthetic traces: they show that each mechanism does what it should and where its
limits are, not what the core will get on real programs. For that, feed ISS traces with
`BP_TRACES=...`, and compare with the core's `br_mispred` statistics.

## 12. Known limitations and next steps

- Direct-mapped BTB, last-target JALR prediction, no indirect predictor.
- The history contains only branches that hit in the BTB at fetch.
- RAS repair is top-pointer + TOS value, not a full copy (deep wrong-path damage stays).
- Training re-reads the tables at update time (second read port on every table). A 1R1W SRAM
  version would carry the counter values read at predict time in the meta, or add a bypass.
- Tables are in flops with reset; an SRAM has no reset and would need an init walk or valid bits.
- STATIC_NT predicts backward branches not taken too; backward-taken/forward-not-taken would be a
  better static baseline but needs the branch offset, which the BTB does not store.
- RANDOM's wrong targets are pc+4..pc+64: code within 64 bytes of the end of RAM could make a
  wrong-path fetch leave the memory map.
- No synthesis run yet (sv2v and Yosys are not installed on this machine); the RTL sticks to the
  subset of SPEC section 3, and timing and area above are estimates, not reports.
