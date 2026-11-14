// ECALL traps: the run must end with TRAP, and nothing after it may execute.
#include "test.h"
TEST_BEGIN
  li   a0, 1
  ecall
  j    pass             // never reached
TEST_PASS
