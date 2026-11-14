// Taken branch with target pc + 2: beq x0, x0, +2.
#include "test.h"
TEST_BEGIN
  .word 0x00000163
  j    pass
TEST_PASS
