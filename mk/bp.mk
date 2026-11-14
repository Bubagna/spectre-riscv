# Branch predictor (rtl/bp/, tb/bp/). Included by the top-level Makefile, which defines ROOT,
# BUILD and PY. Design notes: docs/BRANCH_PREDICTOR.md.
#
#   make bp-lint       verilator --lint-only -Wall on bpu for every BP_TYPE and a few parameter sets
#   make bp-test       cocotb tests (directed + random vs the golden model), all configurations
#   make bp-accuracy   trace-driven accuracy study on synthetic traces (and BP_TRACES=... if given)
#   make bp-mutate     inject 15 known bugs into a copy of rtl/bp/, each must make a test fail
#
# BP_SRCS is in compile order (package first); the core can reuse it to build rv_core.

BP_DIR   := $(ROOT)/rtl/bp
BP_SRCS  := $(BP_DIR)/bp_pkg.sv $(BP_DIR)/bp_ram.sv $(BP_DIR)/bp_btb.sv \
            $(BP_DIR)/bp_bimodal.sv $(BP_DIR)/bp_gshare.sv $(BP_DIR)/bp_tournament.sv \
            $(BP_DIR)/bp_ras.sv $(BP_DIR)/bp_ind.sv $(BP_DIR)/bpu.sv
BP_BUILD := $(BUILD)/bp
BP_TYPES := STATIC_NT BIMODAL GSHARE TOURNAMENT RANDOM

# Parameter sets for lint, as BTB_ENTRIES:BHT_ENTRIES:GHR_BITS:RAS_DEPTH:IND_ENTRIES. The first is
# the default; the others hit the corners (tiny tables, GHR shorter than the index, no RAS, GHR of
# 1 bit, no indirect table, a 2-entry one).
BP_LINT_PARAMS := 64:512:9:4:32 4:16:4:2:2 16:64:3:0:0 8:8:1:8:4 64:512:9:4:0

# Extra options for bp-test, e.g. BP_TEST_ARGS="--cycles 50000 -j 4 --only GSHARE".
BP_TEST_ARGS ?=
# Extra trace files for bp-accuracy (format in docs/BRANCH_PREDICTOR.md), e.g. from the ISS.
BP_TRACES ?=

.PHONY: bp-lint bp-test bp-accuracy bp-mutate bp-clean

bp-lint:
	@set -e; for t in $(BP_TYPES); do for p in $(BP_LINT_PARAMS); do \
	  set -- $$(echo $$p | tr ':' ' '); \
	  echo "lint bpu BP_TYPE=$$t BTB=$$1 BHT=$$2 GHR=$$3 RAS=$$4 IND=$$5"; \
	  verilator --lint-only -Wall --top-module bpu \
	    -GBP_TYPE='"'$$t'"' -GBTB_ENTRIES=$$1 -GBHT_ENTRIES=$$2 -GGHR_BITS=$$3 -GRAS_DEPTH=$$4 \
	    -GIND_ENTRIES=$$5 \
	    $(BP_SRCS); \
	done; done
	@echo "bp-lint: clean"

bp-test:
	$(PY) $(ROOT)/tb/bp/run_tests.py --build $(BP_BUILD)/cocotb $(BP_TEST_ARGS)

bp-accuracy:
	$(PY) $(ROOT)/tb/bp/synth_traces.py --out $(BP_BUILD)/traces
	$(PY) $(ROOT)/tb/bp/trace_sim.py --markdown $(BP_BUILD)/accuracy.md \
	  --sweep $(BP_BUILD)/traces/*.trace $(BP_TRACES)

bp-mutate:
	$(PY) $(ROOT)/tb/bp/mutate.py --build $(BP_BUILD)/mutants

bp-clean:
	rm -rf $(BP_BUILD)
