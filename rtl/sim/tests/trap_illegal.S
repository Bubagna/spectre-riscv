// All-zero word: illegal instruction.
#include "test.h"
TEST_BEGIN
  .word 0
  j    pass
TEST_PASS
