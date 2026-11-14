# Verification (owner: verification). Plan and status: docs/VERIFICATION.md.
#
#   make iss-test               unit tests of the ISS + all riscv-tests on the ISS (pytest)
#   make verif-selftest         tests of the comparator, the random generator and the runner
#   make riscv-tests            riscv-tests, RTL vs ISS lockstep, every simulator variant, with and
#                               without random memory stalls
#   make regress-random N=200   N random programs (seeds SEED0..SEED0+N-1), RTL vs ISS
#   make regress-bench          benchmarks, RTL vs ISS, plus the CPI / misprediction table
#   make regress                all of the above (riscv-tests, benchmarks, N random programs)
#   make btrace                 branch traces of the benchmarks -> build/btrace/*.btrace
#
# Knobs (V-prefixed because mk/sim.mk already uses BP, STALL and SEED for "make run"):
#   VBP=all|<V1,V2>   simulator variants in build/sim/ (default all that are built)
#   VSTALL=0,30       random memory stall percentages
#   VSEEDS=2          simulator seeds per stalled or random-predictor configuration
#   J=<jobs>, N=<random programs>, SEED0=<first random seed>, REGRESS_ARGS=<extra options>

VBP          ?= all
VSTALL       ?= 0,30
VSEEDS       ?= 2
J            ?= $(shell sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
N            ?= 200
SEED0        ?= 1
REGRESS_ARGS ?=

REGRESS := $(PY) $(ROOT)/scripts/regress.py --bp $(VBP) --stall $(VSTALL) --seeds $(VSEEDS) -j $(J) \
           $(REGRESS_ARGS)

.PHONY: iss-test verif-selftest riscv-tests regress-random regress-bench regress btrace verif-clean

iss-test: riscv-tests-elf
	cd $(ROOT)/model && $(PY) -m pytest -q test_iss.py

verif-selftest: riscv-tests-elf
	$(PY) -m pytest -q $(ROOT)/scripts/test_verif.py

# The simulator binaries come from mk/sim.mk ("make sim"); they are not a prerequisite here so
# that a regression never triggers a rebuild of RTL another agent is editing. regress.py says
# clearly when a binary is missing.
riscv-tests: riscv-tests-elf
	$(REGRESS) --suite riscv-tests --summary $(BUILD)/regress/riscv-tests.md

regress-random:
	$(REGRESS) --random $(N) --random-seed0 $(SEED0) --summary $(BUILD)/regress/random.md

regress-bench: bench-elf
	$(REGRESS) --suite bench --summary $(BUILD)/regress/bench.md

regress: riscv-tests-elf bench-elf
	$(REGRESS) --suite riscv-tests --suite bench --random $(N) --random-seed0 $(SEED0) \
	  --summary $(BUILD)/regress/summary.md

btrace: bench-elf
	$(PY) $(ROOT)/scripts/branch_trace.py -o $(BUILD)/btrace $(BENCH_ELFS)

verif-clean:
	rm -rf $(BUILD)/regress $(BUILD)/btrace $(BUILD)/sw/random
