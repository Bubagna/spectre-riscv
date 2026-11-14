# Verification plan and status

How the core is checked, what each level catches, how to run it, and what is still not covered.
The contract being checked is `docs/SPEC.md`; the pipeline is described in `docs/ARCHITECTURE.md`
and the predictor in `docs/BRANCH_PREDICTOR.md`.

The central idea: **every retired instruction of the RTL is compared with a reference model**.
The RTL harness writes one trace line per retired instruction from the RVFI port; the ISS writes
the same line for the same program; a comparator checks them line by line. A bug that changes any
architectural result (a register value, a store, a load address, the control flow, a trap) shows
up at the first instruction where it matters, not at the end of the program.

## 1. Levels

| Level | What runs | What it catches | Command |
|---|---|---|---|
| ISS unit tests | `model/test_iss.py` (pytest): every instruction against a naive reference, edge operands, traps, MMIO, trace format, decoder legality against GNU objdump, a mutation check | bugs in the golden model itself | `make iss-test` |
| ISA tests | riscv-tests `rv32ui-p-*` (41 programs, custom env) on the ISS and on the RTL | wrong instruction semantics, in isolation | `make riscv-tests` |
| Lockstep trace compare | every program, RTL trace vs ISS trace (`scripts/cmp_trace.py`) | any architectural divergence, at the first wrong instruction | part of every regression |
| Random programs | `scripts/rand_prog.py`: constrained-random RV32I, one seed per program | forwarding, load-use, flush/redirect, RAS/BTB interaction bugs that hand-written tests miss | `make regress-random N=200` |
| Random stalls | `+stall=P` makes `sim_mem` drop `gnt` and delay `rvalid` at random | handshake and stall bugs: lost or duplicated requests, stalls that do not freeze a stage | `VSTALL=0,30,60` on any regression |
| Random predictor | `RANDOM` and `STUB_RANDOM` variants predict at random, seeded by `+seed` | recovery bugs: the core must be correct with any prediction, including absurd ones | `VBP=all` (default) |
| Benchmarks | `sw/bench/*.c` and CoreMark, self-checking, compared in lockstep too | bugs that need real compiled code (libgcc calls, deep recursion, long runs) + the CPI table | `make regress-bench` |
| Self-tests of the scripts | `scripts/test_verif.py`: comparator rules, 60 random seeds legal on the ISS, the runner catches an injected bug | a regression that cannot fail | `make verif-selftest` |

Other checks owned by the core and predictor areas (not described here): `make lint`,
`make core-tests`, `make core-unit` (`mk/sim.mk`), `make bp-test`, `make bp-accuracy` (`mk/bp.mk`).
The RTL harness also has built-in checkers (memory protocol, predictor-update consistency) that end
a run with `ERROR`, which the regression counts as a failure.

## 2. The pieces

### ISS (`model/iss.py`)

The golden model. Implements section 1 of the spec exactly: RV32I, FENCE as a NOP, FENCE.I, the
six Zicntr counters and `mhartid` read-only (any write attempt or other CSR is illegal), traps on
ECALL, EBREAK, illegal instructions, misaligned loads/stores and misaligned taken targets. Memory
map of section 2, ELF loader (PT_LOAD segments), trace of section 6, result and stats lines of
section 7 (`cycles = instret`, no mispredictions).

```
.venv/bin/python model/iss.py <elf> [--trace FILE] [--btrace FILE] [--max-instr N] [--quiet]
```

Exit status 0 PASS, 1 FAIL, 2 TRAP, 3 TIMEOUT, 4 ERROR (access outside RAM/MMIO, bad ELF), the
same as the RTL harness. Decisions on points the spec leaves open:

- `TOHOST`: 1 is PASS, any other value v is `FAIL v>>1`. `HALT`: 0 is PASS, any other value is
  `FAIL v`. The halting store retires and is in the trace.
- A trapping instruction retires (it is in the trace as `<pc> <insn> trap`, with no other field)
  and has no side effect: no register write, no memory access.
- FENCE and FENCE.I ignore their reserved fields, as the ISA requires of base implementations.
  Shift-immediates with `shamt[5] = 1` are illegal (RV32I).
- Counter reads return the number of instructions retired before the reading one; `cycle` =
  `time` = `instret` in the ISS.
- A store to RAM drops the decode-cache entry of the word it writes, so self-modifying code is
  always seen. FENCE.I is therefore a NOP in the ISS; the RTL must flush to match.

Speed on this Mac (M-series, Python 3.14): about 2 M instructions/s without trace, 0.6 M/s with
the commit trace. The decode cache keeps one closure per PC.

### Trace comparator (`scripts/cmp_trace.py`)

```
.venv/bin/python scripts/cmp_trace.py <rtl.trace> <iss.trace> [--elf prog.elf] [--context 6] [--strict]
```

Field by field: pc, instruction, trap flag, `csr` suffix, destination register and value, store
(word address, mask, data), load address. On lines with ` csr` the register value is ignored (the
register number is not). Store data is compared only on the byte lanes of the mask, since RVFI
leaves the other lanes undefined (the core replicates the data, the ISS writes zeros); `--strict`
compares the whole word. On a mismatch it prints which fields differ, the preceding matching
lines, both offending lines and the next line of each, disassembled with objdump when `--elf` is
given. Exit 0 match, 1 mismatch, 2 malformed trace.

### Random program generator (`scripts/rand_prog.py`)

```
.venv/bin/python scripts/rand_prog.py --seed N -o out.S [--budget 30000] [--blocks 60] [--no-wrong-path-traps]
```

Builds a program from random blocks, nested: ALU sequences and dependency chains, loads and
stores of every width at random aligned addresses, store-then-overlapping-load, pointer chasing
(`lw t, k(t)`), writes to x0 followed by reads of x0, if/else with data-dependent conditions,
branch chains and branch-to-next, counted loops (do-while and while forms, up to 3 deep, 1 to 40
trips, data-dependent early exits), calls through `ra` and `t0` to leaf and non-leaf functions
(a call DAG, so the depth is bounded), indirect calls (`jalr` with offsets, through function
tables, through the link register itself), a bounded recursion of depth 1 to 12 (deeper than the
4-entry RAS), jump tables with JALR offsets including the bit-0 clear, unreachable code after
unconditional jumps containing `ecall`/`ebreak`/illegal words (fetched on the wrong path, must
never retire), FENCE, FENCE.I, self-modifying code (store a new instruction, FENCE.I, execute
it), counter reads in all legal forms.

The invariants that make every program legal and deterministic are listed in the script's
docstring (reserved registers, counters only touched by their loop, addresses forced into the
data area by masking, counter values killed by the next instruction, pointer table never stored
to). `scripts/test_verif.py` checks 60 seeds end in PASS on the ISS; the regressions run hundreds
more. Programs average about 5,000-20,000 retired instructions.

### Regression runner (`scripts/regress.py`)

```
.venv/bin/python scripts/regress.py [--suite riscv-tests|bench|random ...] [--elf GLOB ...]
    [--random N --random-seed0 S] [--bp all|V1,V2] [--stall 0,30] [--seeds K] [-j J]
    [--iss-only] [--keep] [--summary FILE]
```

Runs the ISS once per ELF (golden trace cached in `build/regress/iss/`), then every
variant x stall x seed on the RTL (`build/sim/<VARIANT>/Vtb_top`), in parallel. Seeds only
multiply the runs where they change something (stall > 0 or a random predictor). A run passes
when the RTL result line equals the ISS one, the traces match and instret agrees. Failing runs
keep trace, log and comparator report under `build/regress/runs/`, and the summary gives the exact
rerun and compare commands. The summary (`build/regress/*.md`) has the pass table and, for the
benchmarks, cycles / CPI / misprediction rates per variant at stall 0.

`scripts/mock_sim.py` is a stand-in simulator that runs the ISS and can inject a bug
(`MOCK_BUG=n`); it is only there to test the runner.

### Coverage (`scripts/coverage.py`)

```
.venv/bin/python scripts/coverage.py build/sw/random/*.elf [--gaps]
```

Architectural functional coverage from ISS traces: every mnemonic, branch type x outcome, store
width x byte mask, read-after-write at distance 1, 2, 3 by producer (ALU, load, jump, CSR) and
consumer (ALU, branch, address, store data, JALR), x0 written then read, load of a word stored in
the last 3 instructions, calls deeper than 4, execution of a stored word, counter reads. On 200
random programs (seeds 1-200): **101/101 expected bins hit**, max call depth 14. This is how the
pointer-chasing block was found to be missing (load to address at distance 1 and 2 had zero hits
before it was added).

### Software (`sw/`, `mk/sw.mk`)

- `sw/common/`: `crt0.S` (zero registers, sp, gp, clear .bss, call main, store the return value
  to HALT), `link.ld` (1 MiB RAM at 0x8000_0000, stack at the top, link fails if less than 64 KiB
  is left for it), `io.c`/`io.h` (`printf_` on PUTCHAR with `%d %i %u %x %X %c %s %p %%`, widths
  and `0` padding; `memset`/`memcpy`/`memmove`/`memcmp`/`strlen`), `common.mk` (flags).
- riscv-tests are **cloned, not vendored**, at a pinned commit (`3df703cd`, 2026-10-19) into
  `build/third_party/riscv-tests` by `make riscv-tests-elf` (BSD licence in that directory). Only
  `isa/rv32ui`, `isa/rv64ui` and `isa/macros` are used; the upstream environment is replaced by
  `sw/riscv-tests-env/riscv_test.h`: no CSR or trap setup, all registers zeroed, PASS stores 1 to
  TOHOST, FAIL stores `(TESTNUM<<1)|1`. Cloning keeps third-party code out of the repo and the
  pinned commit keeps the build reproducible; the cost is that the first build needs the network.
  41 ELFs in `build/sw/riscv-tests/`, every `rv32ui-p-*` except `ma_data` (misaligned accesses
  trap by design, and there is no handler to emulate them).
- Benchmarks in `sw/bench/`, each self-checking (exit code through HALT):

| Program | What it stresses | Retired instructions |
|---|---|---:|
| `sort` | quicksort (recursion) + insertion sort on random data | 141 k |
| `bsearch` | binary search, one unpredictable branch per step | 328 k |
| `fib` | recursive Fibonacci (built at -O1 so each call is a real call) and Ackermann | 370 k |
| `sieve` | sieve of Eratosthenes: regular loops + one data-dependent branch | 544 k |
| `crc` | bitwise CRC-32 (GCC makes it branch-free) vs table CRC: a low-branch control | 173 k |
| `interp` | register-machine interpreter, `switch` dispatch through a jump table | 824 k |
| `fsm` | tokenizer state machine over random text | 858 k |
| `mix` | Dhrystone-like: small functions, struct copy, strings, linked list, libgcc mul/div | 43 k |
| `coremark` | EEMBC CoreMark, 2 iterations, 2K performance seeds, CRCs validated | 1.46 M |

CoreMark is cloned at a pinned commit (`1f483d5b`) into `build/third_party/coremark` and built
unmodified with the port in `sw/bench/coremark/` (Apache 2.0 for the code, EEMBC terms in its
`LICENSE.md`). The port reads no counter, because a counter value flowing into the program would
make the RTL and ISS traces diverge: the timer is 0, so CoreMark prints its "must run 10 s" error
and "Errors detected". The exit code counts only the CRC and data-type checks, so `PASS` means
"CRCs validated". Cycle counts come from the simulator stats line. These are not official
CoreMark scores. A mutated ISS (`xori` decoded as `ori`) makes it fail all three CRCs, so the check is real.

### Branch traces (`scripts/branch_trace.py`)

```
make btrace                    # benchmarks -> build/btrace/<name>.btrace
.venv/bin/python scripts/branch_trace.py <elf>... [-o DIR]
.venv/bin/python model/iss.py <elf> --btrace FILE
```

One line per retired control-flow instruction, in program order, the input format of
`tb/bp/trace_sim.py` (`docs/BRANCH_PREDICTOR.md`, section 11):

```
<pc 8 hex> <kind 1 dec> <taken 0/1> <target 8 hex>
```

kind is the `upd_kind_i` encoding (1 branch, 2 jal, 3 jalr, 4 call, 5 ret; call = jal/jalr with
rd in {x1, x5}, ret = jalr with rs1 in {x1, x5} and rd = x0). For a conditional branch the target
is pc+imm whether or not it was taken; jumps are always taken and target is where they went.
Instructions that trap are not in the trace.

## 3. Status (2026-11-14)

All counts below are runs actually executed, with the real `bpu` integrated. The RTL binaries
were rebuilt by the core area during the campaigns (13:06, 13:15, 14:03), so the rows do not all
refer to the same RTL revision; rerun `make regress` after each RTL change.

| Campaign | Runs | Result |
|---|---:|---|
| ISS unit tests + riscv-tests on the ISS | 135 tests | 135 pass |
| riscv-tests on the ISS | 41 | 41 PASS |
| Script self-tests | 73 tests | 73 pass |
| riscv-tests, 7 variants, stall 0/30, 2 seeds | 943 | 943 match |
| 200 random programs, 7 variants, stall 0/40 | 2,800 | 2,800 match |
| 1000 random programs (seeds 10000-10999), GSHARE/TOURNAMENT/RANDOM/STUB_RANDOM, stall 0/50 | 8,000 | 8,000 match |
| 8 benchmarks, 7 variants, stall 0/30 | 112 | 112 match |
| CoreMark, 7 variants, stall 0/30 | 14 | 14 match |
| `make regress N=300 VSTALL=0,40 VSEEDS=2` (riscv-tests + 9 benchmarks + 300 random, current generator), 7 variants | 8,050 | 8,050 match |

The 200- and 1000-program campaigns used the generator before the pointer-chasing block was
added; the `make regress` run uses the current one.

No RTL bug was found by these campaigns: the core passed the first time it was run against them.
That is good news but also a reason for suspicion, so the infrastructure itself is tested: the
comparator against hand-made faulty traces, the runner against a simulator with an injected
register-value bug (`MOCK_BUG`), the riscv-tests and CoreMark against a mutated ISS. All of them
fail as they should.

Benchmark performance at stall 0 (cycles / CPI from the RTL stats line) is in
`build/regress/bench.md` and `build/regress/coremark.md`. CoreMark CPI: TOURNAMENT 1.155,
BIMODAL 1.158, GSHARE 1.169, STATIC_NT 1.376, RANDOM 1.383.

## 4. Coverage gaps (known, not yet closed)

- **No microarchitectural coverage.** The coverage script measures what the programs do, not what
  the pipeline saw: there is no count of forwarding-mux selections, load-use stalls, flushes per
  cause, stall cycles per stage, BTB hits on non-branches. That needs counters or cover points in
  the RTL (or SystemVerilog `cover property` with Verilator `--coverage`).
- **Wrong-path behaviour is only checked through its effects.** A wrong-path instruction that
  writes nothing visible but corrupts internal state would be caught only if it later changes a
  retired result.
- **Load byte offset is not in the trace.** `ld` carries the word address (RVFI style); a load
  from the wrong byte of the right word is caught only through the loaded value, so a wrong byte
  holding the same value goes unnoticed.
- **Counter values are not checked.** Lines with ` csr` ignore the value. `instret` could be
  compared exactly (the core matches the ISS); `cycle` can only be checked for monotonicity.
  Neither is done yet.
- **No interrupts, no privilege, no misaligned-access emulation**: out of scope for phase 1;
  `ma_data` is excluded for that reason.
- **No formal verification.** The RVFI port is there for riscv-formal, not yet attached.
- **No independent second reference.** The ISS is checked against riscv-tests, a naive in-test
  reference and objdump's decoder, not against Spike (not installed). A shared misreading of the
  spec by the ISS and the RTL would not be caught by lockstep.
- **Random programs are short** (thousands of instructions): predictors barely warm up, so they
  test correctness, not prediction quality. The benchmarks cover the latter.
- **Self-modifying code is limited** to patching a straight-line ALU instruction ahead of the
  store; patching a branch, a jump, or code inside a running loop is not generated.
