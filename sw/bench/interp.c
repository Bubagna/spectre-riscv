// A small register-machine interpreter. The dispatch switch compiles to a jump table,
// so every bytecode is an indirect jump whose target depends on the program: the
// classic case where a BTB alone mispredicts and global history helps.
#include "bench.h"

enum { LI, MOV, ADD, INC, DEC, ANDI, SHR, JZ, JNZ, JMP, XOR, HALT };

typedef struct { uint8_t op, a, b; } insn_t;  // b: register, immediate or target

// Total Collatz steps for n = N..1 (3x+1 is x+x+x+1: rv32i has no multiply anyway).
static const insn_t collatz[] = {
  /*  0 */ {LI, 0, 100},
  /*  1 */ {MOV, 1, 0},    // outer: x = n
  /*  2 */ {MOV, 3, 1},    // inner: if x == 1 goto next
  /*  3 */ {DEC, 3, 0},
  /*  4 */ {JZ, 3, 16},
  /*  5 */ {INC, 2, 0},    // steps++
  /*  6 */ {MOV, 3, 1},
  /*  7 */ {ANDI, 3, 1},
  /*  8 */ {JNZ, 3, 11},
  /*  9 */ {SHR, 1, 0},    // even: x /= 2
  /* 10 */ {JMP, 0, 2},
  /* 11 */ {MOV, 3, 1},    // odd: x = 3x + 1
  /* 12 */ {ADD, 1, 3},
  /* 13 */ {ADD, 1, 3},
  /* 14 */ {INC, 1, 0},
  /* 15 */ {JMP, 0, 2},
  /* 16 */ {DEC, 0, 0},    // next: n--
  /* 17 */ {JNZ, 0, 1},
  /* 18 */ {HALT, 2, 0},
};

static uint32_t run(const insn_t *code) {
  uint32_t r[4] = {0, 0, 0, 0};
  int pc = 0;
  for (;;) {
    insn_t in = code[pc++];
    switch (in.op) {
      case LI:   r[in.a] = in.b; break;
      case MOV:  r[in.a] = r[in.b]; break;
      case ADD:  r[in.a] += r[in.b]; break;
      case INC:  r[in.a]++; break;
      case DEC:  r[in.a]--; break;
      case ANDI: r[in.a] &= in.b; break;
      case SHR:  r[in.a] >>= 1; break;
      case JZ:   if (!r[in.a]) pc = in.b; break;
      case JNZ:  if (r[in.a]) pc = in.b; break;
      case JMP:  pc = in.b; break;
      case XOR:  r[in.a] ^= r[in.b]; break;
      default:   return r[in.a];
    }
  }
}

static uint32_t collatz_ref(uint32_t n) {
  uint32_t steps = 0;
  for (; n; n--)
    for (uint32_t x = n; x != 1; steps++) x = (x & 1) ? 3 * x + 1 : x >> 1;
  return steps;
}

// Straight-line random bytecode (no jumps): the dispatch target is pseudo-random.
#define LEN 2000
static insn_t stream[LEN + 1];

int main(void) {
  uint32_t a = run(collatz);
  CHECK(a == collatz_ref(100), 1);
  for (int i = 0; i < LEN; i++) {
    uint32_t v = rnd();
    static const uint8_t ops[] = {LI, MOV, ADD, INC, DEC, ANDI, SHR, XOR};
    stream[i].op = ops[v & 7];
    stream[i].a = (v >> 8) & 3;
    stream[i].b = (stream[i].op == LI || stream[i].op == ANDI) ? (uint8_t)(v >> 16) : (v >> 12) & 3;
  }
  stream[LEN] = (insn_t){HALT, 0, 0};
  uint32_t b = 0;
  for (int rep = 0; rep < 4; rep++) b += run(stream);
  CHECK(b == 4 * run(stream), 2);
  printf_("interp ok collatz=%u stream=%08x\n", a, b);
  return 0;
}
