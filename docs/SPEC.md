# Core spec (shared contract)

This file is the contract between the parts of the project. Every module, harness and script must
follow it. If something here is wrong or missing, do not silently diverge: note it in your report
and in the "Open questions" section at the bottom.

Phase 1 goal: a 5-stage in-order RV32I core with a dynamic branch predictor, verified against a
reference model. Spectre, caches and the M extension come later; design so they can be added.

## 1. ISA

- **RV32I** base, all 37 instructions (no compressed, no M yet).
- **FENCE**: NOP. **FENCE.I**: flush the pipeline and refetch from pc+4 (needed for self-modifying
  code in `rv32ui-p-fence_i`; memory is unified and uncached, so a flush is enough).
- **Zicsr subset, read-only counters** (Zicntr): `cycle` (0xC00), `time` (0xC01, = cycle),
  `instret` (0xC02), `cycleh` (0xC80), `timeh` (0xC81), `instreth` (0xC82). All six CSR
  instruction forms decode; any write attempt (csrrw, or csrrs/csrrc with rs1 != x0, imm forms with
  uimm != 0) to these, or any other CSR address, is an illegal instruction. `mhartid` (0xF14)
  reads 0.
- **No privilege modes, no interrupts, no trap handler.** ECALL, EBREAK, illegal instructions,
  misaligned loads/stores and misaligned jump/branch targets cause a *trap*: the instruction
  retires with `rvfi_trap = 1`, the core stops (`halt_o = 1`) and the simulation ends with an
  error. This is enough for riscv-tests with the custom env (section 4).

## 2. Memory map and memory interface

| Address | What |
|---|---|
| `0x8000_0000` – `0x800F_FFFF` | RAM, 1 MiB, unified code + data. Reset PC = `0x8000_0000`. |
| `0x1000_0000` | `TOHOST` (word store): value 1 = PASS, any other odd value v = FAIL with test number v>>1 (riscv-tests convention). Ends the simulation. |
| `0x1000_0004` | `PUTCHAR` (byte/word store): low byte goes to stdout. |
| `0x1000_0008` | `HALT` alias of TOHOST kept for C programs: store exit code; 0 = success. |

Loads from MMIO return 0. Accesses outside RAM and MMIO are an error in the simulator.

Two memory ports (Harvard ports into one dual-port memory). Both use the same simple
request/response protocol so caches and random stalls can be added without touching the pipeline:

```
// instruction port (read only)
output logic        imem_req_o;    // request valid
output logic [31:0] imem_addr_o;   // word aligned
input  logic        imem_gnt_i;    // request accepted this cycle (req && gnt = handshake)
input  logic        imem_rvalid_i; // response valid (one per accepted request, in order)
input  logic [31:0] imem_rdata_i;

// data port
output logic        dmem_req_o;
output logic [31:0] dmem_addr_o;   // word aligned; byte lanes from be
output logic        dmem_we_o;
output logic [3:0]  dmem_be_o;
output logic [31:0] dmem_wdata_o;  // already shifted to the byte lanes
input  logic        dmem_gnt_i;
input  logic        dmem_rvalid_i; // for loads and stores
input  logic [31:0] dmem_rdata_i;  // full word; the core extracts and extends
```

The simulation memory (`rtl/sim/sim_mem.sv`) answers with a latency of 1 cycle by default and has
a parameter / plusarg for **random stalls** (random `gnt` low and random extra response latency).
The core must be correct under random stalls; this is a required test, not an option.

## 3. Pipeline (rtl/core/)

- Stages IF, ID, EX, MEM, WB. Full forwarding EX/MEM→EX and MEM/WB→EX, load-use stall, register
  file write in the first half / bypass so WB→ID needs no stall.
- Branches, JAL and JALR resolve in EX (JAL may be redirected earlier in ID as an optimisation,
  optional). A misprediction flushes the younger instructions and redirects fetch.
- Fetch uses the branch predictor every cycle (section 5). The core must be functionally correct
  with **any** predictor, including one that predicts at random; the predictor only changes speed.
- Top module: `module rv_core #(parameter BP_TYPE = "GSHARE", ...) (input clk_i, input rst_ni, ...)`
  with the two memory ports, `output logic halt_o`, the RVFI port (section 6) and the predictor
  update port exported for statistics (`bp_upd_*`, read only for the harness).
- Coding style: SystemVerilog, synthesizable subset, `always_ff`/`always_comb`, `logic`, packed
  structs, typedef enums and one package `rv_pkg` are fine; no interfaces, no classes, no `initial`
  in synthesizable code. Must also go through **sv2v + Yosys** later, so avoid exotic features.
  Names: snake_case, ports `_i`/`_o`, registers `_q` with next value `_d`, active-low asynchronous
  reset `rst_ni`. Clock `clk_i`. `verilator --lint-only -Wall` must be clean (waivers only with a
  comment saying why).
- Comments explain *why*, not *what*. Every module has a header comment with its role and timing.

## 4. Software (sw/)

- Toolchain: Homebrew `riscv64-elf-gcc` 16.2 (`/opt/homebrew/bin/riscv64-elf-*`), always with
  `-march=rv32i -mabi=ilp32`. libgcc for rv32i/ilp32 is available (`-lgcc`). No newlib: programs
  are bare metal (`-nostdlib -nostartfiles`, own `crt0.S`, own `printf`-lite on PUTCHAR).
- Linker script `sw/common/link.ld`: everything at 0x8000_0000, stack at the top of RAM.
- riscv-tests: `rv32ui-p-*` built with a **custom env** in `sw/riscv-tests-env/` (`riscv_test.h`
  that drops all CSR/trap setup and signals pass/fail by storing to TOHOST). `ma_data` is excluded
  (misaligned accesses trap by design).
- Programs are passed to simulators as **ELF files**; the RTL harness and the ISS both load PT_LOAD
  segments themselves. No hex conversion step.

## 5. Branch predictor (rtl/bp/)

Module `bpu`, parameters `BP_TYPE` (`"STATIC_NT"`, `"BIMODAL"`, `"GSHARE"`, `"TOURNAMENT"`,
sim-only `"RANDOM"`), `BTB_ENTRIES` (default 64), `BHT_ENTRIES` (default 512), `GHR_BITS` (default
9), `RAS_DEPTH` (default 4, 0 = no RAS), `META_W` (derived, exported via a function or localparam).

```
// predict: combinational lookup with the PC that IF is fetching this cycle
input  logic                pred_valid_i,
input  logic [31:0]         pred_pc_i,
output logic                pred_taken_o,    // redirect fetch to pred_target_o next cycle
output logic [31:0]         pred_target_o,
output logic [META_W-1:0]   pred_meta_o,     // carried down the pipeline with the instruction
input  logic                pred_fire_i,     // the prediction was used (fetch advanced); updates speculative state (GHR, RAS)

// update: from EX when a control-flow instruction resolves (exactly once per such instruction, in order)
input  logic                upd_valid_i,
input  logic [31:0]         upd_pc_i,
input  logic [2:0]          upd_kind_i,      // 0 none, 1 branch, 2 jal, 3 jalr, 4 call, 5 ret (call = jal/jalr with rd in {x1,x5}; ret = jalr rs1 in {x1,x5}, rd = x0)
input  logic                upd_taken_i,
input  logic [31:0]         upd_target_i,
input  logic                upd_mispredict_i, // direction or target wrong: restore speculative state from upd_meta_i
input  logic [META_W-1:0]   upd_meta_i,

// flush: younger predictions discarded for a reason other than a misprediction of this instruction (e.g. FENCE.I, trap)
input  logic                flush_i,
input  logic [META_W-1:0]   flush_meta_i
```

- The BTB stores target and kind, so IF knows "this PC is a branch / call / ret" before decode.
- Speculative global history and RAS are checkpointed in `pred_meta_o` and repaired from
  `upd_meta_i` on a misprediction. Repair must be exact for the GHR; for the RAS, top-pointer
  (+ top entry) repair is enough.
- Storage in flops for now (small tables); keep the table access in one place so it can become an
  SRAM macro later.

## 6. Commit trace: RVFI port and trace format

The core exports a **RVFI** port (riscv-formal names, NRET = 1), so riscv-formal can be attached
later: `rvfi_valid, rvfi_order[63:0], rvfi_insn, rvfi_trap, rvfi_halt, rvfi_intr(=0), rvfi_mode(=3),
rvfi_ixl(=1), rvfi_rs1_addr, rvfi_rs2_addr, rvfi_rs1_rdata, rvfi_rs2_rdata, rvfi_rd_addr,
rvfi_rd_wdata, rvfi_pc_rdata, rvfi_pc_wdata, rvfi_mem_addr, rvfi_mem_rmask, rvfi_mem_wmask,
rvfi_mem_rdata, rvfi_mem_wdata`. Per the RVFI spec, `rvfi_rd_wdata` is 0 when `rvfi_rd_addr` is 0,
and `rvfi_mem_addr` is the word-aligned address with masks on byte lanes.

Both the RTL harness and the ISS write the **same text trace**, one line per retired instruction:

```
<pc 8 hex> <insn 8 hex>[ x<rd 2 dec> <wdata 8 hex>][ st <addr 8 hex> <wmask 1 hex> <wdata 8 hex>][ ld <addr 8 hex>][ trap]
```

Examples: `80000004 00100093 x01 00000001`, `80000010 00112623 st 80001ffc c 00010000`
(byte-lane format: word address, mask, data already in lanes). Lowercase hex, single spaces, rd
field only if rd != 0. CSR reads of the counters get the suffix ` csr` so the comparator ignores
their value (cycle counts differ between RTL and ISS).

## 7. Directory layout and ownership

| Path | Contents |
|---|---|
| `docs/` | `SPEC.md` (this), `ARCHITECTURE.md` (pipeline explained, diagrams), `VERIFICATION.md` |
| `rtl/core/` | pipeline, `rv_pkg.sv`, decoder, ALU, register file, CSR counters |
| `rtl/bp/` | branch predictor |
| `rtl/sim/` | simulation-only: `sim_mem.sv`, `tb_top.sv` (core + memory + MMIO) |
| `sim/` | Verilator C++ harness: ELF loader, trace writer from RVFI, stats, plusargs |
| `tb/` | cocotb unit tests (`tb/core/`, `tb/bp/`); `tb/counter` is an old exercise, leave it |
| `model/` | Python ISS (`model/iss.py`), the golden reference |
| `sw/` | crt0, link script, riscv-tests env and build, test programs, benchmarks |
| `scripts/` | trace comparator, random program generator, regression runner |
| `mk/` | one make fragment per area (`sim.mk`, `sw.mk`, `bp.mk`, `verif.mk`), included by `Makefile` |
| `build/` | all generated files (ignored by git) |

Simulator command (provided by `mk/sim.mk`):
`build/sim/Vtb_top +elf=<file> [+trace=<file>] [+max_cycles=N] [+stall=<0..100>] [+seed=N] [+bp=<type>]`
(`+bp` only if the predictor type is runtime-selectable; otherwise one binary per type,
`build/sim/<type>/Vtb_top`). At the end it prints `PASS`/`FAIL n`/`TIMEOUT`/`TRAP` and a stats line:
`cycles=… instret=… cpi=… branches=… br_mispred=… jumps=… jump_mispred=…`.

ISS command: `.venv/bin/python model/iss.py <elf> [--trace <file>] [--max-instr N]`, same final lines.

## Rules for everyone

- Do not commit to git (Luigi commits himself). Do not touch files owned by another area; if you
  need a change there, write it in your report.
- Everything generated goes in `build/`.
- Use the repo venv: `.venv/bin/python`, `.venv/bin/pip`.

## Open questions

(add here, with your area and date)

**[bp, 2026-11-14] Branch predictor: points the interface leaves open.** Implemented in
`rtl/bp/bpu.sv` as follows; details in `docs/BRANCH_PREDICTOR.md` section 2. The port list is
unchanged.

1. *Non-control-flow instruction predicted taken* (stale BTB entry after self-modifying code, as
   in `rv32ui-p-fence_i`; `RANDOM` does it on purpose on ~1/16 of BTB misses). Section 5 only
   defines updates for control-flow instructions. Proposal: the core redirects to pc+4 and sends
   one update with `upd_kind_i = 0`, `upd_taken_i = 0`, `upd_mispredict_i = 1` and the
   instruction's meta. The bpu restores GHR/RAS with no effect of the instruction and invalidates
   the BTB entry. (`flush_i` with its meta also repairs GHR/RAS but keeps the stale entry.)
2. *No same-cycle bypass.* In a cycle with `flush_i` or `upd_valid_i && upd_mispredict_i`,
   `pred_fire_i` is ignored (the instruction fetched then is wrong path); fetch the redirect target
   from the next cycle on. Priority: flush > mispredict > fire.
3. *`flush_meta_i`* is the meta of the instruction at which the flush happens (FENCE.I, trap);
   GHR/RAS go back to the state before it. If flush and an update come in the same cycle, the
   core must only assert the one that belongs to the older instruction.
4. *`pred_target_o`* is the predicted next PC: the target if taken, pc+4 otherwise.
   `upd_target_i` is only read when `upd_taken_i = 1`.
5. *META_W*: `bp_pkg::meta_w(GHR_BITS, RAS_DEPTH)`, independent of `BP_TYPE` (41 with the
   defaults). Compile `rtl/bp/bp_pkg.sv` first (`BP_SRCS` in `mk/bp.mk` is in order).
6. *JAL redirected in ID* (optional in section 3): the younger instructions it squashes have
   already fired, so GHR/RAS need a restore at ID that replays the JAL's own effect (a call must
   push). `flush_i` does not replay, and sending the JAL's update from ID would break "updates in
   program order" whenever an older control-flow instruction has not resolved yet. Proposal: no
   ID redirect in phase 1; JAL resolves in EX like the others.

**[core, 2026-11-14] Core and RTL harness: assumptions and clarifications.** Implemented in
`rtl/core/`, `rtl/sim/`, `sim/`; details in `docs/ARCHITECTURE.md`. Port lists unchanged.

1. *Memory protocol, made explicit.* (a) A request that is not granted stays on the port with the
   same address/we/be/wdata until it is granted (OBI rule); `sim_mem` checks it. (b) The response
   comes at least one cycle after the grant (no same-cycle response). (c) `gnt` may depend
   combinationally on `req`; `rvalid` must not. (d) `dmem_req_o` depends combinationally on
   `dmem_rvalid_i` (MEM may request in the cycle WB gets its response), so a memory must not
   make `rvalid` depend on `req`. (e) `dmem_be_o` is also driven for loads (the lanes read).
   (f) `dmem_wdata_o` replicates the store data on every lane; `be` selects the lanes written.
2. *Trace `ld` address* is the word-aligned address, like `st` (the ISS does the same). Section 6
   only shows a store example.
3. *`instret` reads* return exactly the number of instructions retired before the reading one
   (the core adds the older instructions still in MEM/WB), so they equal the ISS value. `cycle`
   still differs; lines keep the ` csr` suffix.
4. *Harness end lines.* Same exit codes as the ISS (0 PASS, 1 FAIL, 2 TRAP, 3 TIMEOUT, 4 ERROR).
   `ERROR <reason>` is printed when a built-in checker fires (memory protocol, access outside
   RAM/MMIO, predictor-update mismatch, a `$error` assertion). Branch/jump statistics count only
   updates of instructions that retire. When non-zero, an extra line
   `info: nocf_mispred=N` counts `upd_kind = 0` repairs (not in branches/jumps).
5. *Simulator binaries* are `build/sim/<VARIANT>/Vtb_top` (no `+bp`): STATIC_NT, BIMODAL, GSHARE,
   TOURNAMENT, RANDOM use `rtl/bp/bpu`; STUB_STATIC_NT and STUB_RANDOM use `rtl/core/bpu_stub`.
   Waveforms: `+vcd=<file>` (VCD; FST would need lz4 flags on this machine).
6. *Wrong-path instruction fetches* may go anywhere; `sim_mem` returns 0 (an illegal instruction)
   for fetches outside RAM instead of failing, so such a fetch only matters if it would retire,
   and then it traps.
7. *For the Spectre phase:* in this pipeline nothing younger than an unresolved branch reaches MEM
   (branches resolve in EX, loads access memory in MEM), so **no wrong-path load ever accesses
   data memory** and a Spectre v1 gadget cannot leak through it as built. The attack phase needs a
   longer speculation window (e.g. branch resolution later than load issue, or speculative cache
   fills from an earlier stage). Decision needed before phase 2.
8. *Predictor contract as implemented by the core* (matches the [bp] entry above): `pred_fire_i`
   only on a handshake of a new fetch and never in a redirect cycle; updates when the instruction
   leaves EX, once, in program order; `upd_kind = 0` (taken 0, mispredict 1) for a mispredicted
   non-control-flow instruction; a trap sends `flush_i` (no update); FENCE.I sends `flush_i`
   unless it was mispredicted, in which case only the update is sent; never both in one cycle.
   No JAL redirect in ID.
9. *Make knobs shared between fragments.* `BP` and `STALL` are used by both `mk/sim.mk` (`make run`,
   one variant, one percentage) and `mk/verif.mk` (lists). Fragments are included in alphabetical
   order, so a `?=` default in `sim.mk` silently overrode `verif.mk`'s. Fixed in `sim.mk`: `make
   run` reads them only from the command line or the environment. Suggest that fragments prefix
   their knobs (e.g. `VERIF_BP`) or define no global defaults for shared names.

**[verif, 2026-11-14] Verification and software: decisions on points the spec leaves open.**
Details in `docs/VERIFICATION.md`.

1. *`-march`.* Section 4 says `-march=rv32i`, but GCC 16 rejects `fence.i` and CSR instructions
   without `_zicsr_zifencei`. `sw/` uses `-march=rv32i_zicsr_zifencei -mabi=ilp32`: same rv32i/ilp32
   multilib, no extra instruction can be emitted. Proposal: write that in section 4.
2. *End values.* `TOHOST`: 1 is PASS, any other value v is `FAIL v>>1` (even values too).
   `HALT`: 0 is PASS, any other value is `FAIL v`. The ISS and the harness agree.
3. *Result lines.* Section 7 lists PASS / FAIL n / TIMEOUT / TRAP; both simulators also print
   `ERROR <reason>` (exit 4) for simulator errors. Proposal: add it to section 7.
4. *Store data lanes.* RVFI leaves the bytes outside `wmask` undefined: the core replicates, the
   ISS writes zeros. The comparator compares only the masked lanes (`--strict` for all).
5. *Trap lines* carry no other field (`<pc> <insn> trap`): a trapping instruction has no side
   effect. Same in ISS and harness.
6. *Reserved fields.* FENCE / FENCE.I ignore their reserved fields (legal, as the ISA requires of
   base implementations); OP-IMM shifts with `shamt[5] = 1` are illegal. The RTL decoder should do
   the same; the random generator never emits these encodings, the ISS unit tests do.
7. *Branch traces* use exactly the format of `docs/BRANCH_PREDICTOR.md` section 11 (not-taken
   branches carry the branch target).
8. *Load byte offset.* The `ld` field is the word address, so the trace cannot show a load from
   the wrong byte of the right word when both bytes hold the same value. Proposal for later: add
   the read mask (`ld <addr> <rmask>`), in harness and ISS together.
