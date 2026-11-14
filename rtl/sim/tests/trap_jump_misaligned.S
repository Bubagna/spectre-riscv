// JALR to pc + 2: bit 1 set, misaligned target without the C extension. The link register
// must not be written.
#include "test.h"
TEST_BEGIN
  la   a0, 1f
  jalr ra, 2(a0)
1:nop
  j    pass
TEST_PASS
