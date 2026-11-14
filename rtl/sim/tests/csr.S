// Counter CSRs: every legal read form, monotonic values, mhartid = 0.
#include "test.h"
TEST_BEGIN
  rdcycle   s0
  rdinstret s1
  rdtime    s2
  nop
  nop
  rdcycle   s3
  rdinstret s4
  li   gp, 1
  bgeu s0, s3, fail     // cycle grows
  li   gp, 2
  bgeu s1, s4, fail     // instret grows
  sub  t0, s4, s1
  CHECK(3, t0, 5)       // exact: the 5 instructions retired in between
  rdcycleh   s5
  rdinstreth s6
  rdtimeh    s7
  CHECK(4, s5, 0)
  CHECK(5, s6, 0)
  CHECK(6, s7, 0)
  csrr  s8, mhartid
  CHECK(7, s8, 0)
  // Set/clear forms with x0 / uimm 0 do not write, so they are legal reads.
  csrrs  s9, cycle, zero
  csrrc  s10, instret, zero
  csrrsi s11, time, 0
  csrrci a0, cycle, 0
  li   gp, 8
  beqz s9, fail
  csrr  zero, cycle     // rd = x0: legal, nothing written
  j    pass
TEST_PASS
