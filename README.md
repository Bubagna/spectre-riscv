# spectre-riscv

A 5-stage in-order RV32I core in SystemVerilog, with a dynamic branch predictor, checked
instruction by instruction against a reference model in Python.

The long-term plan is in the name: make the core speculate further, run a Spectre v1 attack on it,
then fix it in hardware and measure what the fix costs. This first part is the core and the
predictor, and the verification around them.

## What is in it

```
        +----+    +----+    +----+    +-----+    +----+
  PC -> | IF | -> | ID | -> | EX | -> | MEM | -> | WB |
        +----+    +----+    +----+    +-----+    +----+
          ^                   |
          | predict           | resolve branch, update predictor,
         BPU <----------------+ flush and redirect on a mispredict
```

The core:

- RV32I base ISA, plus the read-only counters `cycle`, `time` and `instret` (and their high halves).
- 5 stages with full forwarding. A load followed by a use costs 1 stall cycle.
- Branches and jumps resolve in EX, so a mispredict costs 2 cycles.
- FENCE.I flushes the pipeline, so self-modifying code works.
- No traps or privilege modes. ECALL, EBREAK, illegal instructions and misaligned accesses stop
  the core.
- Instruction and data ports use a req/gnt/rvalid handshake. The memory can stall at any cycle and
  answer late, so caches can be added later without touching the pipeline.
- A RVFI port (the riscv-formal interface), so riscv-formal can be plugged in.

The branch predictor (`rtl/bp/`):

- 64-entry BTB that stores the target and the kind of instruction (branch, jump, call, return), so
  fetch knows what a PC is before decode.
- Direction predictor chosen by a parameter: static not-taken, bimodal, gshare, or tournament
  (bimodal + gshare + chooser).
- Return address stack and a small table for indirect jumps.
- The global history and the return stack are updated speculatively at fetch, and repaired from a
  checkpoint carried down the pipeline with each instruction when a branch was mispredicted.
- For testing there is also a RANDOM predictor (simulation only) that is wrong most of the time on
  purpose: it predicts random directions, sends ordinary instructions to random targets and
  corrupts jump targets. The core has to give the same results with it.

## Verification

Every run writes a commit trace (one line per retired instruction: PC, instruction, register
written, store address and data). The same program runs on the Python ISS (`model/iss.py`), and
`scripts/cmp_trace.py` compares the two traces line by line.

What runs in the regression (`make regress`), on 7 predictor variants, with the memory stalling
0% and 40% of the time and several random seeds:

- the 41 `rv32ui-p-*` tests from riscv-tests (all except `ma_data`, since misaligned accesses
  stop the core by design), built with a small custom test environment;
- 9 benchmarks in C: CoreMark, sort, binary search, recursive fib, sieve, CRC, a state machine,
  a small bytecode interpreter and a mix;
- random programs from `scripts/rand_prog.py`, which go after the things that break pipelines and
  predictors (dependency chains, load-use, all load/store widths, data-dependent branches,
  nested loops, deep calls and returns, jump tables, FENCE.I).

The last full run had 350 programs and every configuration passed. Other checks:

- cocotb unit tests for the ALU, the register file and the decoder;
- for the predictor, a bit-exact Python model run in lockstep with the RTL, and long random
  streams of predict/update/flush, for every predictor type and 5 sets of table sizes;
- mutation tests. Bugs injected on purpose in the predictor (15) and in the core (10) were all
  caught by the tests. When a bug in the ISS was injected instead, the riscv-tests and the
  CoreMark checks failed as they should.

What is not done yet: formal verification, coverage of the microarchitecture (which hazards and
forwarding paths were actually hit), and a cross-check of the ISS against Spike.

## Results

CPI (cycles per instruction) with a 1-cycle memory and no stalls:

| Benchmark | Static not-taken | Bimodal | Gshare | Tournament |
|---|---:|---:|---:|---:|
| CoreMark | 1.376 | 1.158 | 1.169 | 1.155 |
| sort | 1.274 | 1.075 | 1.079 | 1.072 |
| bsearch | 1.369 | 1.197 | 1.211 | 1.197 |
| fib | 1.092 | 1.081 | 1.032 | 1.033 |
| sieve | 1.429 | 1.072 | 1.082 | 1.069 |
| crc | 1.107 | 1.015 | 1.013 | 1.012 |
| fsm | 1.468 | 1.120 | 1.132 | 1.116 |
| interp | 1.139 | 1.117 | 1.118 | 1.117 |
| mix | 1.313 | 1.162 | 1.141 | 1.129 |

CoreMark here is the unmodified CoreMark source with a bare-metal port. It runs a fixed number of
iterations and checks its CRCs, but it is not timed, so these are not official CoreMark scores.

One open problem: on CoreMark about 23% of conditional branches are still mispredicted, and gshare
does worse than bimodal. That is too high. My guess is that the 64-entry BTB is too small, since a
taken branch that misses in the BTB is predicted not-taken whatever the direction predictor says.
I am measuring it now.

## Running it

You need Verilator 5, a RISC-V GCC (`brew install riscv64-elf-gcc` on macOS; any
`riscv64-*-elf-gcc` with the rv32i multilib works) and Python 3.

```
python3 -m venv .venv && .venv/bin/pip install cocotb pytest
make sim                   # builds one simulator per predictor in build/sim/<TYPE>/Vtb_top
make riscv-tests-elf       # clones riscv-tests at a pinned commit and builds rv32ui
make bench-elf             # builds the benchmarks (clones CoreMark the first time)
make regress               # full regression, summary in build/regress/summary.md
make run ELF=build/sw/bench/coremark.elf BP=GSHARE STALL=30
```

Other targets: `make lint`, `make core-tests`, `make core-unit`, `make bp-test`, `make bp-mutate`,
`make bp-accuracy`, `make iss-test`. The simulator takes `+elf=`, `+trace=`, `+stall=0..100`,
`+seed=`, `+max_cycles=` and `+vcd=`.

## Layout

| Path | What |
|---|---|
| `rtl/core/` | pipeline, decoder, ALU, register file, counters |
| `rtl/bp/` | branch predictor |
| `rtl/sim/` | simulation memory with random stalls, testbench top, directed assembly tests |
| `sim/` | Verilator C++ harness: ELF loader, trace writer, statistics |
| `model/` | Python ISS and its tests |
| `sw/` | startup code, linker script, riscv-tests environment, benchmarks |
| `scripts/` | trace comparator, random program generator, regression runner |
| `tb/` | cocotb unit tests for the core and the predictor |
| `docs/` | `SPEC.md` (interfaces and conventions), `ARCHITECTURE.md`, `BRANCH_PREDICTOR.md`, `VERIFICATION.md` |

## Next

- Find out why the misprediction rate is so high, and size the BTB and the tables with numbers.
- Synthesis with Yosys on an open standard-cell library (area, max frequency, critical path), then
  OpenROAD.
- riscv-formal on the RVFI port.
- Caches and a longer speculation window. Right now branches resolve before any younger load
  reaches memory, so a Spectre v1 gadget cannot leak anything on this core. That has to change
  before the attack.
