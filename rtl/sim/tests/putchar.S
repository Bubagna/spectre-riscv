// PUTCHAR with byte and word stores, then HALT with exit code 0 (the C-program convention).
#include "test.h"
TEST_BEGIN
  li   s0, PUTCHAR
  la   s1, msg
1:lbu  a0, 0(s1)
  beqz a0, 2f
  sb   a0, 0(s0)
  addi s1, s1, 1
  j    1b
2:li   a0, '\n'
  sw   a0, 0(s0)
  li   s0, 0x10000008
  sw   zero, 0(s0)
3:j    3b
TEST_PASS
  .data
msg: .asciz "hello from rv_core"
