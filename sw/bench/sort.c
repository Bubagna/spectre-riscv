// Quicksort and insertion sort on pseudo-random data: data-dependent branches that
// no predictor can learn well, plus recursion (quicksort) for the RAS.
#include "bench.h"

#define N 600

static int32_t a[N], b[N];

static void quicksort(int32_t *v, int lo, int hi) {
  while (lo < hi) {
    int32_t p = v[(lo + hi) >> 1];
    int i = lo, j = hi;
    while (i <= j) {
      while (v[i] < p) i++;
      while (v[j] > p) j--;
      if (i <= j) {
        int32_t t = v[i]; v[i] = v[j]; v[j] = t;
        i++; j--;
      }
    }
    // Recurse on the smaller half to bound the stack depth.
    if (j - lo < hi - i) { quicksort(v, lo, j); lo = i; }
    else { quicksort(v, i, hi); hi = j; }
  }
}

static void insertion(int32_t *v, int n) {
  for (int i = 1; i < n; i++) {
    int32_t k = v[i];
    int j = i - 1;
    while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; }
    v[j + 1] = k;
  }
}

int main(void) {
  uint32_t sum = 0;
  for (int i = 0; i < N; i++) {
    a[i] = (int32_t)(rnd() >> 8) - (1 << 23);
    b[i] = a[i];
    sum += (uint32_t)a[i];
  }
  quicksort(a, 0, N - 1);
  insertion(b, 200);  // quadratic: keep it smaller
  uint32_t sum2 = 0;
  for (int i = 0; i < N; i++) {
    sum2 += (uint32_t)a[i];
    if (i) CHECK(a[i - 1] <= a[i], 1);
  }
  for (int i = 1; i < 200; i++) CHECK(b[i - 1] <= b[i], 2);
  CHECK(sum == sum2, 3);
  printf_("sort ok %08x\n", sum2);
  return 0;
}
