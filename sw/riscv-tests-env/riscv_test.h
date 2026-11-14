// Custom riscv-tests environment for spectre-riscv (see docs/SPEC.md, section 4).
//
// The upstream "p" environment sets up mtvec, PMP, satp, medeleg and mstatus and
// leaves through ECALL. This core has no privilege modes and no trap handler, so
// all of that is dropped: the test starts at _start with zeroed registers, and the
// result is signalled by a word store to TOHOST (1 = pass, (TESTNUM<<1)|1 = fail).
// Any trap (ECALL, EBREAK, illegal, misaligned) ends the simulation as an error.

#ifndef _ENV_SPECTRE_RISCV_H
#define _ENV_SPECTRE_RISCV_H

#define TOHOST_ADDR 0x10000000

#define RVTEST_RV64U .macro init; .endm
#define RVTEST_RV32U .macro init; .endm

#define TESTNUM gp

// Registers are zeroed explicitly so the result does not depend on the reset
// value of the register file (the RTL and the ISS could differ there).
#define INIT_XREG                                                       \
  li x1, 0;  li x2, 0;  li x3, 0;  li x4, 0;  li x5, 0;  li x6, 0;      \
  li x7, 0;  li x8, 0;  li x9, 0;  li x10, 0; li x11, 0; li x12, 0;     \
  li x13, 0; li x14, 0; li x15, 0; li x16, 0; li x17, 0; li x18, 0;     \
  li x19, 0; li x20, 0; li x21, 0; li x22, 0; li x23, 0; li x24, 0;     \
  li x25, 0; li x26, 0; li x27, 0; li x28, 0; li x29, 0; li x30, 0;     \
  li x31, 0;

#define RVTEST_CODE_BEGIN                                               \
        .section .text.init;                                            \
        .option arch, +zicsr, +zifencei;                                \
        .align  6;                                                      \
        .globl _start;                                                  \
_start:                                                                 \
        INIT_XREG;                                                      \
        li TESTNUM, 0;                                                  \
        init;

#define RVTEST_CODE_END                                                 \
        unimp

// The store to TOHOST ends the simulation; "j ." only matters if it does not.
#define RVTEST_PASS                                                     \
        fence;                                                          \
        li TESTNUM, 1;                                                  \
        li t5, TOHOST_ADDR;                                             \
        sw TESTNUM, 0(t5);                                              \
        j .;

#define RVTEST_FAIL                                                     \
        fence;                                                          \
1:      beqz TESTNUM, 1b;                                               \
        sll TESTNUM, TESTNUM, 1;                                        \
        or TESTNUM, TESTNUM, 1;                                         \
        li t5, TOHOST_ADDR;                                             \
        sw TESTNUM, 0(t5);                                              \
        j .;

#define EXTRA_DATA

#define RVTEST_DATA_BEGIN                                               \
        EXTRA_DATA                                                      \
        .align 4; .global begin_signature; begin_signature:

#define RVTEST_DATA_END .align 4; .global end_signature; end_signature:

#endif
