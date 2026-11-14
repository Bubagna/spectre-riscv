// Shared helpers for the predictor benchmarks: deterministic PRNG and a check macro.
#ifndef SW_BENCH_H
#define SW_BENCH_H

#include "io.h"

static uint32_t bench_rng = 0x12345678u;

// xorshift32: no multiply, so it costs the same on rv32i as anywhere else.
static inline uint32_t rnd(void) {
  uint32_t v = bench_rng;
  v ^= v << 13;
  v ^= v >> 17;
  v ^= v << 5;
  return bench_rng = v;
}

#define CHECK(cond, code)                                    \
  do {                                                       \
    if (!(cond)) {                                           \
      printf_("check failed: %s (line %d)\n", #cond, __LINE__); \
      return (code);                                         \
    }                                                        \
  } while (0)

#endif
