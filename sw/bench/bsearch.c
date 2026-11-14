// Binary search over a sorted table: one hard-to-predict branch per step, short loops.
#include "bench.h"

#define N 1024
#define Q 3000

static uint32_t t[N];

static int find(uint32_t key) {
  int lo = 0, hi = N - 1;
  while (lo <= hi) {
    int mid = (lo + hi) >> 1;
    if (t[mid] == key) return mid;
    if (t[mid] < key) lo = mid + 1;
    else hi = mid - 1;
  }
  return -1;
}

int main(void) {
  uint32_t v = 0;
  for (int i = 0; i < N; i++) { v += 1 + (rnd() & 15); t[i] = v; }
  int hits = 0, misses = 0;
  for (int q = 0; q < Q; q++) {
    uint32_t r = rnd();
    if (r & 1) {
      int i = (int)((r >> 1) & (N - 1));
      CHECK(find(t[i]) == i, 1);
      hits++;
    } else {
      // Between two entries (they differ by at least 1, often more): usually absent.
      int i = (int)((r >> 1) & (N - 1));
      int k = find(t[i] + 1);
      if (k < 0) misses++;
      else CHECK(t[k] == t[i] + 1, 2);
    }
  }
  printf_("bsearch ok hits=%d misses=%d\n", hits, misses);
  return 0;
}
