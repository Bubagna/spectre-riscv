// Macros for the core's hand-written self-checking tests (rtl/sim/tests/*.S).
//
// A test starts at _start (0x8000_0000) and ends with TEST_PASS, or jumps to fail with the
// number of the failing check in gp. The result is a word store to TOHOST, as in riscv-tests:
// 1 = pass, (n << 1) | 1 = check n failed. t6 is reserved for CHECK.
#define TOHOST 0x10000000
#define PUTCHAR 0x10000004

#define TEST_BEGIN          \
  .section .text.init;      \
  .option norvc;            \
  .globl _start;            \
_start:                     \
  li gp, 0;                 \
  la sp, _stack_top;

// CHECK(n, reg, value): if reg != value, the test fails with number n.
#define CHECK(n, reg, val)  \
  li gp, n;                 \
  li t6, val;               \
  bne reg, t6, fail;

#define TEST_PASS           \
pass:                       \
  li t0, TOHOST;            \
  li t1, 1;                 \
  sw t1, 0(t0);             \
1: j 1b;                    \
fail:                       \
  slli gp, gp, 1;           \
  ori gp, gp, 1;            \
  li t0, TOHOST;            \
  sw gp, 0(t0);             \
2: j 2b;
