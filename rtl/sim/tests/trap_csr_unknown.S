// Reading a CSR that does not exist here (mscratch) is illegal.
#include "test.h"
TEST_BEGIN
  csrr a0, mscratch
  j    pass
TEST_PASS
