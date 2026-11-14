/*
 * CoreMark port for spectre-riscv. Based on barebones/core_portme.c of EEMBC
 * CoreMark, Apache License 2.0. See core_portme.h for what this port changes.
 */
#include "coremark.h"
#include "io.h"

#if VALIDATION_RUN
volatile ee_s32 seed1_volatile = 0x3415;
volatile ee_s32 seed2_volatile = 0x3415;
volatile ee_s32 seed3_volatile = 0x66;
#endif
#if PERFORMANCE_RUN
volatile ee_s32 seed1_volatile = 0x0;
volatile ee_s32 seed2_volatile = 0x0;
volatile ee_s32 seed3_volatile = 0x66;
#endif
#if PROFILE_RUN
volatile ee_s32 seed1_volatile = 0x8;
volatile ee_s32 seed2_volatile = 0x8;
volatile ee_s32 seed3_volatile = 0x8;
#endif
volatile ee_s32 seed4_volatile = ITERATIONS;
volatile ee_s32 seed5_volatile = 0;

ee_u32 default_num_contexts = 1;

// No counter is read (see core_portme.h): time is always 0 ticks.
void start_time(void) {}
void stop_time(void) {}
CORE_TICKS get_time(void) { return 0; }
secs_ret time_in_secs(CORE_TICKS ticks) { return (secs_ret)ticks; }

// CoreMark keeps its error count local to main(). The CRC checks report through
// messages that start with "[n]ERROR!" (and "ERROR:" for the data-type checks),
// so ee_printf counts those; the 10-second rule message is the only "ERROR!" that
// starts without "[" and is deliberately not counted.
static int cm_errors;

int ee_printf(const char *fmt, ...) {
  if ((fmt[0] == '[' && fmt[1] == '%' && fmt[3] == ']' && fmt[4] == 'E') ||
      (fmt[0] == 'E' && fmt[5] == ':') || (fmt[0] == 'C' && fmt[1] == 'a') /* Cannot validate */)
    cm_errors++;
  va_list ap;
  va_start(ap, fmt);
  int n = vprintf_(fmt, ap);
  va_end(ap);
  return n;
}

void portable_init(core_portable *p, int *argc, char *argv[]) {
  (void)argc;
  (void)argv;
  if (sizeof(ee_ptr_int) != sizeof(ee_u8 *)) cm_errors++;
  if (sizeof(ee_u32) != 4) cm_errors++;
  p->portable_id = 1;
}

void portable_fini(core_portable *p) { p->portable_id = 0; }

// core_main.c is compiled with -Dmain=coremark_main (see mk/sw.mk).
int coremark_main(void);

int main(void) {
  coremark_main();
  if (cm_errors) {
    printf_("coremark: %d CRC/validation errors\n", cm_errors);
    return 1;
  }
  printf_("coremark ok (CRCs validated; timing comes from the simulator stats line)\n");
  return 0;
}
