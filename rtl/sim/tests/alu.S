// Every ALU operation, register and immediate forms, with sign and shift-amount edge cases.
#include "test.h"
TEST_BEGIN
  li   a0, 0x80000000
  li   a1, 0x7fffffff
  li   a2, -1
  li   a3, 0x12345678

  add  s0, a0, a1;   CHECK(1, s0, 0xffffffff)
  add  s0, a1, a2;   CHECK(2, s0, 0x7ffffffe)
  sub  s0, a0, a1;   CHECK(3, s0, 0x00000001)
  sub  s0, zero, a2; CHECK(4, s0, 1)
  addi s0, a1, 1;    CHECK(5, s0, 0x80000000)
  addi s0, a3, -2048; CHECK(6, s0, 0x12345678 - 2048)

  slt  s0, a0, a1;   CHECK(10, s0, 1)
  slt  s0, a1, a0;   CHECK(11, s0, 0)
  sltu s0, a0, a1;   CHECK(12, s0, 0)
  sltu s0, a1, a0;   CHECK(13, s0, 1)
  slti s0, a2, 0;    CHECK(14, s0, 1)
  sltiu s0, a2, -1;  CHECK(15, s0, 0)     // -1 as unsigned is the largest value
  sltiu s0, zero, 1; CHECK(16, s0, 1)     // seqz
  slt  s0, a2, a2;   CHECK(17, s0, 0)

  xor  s0, a3, a2;   CHECK(20, s0, 0xedcba987)
  or   s0, a3, a0;   CHECK(21, s0, 0x92345678)
  and  s0, a3, a1;   CHECK(22, s0, 0x12345678)
  xori s0, a3, -1;   CHECK(23, s0, 0xedcba987)
  ori  s0, a3, 0x7ff; CHECK(24, s0, 0x123457ff)
  andi s0, a3, 0x0f0; CHECK(25, s0, 0x70)
  andi s0, a2, -16;  CHECK(26, s0, 0xfffffff0)

  li   a4, 31
  li   a5, 33        // only the low 5 bits count: shift by 1
  sll  s0, a3, a4;   CHECK(30, s0, 0)
  sll  s0, a3, a5;   CHECK(31, s0, 0x2468acf0)
  srl  s0, a0, a4;   CHECK(32, s0, 1)
  sra  s0, a0, a4;   CHECK(33, s0, 0xffffffff)
  sra  s0, a1, a4;   CHECK(34, s0, 0)
  srl  s0, a2, a5;   CHECK(35, s0, 0x7fffffff)
  sra  s0, a0, a5;   CHECK(36, s0, 0xc0000000)
  slli s0, a3, 4;    CHECK(37, s0, 0x23456780)
  srli s0, a0, 4;    CHECK(38, s0, 0x08000000)
  srai s0, a0, 4;    CHECK(39, s0, 0xf8000000)
  srai s0, a3, 0;    CHECK(40, s0, 0x12345678)

  lui  s0, 0xfffff;  CHECK(50, s0, 0xfffff000)
  lui  s0, 0;        CHECK(51, s0, 0)
1:auipc s0, 0
  la   s1, 1b
  bne  s0, s1, fail_auipc
2:auipc s0, 0x1
  la   s1, 2b
  sub  s0, s0, s1;   CHECK(52, s0, 0x1000)

  // x0 is never written, even as a destination of every kind of instruction.
  addi zero, zero, 5
  lui  zero, 0x12345
  add  s0, zero, zero; CHECK(60, s0, 0)
  j    pass
fail_auipc:
  li   gp, 53
  j    fail
TEST_PASS
