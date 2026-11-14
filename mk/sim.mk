# Core RTL simulation (owner: core). See docs/SPEC.md sections 3, 6, 7 and docs/ARCHITECTURE.md.
#
#   make lint             verilator --lint-only -Wall: core with the stub and with every real
#                         predictor type, and tb_top
#   make sim              build every simulator variant that exists -> build/sim/<VARIANT>/Vtb_top
#   make sim-<VARIANT>    one variant:
#                           STATIC_NT BIMODAL GSHARE TOURNAMENT RANDOM  real bpu (rtl/bp/), when it
#                                                                       exists; without it,
#                                                                       STATIC_NT and RANDOM use
#                                                                       the stub
#                           STUB_STATIC_NT STUB_RANDOM                  always rtl/core/bpu_stub.sv
#   make run ELF=<file> [BP=<VARIANT>] [STALL=0..100] [SEED=N] [TRACE=<file>] [MAX_CYCLES=N]
#            [VCD=<file>]
#   make core-tests       assemble rtl/sim/tests/*.S and run them on every variant, with and
#                         without random memory stalls, and against the ISS when it exists
#   make core-unit        cocotb unit tests of decoder, ALU and register file (tb/core/)

VERILATOR ?= verilator
SIM_OUT   := $(BUILD)/sim

CORE_SRCS := $(addprefix $(ROOT)/rtl/core/,rv_pkg.sv rv_decoder.sv rv_alu.sv rv_regfile.sv \
               rv_csr.sv rv_fetch.sv rv_core.sv)
STUB_SRCS := $(ROOT)/rtl/core/bpu_stub.sv
TB_SRCS   := $(ROOT)/rtl/sim/sim_mem.sv $(ROOT)/rtl/sim/tb_top.sv
HARNESS   := $(ROOT)/sim/main.cpp $(ROOT)/sim/elf_loader.cpp
HARNESS_H := $(ROOT)/sim/elf_loader.h

# Real predictor, in compile order (bp_pkg first, the core imports it). mk/bp.mk owns the list;
# the wildcard is only a fallback if that fragment is missing.
BP_PKG    := $(wildcard $(ROOT)/rtl/bp/bp_pkg.sv)
BP_REAL   := $(if $(BP_SRCS),$(BP_SRCS),$(BP_PKG) $(filter-out $(BP_PKG),$(wildcard $(ROOT)/rtl/bp/*.sv)))
HAVE_BPU  := $(if $(wildcard $(ROOT)/rtl/bp/bpu.sv),1,)

SIM_BP_TYPES := STATIC_NT BIMODAL GSHARE TOURNAMENT RANDOM
SIM_VARIANTS := $(if $(HAVE_BPU),$(SIM_BP_TYPES),STATIC_NT RANDOM) STUB_STATIC_NT STUB_RANDOM

# --assert enables the protocol self-checks; --x-assign/--x-initial unique make X a random value
# (with +verilator+rand+reset+2) instead of a convenient zero, so reset bugs show up.
VFLAGS := --cc --exe --build -j 8 -Wall --assert --x-assign unique --x-initial unique \
          --trace-vcd --top-module tb_top -O3 -CFLAGS "-O2 -std=c++17"

define SIM_VARIANT
$(1)_TYPE := $(patsubst STUB_%,%,$(1))
$(1)_STUB := $(if $(filter STUB_%,$(1)),1,$(if $(HAVE_BPU),,1))
$(1)_RTL  := $$(if $$($(1)_STUB),$(STUB_SRCS),$(BP_REAL)) $(CORE_SRCS) $(TB_SRCS)

$(SIM_OUT)/$(1)/Vtb_top: $$($(1)_RTL) $(HARNESS) $(HARNESS_H) $(ROOT)/mk/sim.mk
	@mkdir -p $(SIM_OUT)/$(1)
	$(VERILATOR) $(VFLAGS) $$(if $$($(1)_STUB),-DRV_BPU_STUB) -GBP_TYPE='"$$($(1)_TYPE)"' \
	  --Mdir $(SIM_OUT)/$(1)/obj -o $(SIM_OUT)/$(1)/Vtb_top $$($(1)_RTL) $(HARNESS) > $(SIM_OUT)/$(1)/build.log 2>&1 \
	  || (cat $(SIM_OUT)/$(1)/build.log; exit 1)
	@echo "built $$@"

.PHONY: sim-$(1)
sim-$(1): $(SIM_OUT)/$(1)/Vtb_top
endef

$(foreach v,$(SIM_VARIANTS),$(eval $(call SIM_VARIANT,$(v))))

.PHONY: sim
sim: $(addprefix sim-,$(SIM_VARIANTS))

# ---------------------------------------------------------------- lint
LINT := $(VERILATOR) --lint-only -Wall

.PHONY: lint
lint:
	$(LINT) -DRV_BPU_STUB --top-module tb_top $(CORE_SRCS) $(STUB_SRCS) $(TB_SRCS)
	@for t in STATIC_NT RANDOM; do \
	  echo "lint rv_core + bpu_stub $$t"; \
	  $(LINT) -DRV_BPU_STUB --top-module rv_core -GBP_TYPE="\"$$t\"" $(CORE_SRCS) $(STUB_SRCS) || exit 1; \
	done
ifneq ($(HAVE_BPU),)
	@for t in $(SIM_BP_TYPES); do \
	  echo "lint rv_core + bpu $$t"; \
	  $(LINT) --top-module rv_core -GBP_TYPE="\"$$t\"" $(BP_REAL) $(CORE_SRCS) || exit 1; \
	done
endif

# ---------------------------------------------------------------- run one program
# BP and STALL are also knobs of mk/verif.mk (with other meanings and defaults), so no global
# default is set here: they are used only when given on the command line or in the environment.
user_var = $(if $(filter command line environment,$(origin $(1))),$($(1)),$(2))
RUN_BP    := $(call user_var,BP,$(if $(HAVE_BPU),GSHARE,STATIC_NT))
RUN_STALL := $(call user_var,STALL,0)
RUN_SEED  := $(call user_var,SEED,1)

.PHONY: run
run: $(SIM_OUT)/$(RUN_BP)/Vtb_top
	@test -n "$(ELF)" || (echo "usage: make run ELF=<file> [BP=<variant>] [STALL=N] [SEED=N] [TRACE=f]"; exit 2)
	$(SIM_OUT)/$(RUN_BP)/Vtb_top +elf=$(ELF) +stall=$(RUN_STALL) +seed=$(RUN_SEED) \
	  $(if $(TRACE),+trace=$(TRACE)) $(if $(MAX_CYCLES),+max_cycles=$(MAX_CYCLES)) $(if $(VCD),+vcd=$(VCD))

# ---------------------------------------------------------------- core tests (hand-written asm)
CT_SRC  := $(ROOT)/rtl/sim/tests
CT_OUT  := $(BUILD)/core-tests
CT_ASMS := $(wildcard $(CT_SRC)/*.S)
CT_ELFS := $(patsubst $(CT_SRC)/%.S,$(CT_OUT)/%.elf,$(CT_ASMS))
CT_CC   := /opt/homebrew/bin/riscv64-elf-gcc
CT_DUMP := /opt/homebrew/bin/riscv64-elf-objdump

$(CT_OUT)/%.elf: $(CT_SRC)/%.S $(CT_SRC)/test.h $(CT_SRC)/link.ld
	@mkdir -p $(CT_OUT)
	$(CT_CC) -march=rv32i_zicsr_zifencei -mabi=ilp32 -nostdlib -nostartfiles -static \
	  -Wl,--no-warn-rwx-segments -T $(CT_SRC)/link.ld -I $(CT_SRC) $< -o $@
	@$(CT_DUMP) -d $@ > $(@:.elf=.dump)

.PHONY: core-tests-elf core-tests
core-tests-elf: $(CT_ELFS)

# Extra arguments for the runner, e.g. CT_ARGS="--seeds 5 --stalls 0,30,60".
core-tests: core-tests-elf sim
	$(PY) $(CT_SRC)/run_tests.py --sim-dir $(SIM_OUT) --variants "$(SIM_VARIANTS)" $(CT_ARGS) $(CT_ELFS)

# ---------------------------------------------------------------- cocotb unit tests
.PHONY: core-unit
core-unit:
	$(MAKE) -C $(ROOT)/tb/core ROOT=$(ROOT) BUILD=$(BUILD) PY=$(PY)
