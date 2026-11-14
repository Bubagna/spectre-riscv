// A Dhrystone-like mix: small functions, struct copies, string compare and copy,
// a switch, pointer chasing through a linked list, and libgcc multiply/divide.
#include "bench.h"

typedef struct node { struct node *next; int32_t val; char name[12]; } node_t;

#define NODES 64
static node_t pool[NODES];

static int str_eq(const char *a, const char *b) {
  while (*a && *a == *b) { a++; b++; }
  return *a == *b;
}

static void str_cpy(char *d, const char *s) { while ((*d++ = *s++)) {} }

static int classify(int32_t v) {
  switch ((v >> 3) & 7) {
    case 0: return v + 1;
    case 1: return v - 3;
    case 2: return v ^ 0x55;
    case 3: return v << 1;
    case 4: return v >> 2;
    case 5: return -v;
    case 6: return v & 0xFF;
    default: return 7;
  }
}

static int32_t walk(node_t *n, int steps) {
  int32_t acc = 0;
  while (steps--) { acc += classify(n->val); n = n->next; }
  return acc;
}

int main(void) {
  static const char *names[] = {"alpha", "beta", "gamma", "delta"};
  for (int i = 0; i < NODES; i++) {
    pool[i].next = &pool[rnd() % NODES];
    pool[i].val = (int32_t)(rnd() & 0xFFF);
    str_cpy(pool[i].name, names[i & 3]);
  }
  int32_t acc = 0;
  int matches = 0;
  for (int rep = 0; rep < 40; rep++) {
    node_t tmp = pool[rep % NODES];  // struct copy
    acc += walk(&tmp, 50);
    if (str_eq(tmp.name, "gamma")) matches++;
    acc += (int32_t)((uint32_t)tmp.val * 2654435761u >> 20);  // __mulsi3
    acc += tmp.val / 7 - tmp.val % 5;                         // __divsi3, __modsi3
  }
  CHECK(matches == 10, 1);
  printf_("mix ok %d %d\n", acc, matches);
  return 0;
}
