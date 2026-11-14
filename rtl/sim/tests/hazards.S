// Data hazards: every forwarding path, the load-use stall on each operand and on every kind
// of consumer (ALU, store address, store data, branch, JALR), x0 never forwarding, and the
// youngest of several writers winning.
#include "test.h"
TEST_BEGIN
  // EX -> EX (distance 1), MEM -> EX (distance 2), WB -> ID bypass (distance 3)
  li   a0, 1
  addi a1, a0, 1        // a0 from distance 1
  addi a2, a0, 2        // a0 from distance 2
  addi a3, a0, 3        // a0 from distance 3
  add  a4, a1, a2       // both operands forwarded
  CHECK(1, a1, 2)
  CHECK(2, a2, 3)
  CHECK(3, a3, 4)
  CHECK(4, a4, 5)

  // chain: each instruction depends on the previous one
  li   s0, 0
  addi s0, s0, 1
  addi s0, s0, 2
  addi s0, s0, 3
  slli s0, s0, 2
  sub  s0, zero, s0
  CHECK(5, s0, -24)

  // several writers of the same register: the youngest wins
  li   s1, 10
  li   s1, 20
  li   s1, 30
  add  s2, s1, s1
  CHECK(6, s2, 60)

  // x0 as destination must not forward
  li   s3, 7
  add  zero, s3, s3
  add  s4, zero, s3
  CHECK(7, s4, 7)

  la   s5, data
  // load-use on rs1, rs2, both
  lw   t0, 0(s5)        // 11
  addi t1, t0, 1
  CHECK(10, t1, 12)
  lw   t0, 4(s5)        // 22
  add  t1, s3, t0
  CHECK(11, t1, 29)
  lw   t0, 0(s5)
  add  t1, t0, t0
  CHECK(12, t1, 22)
  // load, independent, use (distance 2: forwarded from WB, no stall needed)
  lw   t0, 4(s5)
  nop
  addi t1, t0, 0
  CHECK(13, t1, 22)
  // load -> store data and store address
  lw   t0, 0(s5)
  sw   t0, 8(s5)
  lw   t1, 8(s5)
  CHECK(14, t1, 11)
  lw   t2, 12(s5)       // address of data+16
  sw   s3, 0(t2)
  lw   t1, 16(s5)
  CHECK(15, t1, 7)
  // load -> load address (pointer chasing)
  lw   t2, 12(s5)
  lw   t1, 0(t2)
  CHECK(16, t1, 7)
  // load -> branch
  li   gp, 17
  lw   t0, 0(s5)
  li   t1, 11
  bne  t0, t1, fail
  // load -> jalr
  li   gp, 18
  lw   t0, 20(s5)       // address of label 1f
  jalr zero, 0(t0)
  j    fail
jalr_target:
  // load-use where the load writes x0: no stall needed, and no value
  lw   zero, 0(s5)
  addi t1, zero, 5
  CHECK(19, t1, 5)
  // ALU result -> store data and address, both at distance 1
  addi t0, s5, 24
  li   t1, 0x55
  sw   t1, 0(t0)
  lw   t2, 24(s5)
  CHECK(20, t2, 0x55)
  // back-to-back stores and loads to the same word
  li   t0, 1
  li   t1, 2
  sw   t0, 28(s5)
  sw   t1, 28(s5)
  lw   t2, 28(s5)
  lw   t3, 28(s5)
  add  t4, t2, t3
  CHECK(21, t4, 4)
  // two consecutive loads, the second uses the first
  lw   t0, 12(s5)
  lw   t1, 0(t0)
  addi t1, t1, 1
  CHECK(22, t1, 8)
  j    pass
TEST_PASS

  .data
  .align 4
data:
  .word 11, 22, 0, data + 16, 0, jalr_target, 0, 0
