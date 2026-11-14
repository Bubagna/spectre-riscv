// A tokenizer state machine over a pseudo-random text: branches and a switch whose
// outcomes follow the input characters, like a lexer or a protocol parser.
#include "bench.h"

#define LEN 3000

enum { S_SPACE, S_WORD, S_NUM, S_PUNCT };

static char text[LEN];

int main(void) {
  static const char alphabet[] = "  abcdefghij  0123456789 ,.;!  klmnopqrstuvwxyz  ";
  for (int i = 0; i < LEN; i++) text[i] = alphabet[rnd() % (sizeof alphabet - 1)];
  int words = 0, nums = 0, puncts = 0, state = S_SPACE;
  uint32_t numsum = 0, cur = 0;
  for (int i = 0; i < LEN; i++) {
    char c = text[i];
    int cls = (c >= 'a' && c <= 'z') ? S_WORD : (c >= '0' && c <= '9') ? S_NUM
            : (c == ' ') ? S_SPACE : S_PUNCT;
    switch (state) {
      case S_SPACE:
      case S_PUNCT:
        if (cls == S_WORD) words++;
        else if (cls == S_NUM) { nums++; cur = (uint32_t)(c - '0'); }
        else if (cls == S_PUNCT) puncts++;
        state = cls;
        break;
      case S_WORD:
        if (cls == S_NUM) { nums++; cur = (uint32_t)(c - '0'); state = S_NUM; }
        else if (cls != S_WORD) { if (cls == S_PUNCT) puncts++; state = cls; }
        break;
      case S_NUM:
        if (cls == S_NUM) cur = (cur << 3) + (cur << 1) + (uint32_t)(c - '0');
        else {
          numsum += cur;
          if (cls == S_WORD) words++;
          else if (cls == S_PUNCT) puncts++;
          state = cls;
        }
        break;
    }
  }
  if (state == S_NUM) numsum += cur;
  // Recount with a different structure: transitions into a class.
  int w2 = 0, n2 = 0, p2 = 0, prev = S_SPACE;
  for (int i = 0; i < LEN; i++) {
    char c = text[i];
    int cls = (c >= 'a' && c <= 'z') ? S_WORD : (c >= '0' && c <= '9') ? S_NUM
            : (c == ' ') ? S_SPACE : S_PUNCT;
    if (cls != prev || cls == S_PUNCT) {
      if (cls == S_WORD) w2++;
      if (cls == S_NUM) n2++;
      if (cls == S_PUNCT) p2++;
    }
    prev = cls;
  }
  CHECK(words == w2 && nums == n2 && puncts == p2, 1);
  printf_("fsm ok words=%d nums=%d punct=%d sum=%u\n", words, nums, puncts, numsum);
  return 0;
}
