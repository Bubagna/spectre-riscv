// Every branch taken and not taken (signed and unsigned edges), JAL/JALR link values, JALR
// clearing bit 0, nested calls and returns, a backward loop, and an untaken branch whose
// target is misaligned (must not trap: the target only matters if the branch is taken).
#include "test.h"
#define TAKEN(n, op, a, b)     li gp, n; op a, b, 1f; j fail; 1:
#define NOT_TAKEN(n, op, a, b) li gp, n; op a, b, fail
TEST_BEGIN
  li   a0, -1
  li   a1, 1
  li   a2, 0x80000000
  li   a3, 0x7fffffff

  TAKEN(1, beq, a1, a1)
  NOT_TAKEN(2, beq, a0, a1)
  TAKEN(3, bne, a0, a1)
  NOT_TAKEN(4, bne, a0, a0)
  TAKEN(5, blt, a0, a1)
  NOT_TAKEN(6, blt, a1, a0)
  NOT_TAKEN(7, blt, a1, a1)
  TAKEN(8, blt, a2, a3)
  TAKEN(9, bge, a1, a0)
  TAKEN(10, bge, a1, a1)
  NOT_TAKEN(11, bge, a0, a1)
  TAKEN(12, bltu, a1, a0)
  NOT_TAKEN(13, bltu, a0, a1)
  TAKEN(14, bltu, a3, a2)
  TAKEN(15, bgeu, a0, a1)
  TAKEN(16, bgeu, a1, a1)
  NOT_TAKEN(17, bgeu, a1, a0)

  // jal: link = pc + 4
  li   gp, 20
  jal  ra, 2f
3:j    fail
2:la   t0, 3b
  bne  ra, t0, fail
  // jal with rd = x0
  jal  zero, 4f
  j    fail
4:
  // jalr: target = (rs1 + imm) & ~1, link = pc + 4
  li   gp, 21
  la   t0, 5f
  addi t0, t0, -3        // odd: bit 0 is dropped, imm +3 brings it back
  jalr t1, 3(t0)
6:j    fail
5:la   t2, 6b
  bne  t1, t2, fail
  // jalr where rd == rs1
  li   gp, 22
  la   t0, 7f
  jalr t0, 0(t0)
8:j    fail
7:la   t2, 8b
  bne  t0, t2, fail

  // nested calls and returns (ra and t0 = x5 as link registers)
  li   gp, 23
  li   s0, 0
  call func_a
  CHECK(24, s0, 0x321)

  // backward loop
  li   s1, 0
  li   s2, 100
9:addi s1, s1, 3
  addi s2, s2, -1
  bnez s2, 9b
  CHECK(25, s1, 300)

  // untaken branch with target pc + 2: beq x0, x1, +2 (x1 != 0 here)
  li   ra, 1
  li   gp, 26
  .word 0x00100163
  CHECK(27, ra, 1)
  j    pass

func_a:
  addi sp, sp, -16
  sw   ra, 0(sp)
  ori  s0, s0, 0x1
  call func_b
  ori  s0, s0, 0x20 << 4    // 0x200
  lw   ra, 0(sp)
  addi sp, sp, 16
  ret
func_b:
  mv   s3, ra
  jal  t0, func_c          // call through x5
  ori  s0, s0, 0x20
  mv   ra, s3
  ret
func_c:
  ori  s0, s0, 0x100
  jr   t0                  // return through x5
TEST_PASS
