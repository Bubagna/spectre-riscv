// Bitwise CRC-32 (one data-dependent branch per bit) and a table-driven CRC-32
// that must agree with it.
#include "bench.h"

#define LEN 2048

static uint8_t buf[LEN];
static uint32_t table[256];

static uint32_t crc_bitwise(const uint8_t *p, int n) {
  uint32_t c = 0xFFFFFFFFu;
  for (int i = 0; i < n; i++) {
    c ^= p[i];
    for (int k = 0; k < 8; k++) {
      if (c & 1) c = (c >> 1) ^ 0xEDB88320u;
      else c >>= 1;
    }
  }
  return ~c;
}

static uint32_t crc_table(const uint8_t *p, int n) {
  uint32_t c = 0xFFFFFFFFu;
  for (int i = 0; i < n; i++) c = table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
  return ~c;
}

int main(void) {
  for (uint32_t i = 0; i < 256; i++) {
    uint32_t c = i;
    for (int k = 0; k < 8; k++) c = (c & 1) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
    table[i] = c;
  }
  // Known answer: CRC-32 of "123456789" is 0xCBF43926.
  CHECK(crc_bitwise((const uint8_t *)"123456789", 9) == 0xCBF43926u, 1);
  for (int i = 0; i < LEN; i++) buf[i] = (uint8_t)rnd();
  uint32_t a = crc_bitwise(buf, LEN);
  uint32_t b = crc_table(buf, LEN);
  CHECK(a == b, 2);
  printf_("crc ok %08x\n", a);
  return 0;
}
