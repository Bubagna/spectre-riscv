# Shared toolchain settings for everything under sw/ (docs/SPEC.md, section 4).
# Included by mk/sw.mk; ROOT and BUILD come from the top-level Makefile.

RV_PREFIX  ?= /opt/homebrew/bin/riscv64-elf-
RV_CC      := $(RV_PREFIX)gcc
RV_OBJDUMP := $(RV_PREFIX)objdump

# Zicsr and Zifencei must be named explicitly since the 2019 ISA split; they map
# to the same rv32i/ilp32 multilib, so no M, A or C instruction is ever emitted.
RV_ARCH    := -march=rv32i_zicsr_zifencei -mabi=ilp32
SW_COMMON  := $(ROOT)/sw/common
RV_LDFLAGS := -nostdlib -nostartfiles -static -T $(SW_COMMON)/link.ld -Wl,--no-warn-rwx-segments
RV_CFLAGS  := $(RV_ARCH) -O2 -g -ffreestanding -fno-builtin-printf -mcmodel=medany \
              -fno-common -fno-pic -Wall -Wextra -I $(SW_COMMON)
RV_LIBS    := -lgcc
# crt0 + printf-lite, linked into every C program.
SW_RUNTIME := $(SW_COMMON)/crt0.S $(SW_COMMON)/io.c
