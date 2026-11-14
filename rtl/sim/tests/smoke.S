// Smallest end-to-end test: a few dependent ALU ops, a store and a load, then PASS.
#include "test.h"
TEST_BEGIN
  li   a0, 5
  addi a1, a0, 7
  add  a2, a1, a0
  CHECK(1, a2, 17)
  la   a3, data
  sw   a2, 0(a3)
  lw   a4, 0(a3)
  CHECK(2, a4, 17)
  j    pass
TEST_PASS

  .data
data: .word 0
