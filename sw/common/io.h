// Minimal I/O for bare-metal programs: output on the PUTCHAR MMIO register.
#ifndef SW_COMMON_IO_H
#define SW_COMMON_IO_H

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

#define MMIO_TOHOST  ((volatile uint32_t *)0x10000000u)
#define MMIO_PUTCHAR ((volatile uint32_t *)0x10000004u)
#define MMIO_HALT    ((volatile uint32_t *)0x10000008u)

void putchar_(char c);
void puts_(const char *s);
// Supports %d %i %u %x %X %c %s %p %%, an optional '0' flag, a width and the 'l' length (ignored, ILP32).
int printf_(const char *fmt, ...);
int vprintf_(const char *fmt, va_list ap);

// Counter reads (Zicntr). Values differ between RTL and ISS; never print them
// in programs used for lockstep comparison unless the output is not compared.
static inline uint32_t rdcycle_(void)   { uint32_t v; __asm__ volatile("rdcycle %0" : "=r"(v)); return v; }
static inline uint32_t rdinstret_(void) { uint32_t v; __asm__ volatile("rdinstret %0" : "=r"(v)); return v; }

void *memset(void *d, int c, size_t n);
void *memcpy(void *d, const void *s, size_t n);
void *memmove(void *d, const void *s, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);

#endif
