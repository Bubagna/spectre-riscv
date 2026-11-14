// Loop kernels for CPI measurement (and a correctness check): array fill, array sum with a
// load-use pair in the body, word copy, and a call/return loop.
#include "test.h"
#define N 256
TEST_BEGIN
  // fill src[i] = i
  la   s0, src
  li   t0, 0
  li   t1, N
1:sw   t0, 0(s0)
  addi s0, s0, 4
  addi t0, t0, 1
  bne  t0, t1, 1b

  // sum = src[0] + ... + src[N-1], 4 times
  li   s2, 0
  li   s3, 4
2:la   s0, src
  li   t0, N
3:lw   t2, 0(s0)
  add  s2, s2, t2       // load-use: one bubble per iteration
  addi s0, s0, 4
  addi t0, t0, -1
  bnez t0, 3b
  addi s3, s3, -1
  bnez s3, 2b
  CHECK(1, s2, 4 * (N * (N - 1) / 2))

  // copy src -> dst, scheduled so the load result is used two instructions later
  la   s0, src
  la   s1, dst
  li   t0, N
4:lw   t2, 0(s0)
  addi s0, s0, 4
  sw   t2, 0(s1)
  addi s1, s1, 4
  addi t0, t0, -1
  bnez t0, 4b
  la   s1, dst
  lw   t2, 4*100(s1)
  CHECK(2, t2, 100)

  // 100 calls to a leaf function
  li   s4, 0
  li   s5, 100
5:call leaf
  addi s5, s5, -1
  bnez s5, 5b
  CHECK(3, s4, 300)
  j    pass
leaf:
  addi s4, s4, 3
  ret
TEST_PASS
  .data
  .align 4
src: .space 4*N
dst: .space 4*N
