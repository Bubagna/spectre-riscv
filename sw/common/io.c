// printf-lite and the few libc functions GCC may call on its own.
#include "io.h"

void putchar_(char c) { *MMIO_PUTCHAR = (uint8_t)c; }

void puts_(const char *s) {
  while (*s) putchar_(*s++);
}

static int put_uint(uint32_t v, unsigned base, int upper, int width, char pad) {
  char buf[12];
  const char *digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
  int n = 0;
  do {
    buf[n++] = digits[v % base];
    v /= base;
  } while (v);
  int out = 0;
  for (; width > n; width--, out++) putchar_(pad);
  while (n) { putchar_(buf[--n]); out++; }
  return out;
}

int vprintf_(const char *fmt, va_list ap) {
  int out = 0;
  for (; *fmt; fmt++) {
    if (*fmt != '%') { putchar_(*fmt); out++; continue; }
    fmt++;
    char pad = ' ';
    int width = 0;
    if (*fmt == '0') { pad = '0'; fmt++; }
    while (*fmt >= '0' && *fmt <= '9') width = width * 10 + (*fmt++ - '0');
    while (*fmt == 'l') fmt++;
    switch (*fmt) {
      case 'd': case 'i': {
        int32_t v = va_arg(ap, int32_t);
        uint32_t u = (uint32_t)v;
        if (v < 0) {
          u = -u;
          if (pad == '0') { putchar_('-'); out++; width--; }
          else {
            // Count the sign in the width when padding with spaces.
            uint32_t t = u; int n = 1;
            while (t >= 10) { t /= 10; n++; }
            for (; width > n + 1; width--, out++) putchar_(' ');
            putchar_('-'); out++; width = 0;
          }
        }
        out += put_uint(u, 10, 0, width, pad);
        break;
      }
      case 'u': out += put_uint(va_arg(ap, uint32_t), 10, 0, width, pad); break;
      case 'x': out += put_uint(va_arg(ap, uint32_t), 16, 0, width, pad); break;
      case 'X': out += put_uint(va_arg(ap, uint32_t), 16, 1, width, pad); break;
      case 'p': puts_("0x"); out += 2 + put_uint((uint32_t)va_arg(ap, void *), 16, 0, 8, '0'); break;
      case 'c': putchar_((char)va_arg(ap, int)); out++; break;
      case 's': {
        const char *s = va_arg(ap, const char *);
        int n = (int)strlen(s);
        for (; width > n; width--, out++) putchar_(' ');
        puts_(s); out += n;
        break;
      }
      case '%': putchar_('%'); out++; break;
      case '\0': return out;
      default: putchar_('%'); putchar_(*fmt); out += 2; break;
    }
  }
  return out;
}

int printf_(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  int n = vprintf_(fmt, ap);
  va_end(ap);
  return n;
}

void *memset(void *d, int c, size_t n) {
  uint8_t *p = d;
  while (n--) *p++ = (uint8_t)c;
  return d;
}

void *memcpy(void *d, const void *s, size_t n) {
  uint8_t *p = d;
  const uint8_t *q = s;
  while (n--) *p++ = *q++;
  return d;
}

void *memmove(void *d, const void *s, size_t n) {
  uint8_t *p = d;
  const uint8_t *q = s;
  if (p < q) { while (n--) *p++ = *q++; }
  else { p += n; q += n; while (n--) *--p = *--q; }
  return d;
}

int memcmp(const void *a, const void *b, size_t n) {
  const uint8_t *p = a, *q = b;
  for (; n; n--, p++, q++)
    if (*p != *q) return *p - *q;
  return 0;
}

size_t strlen(const char *s) {
  size_t n = 0;
  while (s[n]) n++;
  return n;
}
