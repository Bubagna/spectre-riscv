# rv_core: pipeline architecture

This document explains how the core in `rtl/core/` works and why it is built this way. The
interface contract (ISA subset, memory map, memory protocol, predictor port, trace format) is in
`docs/SPEC.md`; this file is about the inside.

## 1. Overview

A 5-stage, in-order, single-issue RV32I pipeline: IF, ID, EX, MEM, WB. It has full forwarding,
one load-use stall, a branch predictor consulted every cycle, and branches resolved in EX. Both
memory ports use a request/grant/response protocol, so any stage that talks to memory can wait
for as long as the memory wants.

```
            redirect_pc (mispredict, FENCE.I, trap)
   +--------------------------------------------------------+
   |                                                        |
   v                                                        |
+--------+  +-------+   +--------+   +--------+   +-----+   +-----+
| fetch  |->| fetch |-->|   ID   |-->|   EX   |-->| MEM |-->| WB  |
| PC/bpu |  | queue |   | decode |   | ALU    |   | req |   | rsp |
+--------+  +-------+   | regfile|   | branch |   +-----+   +-----+
   |  ^         ^       | hazard |   | CSR    |      |   ^     |
   |  |         |       +--------+   +--------+      |   |     |
   |  |         |          ^   ^       ^   ^         |   |     |
   v  |         |          |   |       |   +--MEM----+   |     |
  imem port: req/gnt  rvalid/rdata     |    forwarding   |     |
                                       +------WB---------|-----+
                                          forwarding     |
                                   regfile write, bypass to ID
                                                         |
                                             dmem port: req/gnt (MEM), rvalid/rdata (WB)
```

| Stage | File | Work |
|---|---|---|
| IF | `rv_fetch.sv` | fetch PC, predictor lookup, instruction requests, fetch queue |
| ID | `rv_core.sv`, `rv_decoder.sv`, `rv_regfile.sv` | decode, register read with WB bypass, load-use check |
| EX | `rv_core.sv`, `rv_alu.sv`, `rv_csr.sv` | forwarding, ALU, branch compare and target, misprediction and trap detection, CSR read, load/store address and byte lanes |
| MEM | `rv_core.sv` | data request (req/gnt) |
| WB | `rv_core.sv` | data response (rvalid), load extension, register write, retirement, RVFI |

Pipeline registers: `ex_*_q` (ID/EX), `mem_q` (EX/MEM), `wb_q` (MEM/WB). The two later ones share
one packed struct, `rv_pkg::pipe_t`. There is no explicit IF/ID register: the head of the fetch
queue plays that role (section 3).

The predictor sits next to fetch: `bpu` from `rtl/bp/`, or `rtl/core/bpu_stub.sv` when the core
is built with `RV_BPU_STUB` (section 7).

## 2. The rule that keeps everything precise

Every redirect is decided in EX, in the cycle the instruction leaves EX. EX always holds the
oldest instruction that can still change the flow (branches, jumps, FENCE.I, and every trap,
including misaligned loads/stores, whose address is only known in EX). So:

- a redirect only ever kills IF and ID, never MEM or WB;
- everything in MEM and WB is older than the redirecting instruction and always completes;
- nothing younger than an unresolved branch can reach MEM, so **no wrong-path load or store ever
  touches the data memory**. Wrong-path work stays in IF and ID: instruction fetches, decode,
  register reads.

The last point matters for the Spectre phase of the project: in this pipeline speculation only
covers instruction fetch, so a Spectre v1 gadget cannot leak through data accesses. To show the
attack, the speculation window has to reach MEM (a deeper pipeline, a branch that resolves later
than its dependent load, or a data cache with speculative fills).

## 3. Fetch (rv_fetch.sv)

### 3.1 What one fetch looks like

```
cycle         t            t+1                   t+2
imem_req      1 (pc_q)     1 (next pc)
imem_gnt      1            1
pc_q          A     ->     B = predicted next of A
imem_rvalid                1 (insn at A)
ID decodes                 A                     B
```

In cycle t fetch requests `pc_q` and asks the predictor about the same PC (combinational lookup).
At the edge that grants the request, `pc_q` becomes the predicted next PC: the predictor's target
if it says taken, else `pc_q + 4`. That value is also stored with the instruction as `pred_npc`.
So a correctly predicted taken branch costs nothing: the next request already goes to the target.

The response comes back at least one cycle later. With a 1-cycle memory the instruction is
decoded by ID in the cycle the response arrives: when the queue holds no older instruction, the
response bypasses it and feeds ID directly (`id_insn_o = has_data ? queue : imem_rdata_i`). With
an ideal memory the core runs at one instruction per cycle.

### 3.2 The fetch queue

A circular buffer of `FETCH_DEPTH` (4) entries with three pointers:

```
   head            fill                tail
    |  data ready   |  waiting for data  |  free
    v               v                    v
  [ A | B ]       [ C | D ]            [   ...   ]
```

- an entry is allocated when its request is **granted** (PC, `pred_npc`, predictor meta);
- it is filled when its response arrives (responses come back in order, so always at `fill`);
- it is freed when ID takes it.

A request is only sent while the queue has a free entry, so a response always has somewhere to go
and a stall in ID never needs to stall the memory. The request depends only on flops (queue
occupancy, outstanding count, `stale_q`, `halt`), never on what EX or ID do in the same cycle.

### 3.3 Redirects with requests in flight

A redirect from EX (misprediction, FENCE.I, trap) sets `pc_q` to the new PC and empties the queue.
Two kinds of old requests cannot be cancelled:

1. **Granted, response not back yet.** Responses come in order and cannot be recalled, so the
   fetch unit counts them: `outst_q` counts every granted request without a response, and on a
   redirect `drop_q = outst_q` (the next `drop_q` responses are thrown away). New responses are
   accepted only once `drop_q` is back to 0.
2. **Presented but not granted.** The protocol (like OBI) requires a request to stay on the port,
   unchanged, until it is granted. So the request becomes *stale*: `stale_q` keeps the old address
   on the port, its grant increments `drop_q`, and only then do requests for the new PC start.

`sim_mem` checks the "unchanged until granted" rule, so a violation is a simulation error.

### 3.4 Predictor interface from the fetch side

- `pred_valid_i = 1` whenever fetch presents a new (not stale) request; `pred_pc_i = pc_q`.
- `pred_fire_i = 1` exactly on the handshake of such a request, and **never in a redirect cycle**:
  that fetch is about to be killed and the predictor is repairing its state in the same cycle.
- A predicted target with bit 1 or 0 set is aligned down before use. Such a target can never be
  right (no C extension), and EX treats it as an ordinary misprediction.

## 4. Data hazards

### 4.1 Forwarding

| Producer (distance) | Where the value is when the consumer is in EX | Path | Stall |
|---|---|---|---|
| ALU/JAL/LUI/CSR, distance 1 | `mem_q.result` | MEM -> EX | none |
| ALU/JAL/LUI/CSR, distance 2 | `wb_q.result` | WB -> EX | none |
| load, distance 2 | `dmem_rdata_i` in WB, extended | WB -> EX | none |
| load, distance 1 | not available yet | - | 1 cycle (load-use) |
| anything, distance 3 | being written to the register file | WB -> ID bypass inside `rv_regfile` | none |
| anything, distance >= 4 | register file | - | none |

Priority: MEM over WB (MEM holds the younger value). `x0` never forwards because `rd_we` is
false for `rd = 0`. The register file bypasses the value being written in the same cycle; this is
the flop version of "write in the first half, read in the second half".

### 4.2 Load-use stall

```
cycle        1    2    3    4    5    6
lw   x1      IF   ID   EX   MEM  WB
add  x2,x1        IF   ID   ID   EX   MEM     x1 forwarded from WB in cycle 5
                            ^ bubble into EX
```

Condition (`load_use` in `rv_core.sv`): ID holds a valid instruction that really reads (`use_rs1`
/`use_rs2`, so a store's rs2 counts and an immediate's rs2 field does not) the `rd` of a load in
EX. ID keeps its instruction and EX receives a bubble. The core checks in simulation that a load
in MEM never feeds EX, the invariant this stall guarantees.

### 4.3 Operands of a frozen EX

When MEM or WB stall, EX is frozen, but WB may still retire (for example while MEM waits for its
grant). The instruction leaving WB may be the source of a forwarded operand. To keep the value,
EX writes its forwarded operands back into `ex_rs1_q`/`ex_rs2_q` in every frozen cycle. A
forwarding source that is not valid yet (a load in WB still waiting) stays in WB until it is, and
keeps overriding the stored copy until then.

## 5. Control flow and mispredictions

### 5.1 Resolution in EX

EX computes the real next PC: `ex_npc = taken ? target : pc + 4`, with `target = pc + imm` for
JAL and branches (dedicated adder) and `(rs1 + imm) & ~1` for JALR (the ALU). It then compares it
with what fetch predicted:

```
ex_mispredict = (pred_npc != ex_npc)
```

This single comparison covers a wrong direction, a wrong target, and an ordinary instruction that
the predictor sent elsewhere (e.g. a stale BTB entry or the RANDOM predictor).

### 5.2 How a misprediction flows

```
cycle            1    2    3    4    5    6    7
beq (taken)      IF   ID   EX   MEM  WB
pc+4 (wrong)          IF   ID   xx                  killed in cycle 3 (in ID)
pc+8 (wrong)               IF   xx                  request of cycle 3: response dropped
target                          IF   ID   EX   MEM
                                ^ request for the target in cycle 4
```

In cycle 3, when `beq` leaves EX: `redirect = 1` and `redirect_pc = ex_npc`. The fetch queue and
ID are cleared, outstanding fetches are marked to drop, and the predictor receives the update with
`upd_mispredict = 1`. The request for the target goes out in cycle 4 and its instruction is in ID
in cycle 5. **Misprediction penalty: 2 cycles**, the textbook value for EX resolution.

A correctly predicted branch or jump, taken or not, costs 0 cycles.

### 5.3 Predictor update port

- `upd_valid` fires in the cycle an instruction leaves EX, so **exactly once per instruction and
  in program order** (EX is in order, and an instruction leaves EX once). Not earlier: while EX is
  frozen its operands may not be final.
- It fires for every control-flow instruction (`upd_kind` = branch/jal/jalr/call/ret), and also
  for a non-control-flow instruction that was mispredicted, with `upd_kind = CF_NONE`, so the
  predictor can repair its speculative history and forget that PC.
- `upd_target` is the computed target (`pc + imm` even for a not-taken branch; `pc + 4` for
  `CF_NONE`).
- A trapping instruction sends no update; it sends `flush_i` with its meta.
- FENCE.I sends `flush_i` with its meta, unless it was also mispredicted, in which case the
  update repairs everything. At most one of `flush_i` and `upd_mispredict` is high in a cycle.

The C++ harness checks this contract on every run: each retired control-flow instruction must
match the oldest pending update (same PC, same kind, and the outcome must agree with the next PC
actually retired), and wrong-path instructions must produce none.

### 5.4 FENCE.I

FENCE.I redirects to `pc + 4` like a misprediction, which empties the fetch queue and drops the
fetches in flight. Why that is enough: memory is unified and uncached, and `sim_mem` performs a
write at the edge where the store is granted. Any store older than the FENCE.I has been granted
by the time FENCE.I leaves EX (EX cannot advance while MEM waits for a grant), and the refetch is
requested at least one cycle later, so it reads the new data.

### 5.5 Traps

ECALL, EBREAK and illegal instructions are flagged by the decoder; misaligned branch/jump targets
(taken only) and misaligned loads/stores are detected in EX. A trapping instruction leaving EX:

1. redirects (to kill IF and ID) and sets `halting_q`, which stops all further fetching;
2. continues to MEM and WB with every side effect cleared (no register write, no memory access);
3. retires with `rvfi_trap = 1` (and `rvfi_halt = 1`), then `halt_o` goes high.

The decoder already clears every side-effect field of an illegal word, so garbage fetched on a
wrong path cannot write anything even before it reaches EX.

## 6. Stalls and the memory protocol

### 6.1 The three stall conditions

| Signal | Condition | Effect |
|---|---|---|
| `wb_wait` | WB holds a load/store whose response has not arrived | WB, MEM, EX frozen; ID frozen |
| MEM grant wait | MEM has a load/store and `dmem_gnt_i = 0` | MEM, EX frozen; WB retires and takes a bubble |
| `load_use` | ID reads the `rd` of a load in EX | ID frozen, bubble into EX |

In code: `adv_mem = !wb_wait && (!mem_memop || dmem_gnt_i)`, `adv_ex = adv_mem`,
`id_take = id_valid && adv_ex && !load_use && !redirect`. Fetch is decoupled by its queue: it
keeps fetching until the queue is full.

### 6.2 Data port timing

```
cycle            1        2        3        4
MEM              lw A     sw B     sw B     ...
dmem_req         1        1        1
dmem_gnt         1        0        1                 (memory stalls the grant)
WB               ...      lw A     -        sw B
dmem_rvalid               1        0        1
```

- The request is issued from MEM; the response is consumed in WB. Load data goes straight from
  `dmem_rdata_i` through the extension logic to the register file and the forwarding mux.
- **At most one data access is in flight.** MEM requests only when WB is not waiting for its own
  response, or the response arrives in that very cycle (`dmem_req = mem_memop && (!wb_memop ||
  dmem_rvalid)`). So the response always belongs to WB, and back-to-back accesses still run at
  one per cycle with a 1-cycle memory.
- Once raised, a data request stays unchanged until granted: MEM is frozen while it waits, and
  WB, which it depends on, can only drain.
- Byte enables: `be` from size and address; store data is replicated on all lanes
  (`store_lanes`), so no shifter is needed; RVFI masks the unwritten lanes.

### 6.3 Instruction port timing

See section 3. Up to `FETCH_DEPTH` requests may be outstanding, and responses may arrive with any
latency of at least one cycle.

## 7. Branch predictor and the stub

`rv_core` instantiates `bpu` (`rtl/bp/`) with `META_W = bp_pkg::meta_w(GHR_BITS, RAS_DEPTH)`.
With `+define+RV_BPU_STUB` it instantiates `bpu_stub` instead (and `META_W = 32`), so the core
builds and runs without `rtl/bp/`:

- `STATIC_NT`: never taken.
- `RANDOM` (simulation only): each cycle an LFSR chooses not taken, taken to the last target seen
  for this PC, taken near the PC (wrong-path code with real loads, stores and branches), or taken
  anywhere in RAM, sometimes misaligned. It also redirects ordinary instructions. The meta is the
  predicted PC, so the stub checks that each update carries the meta of its own instruction. The
  core must give the same results as with any other predictor, and the regression proves it.

## 8. CSR counters (rv_csr.sv)

`cycle`/`time` count every clock since reset, `instret` counts retirements, all 64 bits. Reads
happen in EX. Up to two older instructions are still in MEM and WB at that moment, and none of
them can be cancelled, so the read adds their number: `instret` is then exactly the number of
instructions retired before the reading one, as if one instruction ran at a time. This also makes
it equal to the ISS value. Any write, and any unknown CSR, is an illegal instruction (decoder),
so there is no write port. `mhartid` reads 0.

## 9. RVFI and the commit trace

The RVFI port is driven combinationally from the MEM/WB register in the retirement cycle
(`rvfi_valid = wb_valid && !wb_wait`). `rs1/rs2` addresses and data are 0 when the instruction
does not read them; `rd_addr` and `rd_wdata` are 0 when nothing is written; memory addresses are
word aligned with byte masks. The harness (`sim/main.cpp`) turns each retirement into one trace
line in the format of SPEC section 6 and ends the run on a retired store to TOHOST/HALT or a
retired trap.

## 10. Design choices

| Choice | Why | Alternative considered |
|---|---|---|
| Resolve all control flow in EX | One redirect source, older stages always complete, precise traps for free; penalty 2 cycles | JAL redirect in ID saves 1 cycle per mispredicted JAL; left out to keep one redirect path (a BTB predicts JAL anyway) |
| `pred_npc` compare instead of separate taken/target checks | One 32-bit comparator covers direction, target and non-CF redirects | Separate checks need extra cases for ordinary instructions |
| Fetch queue with bypass | Decouples memory latency from ID stalls without adding a cycle | A plain IF/ID register would need the memory to be stalled when ID stalls |
| Request depends only on flops | Redirect has a full cycle to reach `pc_q`; protocol rule "unchanged until grant" holds by construction | Requesting the redirect target in the redirect cycle saves 1 cycle but puts the whole EX compare on the address path |
| One data access in flight | The response always belongs to WB; simple and still 1 access/cycle at 1-cycle latency | Two in flight hides more latency, but needs a "granted" flag in MEM and response tracking |
| Update/redirect only when EX advances | Operands are final, update fires once | Resolving on the first EX cycle needs a "done" flag and care with forwarded values |
| Register file reset to 0 | RTL and ISS start from the same state | No reset saves area; start-up code zeroes registers anyway |
| `instret` corrected by in-flight count | Exact, matches the ISS | Raw count is legal but off by up to 2 |

## 11. Verification

- `make core-unit`: cocotb + Verilator unit tests for the ALU (all operations, edge values and
  random), the register file (reset, x0, random writes, bypass) and the decoder (20k random
  words biased to real opcodes, plus hand-picked encodings, against a reference written from the
  ISA manual, immediates cross-checked with the ISS).
- `make core-tests`: hand-written self-checking programs in `rtl/sim/tests/` (ALU, loads/stores
  of every width and sign, every branch taken/not taken, JAL/JALR/call/ret, hazards, FENCE.I with
  self-modifying code, counters, PUTCHAR/HALT, and one program per trap cause), run on every
  predictor variant, at 0/30/60 % random memory stalls, and compared line by line with the ISS.
- The same runner takes the riscv-tests ELFs (`rv32ui-p-*`) and any other ELF.
- Also run on the verification agent's programs: 9 benchmarks (CoreMark included) and 1200
  random programs from `scripts/rand_prog.py`, on all 7 variants with and without stalls,
  trace-identical to the ISS (14 Nov 2026).
- Built-in checks in every run: memory protocol (`sim_mem`), fetch counters (`rv_fetch`),
  load-use invariant and data responses (`rv_core`), predictor-port rules and meta plumbing
  (`bpu_stub`), predictor updates against retirement (`sim/main.cpp`).

Mutation check (14 Nov 2026): ten bugs injected one at a time into a copy of the core (no
load-use stall, swapped forwarding priority, no operand capture in a frozen EX, no response
drop after a redirect, no stale request, ungated `pred_fire`, no register-file bypass, FENCE.I
without flush, two data accesses in flight, non-control-flow mispredictions ignored) were all
caught by the regression. The weakest was the missing operand capture: it only shows up with
random stalls, in 5 of 236 runs.

Commands:

```
make lint                          # verilator -Wall, stub and real predictor, tb_top
make sim                           # all variants -> build/sim/<VARIANT>/Vtb_top
make core-unit                     # cocotb unit tests
make core-tests                    # asm tests x all variants x stalls 0/30/60 x 2 seeds, vs ISS
make run ELF=build/sw/bench/coremark.elf BP=GSHARE STALL=30 TRACE=build/cm.trace
.venv/bin/python rtl/sim/tests/run_tests.py --variants "GSHARE RANDOM" --stalls 0,40 \
    --cpi build/sw/riscv-tests/*.elf  # any ELF set
```

## 11b. Measured performance (1-cycle memory, no stalls)

CPI = cycles / retired instructions. The cycle counts agree exactly with the model "1 per
instruction + 2 per misprediction + 1 per load-use + a few cycles of start-up".

| Program | instret | STATIC_NT (stub, jumps also mispredicted) | STATIC_NT (bpu, BTB for jumps) | BIMODAL | GSHARE | TOURNAMENT |
|---|---|---|---|---|---|---|
| coremark | 1 460 932 | 1.445 | 1.376 | 1.158 | 1.169 | 1.155 |
| sort | 140 806 | 1.313 | 1.274 | 1.075 | 1.079 | 1.072 |
| fsm | 858 150 | 1.500 | 1.468 | 1.120 | 1.132 | 1.116 |
| crc | 173 449 | 1.116 | 1.107 | 1.015 | 1.013 | 1.012 |
| perf.S (core test) | 8 233 | 1.522 | 1.522 | - | 1.137 | - |

## 12. Limitations and next steps

- No JAL redirect in ID (optional in SPEC).
- One data access in flight: with a slow memory, back-to-back loads pay the full latency each.
- Wrong-path fetches may go anywhere; `sim_mem` returns 0 (illegal) outside RAM. A real system
  needs fetch to be side-effect free in the address map (true here).
- No speculative data access: see section 2 for what the Spectre phase needs.
