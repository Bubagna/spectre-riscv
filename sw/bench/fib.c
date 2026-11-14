// Recursive Fibonacci and Ackermann: deep call/return chains for the return
// address stack (depth well above RAS_DEPTH).
#include "bench.h"

// At -O2 GCC unrolls the recursion and turns calls into loops; -O1 without sibling
// calls keeps one call per invocation, which is the point of this benchmark.
#define PLAIN_RECURSION __attribute__((noinline, optimize("O1", "no-optimize-sibling-calls")))

PLAIN_RECURSION static uint32_t fib(uint32_t n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }

PLAIN_RECURSION static uint32_t ack(uint32_t m, uint32_t n) {
  if (m == 0) return n + 1;
  if (n == 0) return ack(m - 1, 1);
  return ack(m - 1, ack(m, n - 1));
}

int main(void) {
  uint32_t f = fib(20);
  CHECK(f == 6765, 1);
  uint32_t a = ack(2, 30);
  CHECK(a == 63, 2);
  uint32_t b = ack(3, 3);
  CHECK(b == 61, 3);
  printf_("fib ok %u %u %u\n", f, a, b);
  return 0;
}
