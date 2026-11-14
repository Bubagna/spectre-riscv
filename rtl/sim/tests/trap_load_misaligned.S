// A misaligned load traps and must not write its destination; the store after it must not
// happen (checked by the trace against the ISS).
#include "test.h"
TEST_BEGIN
  la   a0, data
  lw   a1, 2(a0)
  sw   a0, 0(a0)
  j    pass
TEST_PASS
  .data
data: .word 0x12345678, 0
