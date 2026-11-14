// Loads and stores of every width and sign at every legal offset, and the byte lanes.
#include "test.h"
TEST_BEGIN
  la   s0, buf
  li   a0, 0x8899aabb
  sw   a0, 0(s0)
  li   a1, 0xccddeeff
  sw   a1, 4(s0)

  lb   s1, 0(s0);  CHECK(1, s1, 0xffffffbb)
  lb   s1, 1(s0);  CHECK(2, s1, 0xffffffaa)
  lb   s1, 2(s0);  CHECK(3, s1, 0xffffff99)
  lb   s1, 3(s0);  CHECK(4, s1, 0xffffff88)
  lbu  s1, 0(s0);  CHECK(5, s1, 0xbb)
  lbu  s1, 3(s0);  CHECK(6, s1, 0x88)
  lh   s1, 0(s0);  CHECK(7, s1, 0xffffaabb)
  lh   s1, 2(s0);  CHECK(8, s1, 0xffff8899)
  lhu  s1, 0(s0);  CHECK(9, s1, 0xaabb)
  lhu  s1, 2(s0);  CHECK(10, s1, 0x8899)
  lw   s1, 4(s0);  CHECK(11, s1, 0xccddeeff)

  li   a2, 0x12345678
  li   a3, 0x00000071
  sw   zero, 8(s0)
  sb   a3, 8(s0)
  sb   a3, 10(s0)
  lw   s1, 8(s0);  CHECK(20, s1, 0x00710071)
  sb   a2, 9(s0)
  sb   a2, 11(s0)
  lw   s1, 8(s0);  CHECK(21, s1, 0x78717871)
  sh   a2, 8(s0)
  lw   s1, 8(s0);  CHECK(22, s1, 0x78715678)
  sh   a2, 10(s0)
  lw   s1, 8(s0);  CHECK(23, s1, 0x56785678)
  lbu  s1, 9(s0);  CHECK(24, s1, 0x56)
  lh   s1, 10(s0); CHECK(25, s1, 0x5678)

  // Positive small values do not get sign-extended.
  li   a4, 0x7f7f7f7f
  sw   a4, 12(s0)
  lb   s1, 13(s0); CHECK(30, s1, 0x7f)
  lh   s1, 14(s0); CHECK(31, s1, 0x7f7f)

  // Negative offsets and a store followed at once by loads of other widths.
  addi s2, s0, 16
  li   a5, 0xdeadbeef
  sw   a5, -4(s2)
  lhu  s1, -2(s2); CHECK(40, s1, 0xdead)
  lbu  s1, -4(s2); CHECK(41, s1, 0xef)
  lw   s1, -4(s2); CHECK(42, s1, 0xdeadbeef)

  // Loads from MMIO read 0.
  li   s3, 0x10000004
  lw   s1, 0(s3);  CHECK(50, s1, 0)

  // A store that is never architecturally executed (it sits after a taken branch) must not
  // reach memory, even though a predictor may fetch it.
  sw   zero, 0(s0)
  li   a6, 1
  bnez a6, 1f
  sw   a6, 0(s0)
1:lw   s1, 0(s0);  CHECK(60, s1, 0)
  j    pass
TEST_PASS

  .data
  .align 4
buf: .space 32
