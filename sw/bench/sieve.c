// Sieve of Eratosthenes: long regular loops (easy for any 2-bit counter) mixed with
// the data-dependent "is it marked" branch.
#include "bench.h"

#define N 30000

static uint8_t comp[N + 1];

int main(void) {
  int count = 0;
  for (int i = 2; i <= N; i++) {
    if (comp[i]) continue;
    count++;
    for (int j = i + i; j <= N; j += i) comp[j] = 1;
  }
  CHECK(count == 3245, 1);
  printf_("sieve ok %d primes\n", count);
  return 0;
}
