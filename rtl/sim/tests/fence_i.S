// Self-modifying code: rewrite an instruction, FENCE.I, execute the new one. The pipeline and
// the fetch queue may hold the old word; FENCE.I must flush them. Also done in a loop so the
// predictor has seen the patched PC before.
#include "test.h"
TEST_BEGIN
  la   s0, patch
  li   s1, 0x02a00513   // addi a0, zero, 42
  li   s2, 0x00700513   // addi a0, zero, 7
  sw   s1, 0(s0)
  fence.i
patch:
  addi a0, zero, 1      // replaced by addi a0, zero, 42
  CHECK(1, a0, 42)

  // Patch a word in a loop body: iteration i executes the version stored before it.
  li   s3, 4
  li   s4, 0
  la   s5, loop_patch
loop:
  andi t0, s3, 1
  beqz t0, 2f
  sw   s1, 0(s5)        // odd: 42
  j    3f
2:sw   s2, 0(s5)        // even: 7
3:fence.i
loop_patch:
  addi a0, zero, 0
  add  s4, s4, a0
  addi s3, s3, -1
  bnez s3, loop
  CHECK(2, s4, 98)      // 7 + 42 + 7 + 42

  // A patched branch: a NOP becomes a taken jump.
  la   s0, patch_j
  li   t0, 0x0080006f   // jal zero, +8
  sw   t0, 0(s0)
  fence.i
  li   a1, 0
patch_j:
  nop
  li   a1, 1            // skipped by the patched jump
  CHECK(3, a1, 0)
  j    pass
TEST_PASS
