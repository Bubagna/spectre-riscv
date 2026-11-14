#include "test.h"
TEST_BEGIN
  la   a0, data
  sh   a0, 1(a0)
  j    pass
TEST_PASS
  .data
data: .word 0
