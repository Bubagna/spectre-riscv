// Writing a read-only counter is illegal.
#include "test.h"
TEST_BEGIN
  li   a0, 5
  csrw cycle, a0
  j    pass
TEST_PASS
