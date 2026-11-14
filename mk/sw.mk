# Software builds (owner: verification). See docs/SPEC.md section 4 and docs/VERIFICATION.md.
#   make riscv-tests-elf   rv32ui-p-* ELFs  -> build/sw/riscv-tests/
#   make bench-elf         benchmarks       -> build/sw/bench/
#   make sw                both
# Random programs are built by mk/verif.mk (they need a seed list).

include $(ROOT)/sw/common/common.mk

SW_BUILD := $(BUILD)/sw

# ---------------------------------------------------------------------------
# riscv-tests: cloned (not vendored) at a pinned commit into build/third_party.
# Only isa/rv32ui, isa/rv64ui and isa/macros are used; the upstream env is replaced
# by sw/riscv-tests-env/riscv_test.h.
RT_URL    := https://github.com/riscv-software-src/riscv-tests
RT_COMMIT := 3df703cd52d5f0d8224e3c50bf03c6cb70efff98
RT_SRC    := $(BUILD)/third_party/riscv-tests
RT_STAMP  := $(RT_SRC)/.spectre-stamp-$(RT_COMMIT)
RT_ENV    := $(ROOT)/sw/riscv-tests-env
RT_OUT    := $(SW_BUILD)/riscv-tests

# Same list as isa/rv32ui/Makefrag, minus ma_data: misaligned accesses trap by
# design on this core, so that test cannot pass without a trap handler.
RV32UI_TESTS := simple add addi and andi auipc beq bge bgeu blt bltu bne fence_i \
                jal jalr lb lbu lh lhu lw ld_st lui or ori sb sh sw st_ld \
                sll slli slt slti sltiu sltu sra srai srl srli sub xor xori
RV32UI_ELFS  := $(addprefix $(RT_OUT)/rv32ui-p-,$(addsuffix .elf,$(RV32UI_TESTS)))

$(RT_STAMP):
	@mkdir -p $(dir $(RT_SRC))
	@if [ ! -d $(RT_SRC)/.git ]; then git clone -q $(RT_URL) $(RT_SRC); fi
	git -C $(RT_SRC) fetch -q origin $(RT_COMMIT) 2>/dev/null || true
	git -C $(RT_SRC) checkout -q $(RT_COMMIT)
	@touch $@

# The sources exist only after the clone; this rule tells make where they come from.
$(RT_SRC)/isa/rv32ui/%.S: $(RT_STAMP) ;

# --no-relax-gp: TESTNUM lives in gp, so the linker must never rewrite "la" as gp-relative.
$(RT_OUT)/rv32ui-p-%.elf: $(RT_SRC)/isa/rv32ui/%.S $(RT_ENV)/riscv_test.h $(SW_COMMON)/link.ld
	@mkdir -p $(dir $@)
	$(RV_CC) $(RV_ARCH) -static -mcmodel=medany -fvisibility=hidden -nostdlib -nostartfiles \
	  -Wl,--no-warn-rwx-segments -Wl,--no-relax-gp -I $(RT_ENV) -I $(RT_SRC)/isa/macros/scalar \
	  -T $(SW_COMMON)/link.ld $< -o $@
	@$(RV_OBJDUMP) -d $@ > $(@:.elf=.dump)

.PHONY: riscv-tests-elf sw
riscv-tests-elf: $(RV32UI_ELFS)

# ---------------------------------------------------------------------------
# Benchmarks (C, rv32i + libgcc): one directory or one .c file per benchmark.
BENCH_SRCS := $(wildcard $(ROOT)/sw/bench/*.c)
BENCH_OUT  := $(SW_BUILD)/bench
BENCH_ELFS := $(patsubst $(ROOT)/sw/bench/%.c,$(BENCH_OUT)/%.elf,$(BENCH_SRCS))

$(BENCH_OUT)/%.elf: $(ROOT)/sw/bench/%.c $(SW_RUNTIME) $(SW_COMMON)/io.h $(SW_COMMON)/link.ld $(wildcard $(ROOT)/sw/bench/*.h)
	@mkdir -p $(dir $@)
	$(RV_CC) $(RV_CFLAGS) $(RV_LDFLAGS) $(SW_RUNTIME) $< $(RV_LIBS) -o $@
	@$(RV_OBJDUMP) -d $@ > $(@:.elf=.dump)

# CoreMark: cloned at a pinned commit (EEMBC license: build/third_party/coremark/LICENSE.md),
# built unmodified with the port in sw/bench/coremark/. CM_ITERATIONS fixes the work done:
# the port has no timer, so CoreMark cannot calibrate itself.
CM_URL        := https://github.com/eembc/coremark
CM_COMMIT     := 1f483d5b8316753a742cbf5590caf5bd0a4e4777
CM_SRC        := $(BUILD)/third_party/coremark
CM_STAMP      := $(CM_SRC)/.spectre-stamp-$(CM_COMMIT)
CM_PORT       := $(ROOT)/sw/bench/coremark
CM_ITERATIONS ?= 2
CM_FLAGS      := -O2
CM_SRCS        = $(addprefix $(CM_SRC)/,core_list_join.c core_matrix.c core_state.c core_util.c)
CM_CFLAGS     := $(RV_ARCH) $(CM_FLAGS) -g -ffreestanding -mcmodel=medany -fno-common \
                 -I $(SW_COMMON) -I $(CM_PORT) -I $(CM_SRC) -DITERATIONS=$(CM_ITERATIONS) \
                 -DPERFORMANCE_RUN=1 -DFLAGS_STR='"$(CM_FLAGS)"'

$(CM_STAMP):
	@mkdir -p $(dir $(CM_SRC))
	@if [ ! -d $(CM_SRC)/.git ]; then git clone -q $(CM_URL) $(CM_SRC); fi
	git -C $(CM_SRC) checkout -q $(CM_COMMIT)
	@touch $@

# core_main.c is compiled on its own so that only its main() is renamed (the port's
# main() calls it and turns the CRC checks into the exit code).
$(BENCH_OUT)/coremark/core_main.o: $(CM_STAMP) $(CM_PORT)/core_portme.h
	@mkdir -p $(dir $@)
	$(RV_CC) $(CM_CFLAGS) -Dmain=coremark_main -c $(CM_SRC)/core_main.c -o $@

$(BENCH_OUT)/coremark.elf: $(BENCH_OUT)/coremark/core_main.o $(CM_PORT)/core_portme.c $(CM_PORT)/core_portme.h $(SW_RUNTIME) $(SW_COMMON)/link.ld
	$(RV_CC) $(CM_CFLAGS) $(RV_LDFLAGS) $(SW_RUNTIME) $(CM_SRCS) $(CM_PORT)/core_portme.c $< \
	  $(RV_LIBS) -o $@
	@$(RV_OBJDUMP) -d $@ > $(@:.elf=.dump)

BENCH_ELFS += $(BENCH_OUT)/coremark.elf

.PHONY: bench-elf
bench-elf: $(BENCH_ELFS)

sw: riscv-tests-elf bench-elf
