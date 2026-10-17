#include "lua.h"
#include "lj_obj.h"
#include "lj_alloc.h"
#include "lj_state.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Budget {
  void *heap;
  long left;
  size_t outstanding;
  unsigned rejected;
} Budget;

/* Fail only growing requests. Shrinks/frees delegate unchanged to dlmalloc. */
static void *budget_alloc(void *ud, void *p, size_t osz, size_t nsz)
{
  Budget *b = ud;
  void *q;
  if (nsz > osz && b->left == 0) { b->rejected++; return NULL; }
  if (nsz > osz && b->left > 0) b->left--;
  q = lj_alloc_f(b->heap, p, osz, nsz);
  if (q || !nsz) b->outstanding = b->outstanding - osz + nsz;
  return q;
}

int main(int argc, char **argv)
{
  long first = argc > 1 ? strtol(argv[1], NULL, 10) : 0;
  long last = argc > 1 ? first + 1 : 96;
  long i;
  unsigned failures = 0, successes = 0;
  setvbuf(stdout, NULL, _IONBF, 0);
  for (i = first; i < last; i++) {
    Budget b;
    lua_State *L;
    b.heap = lj_alloc_create(); b.left = i;
    b.outstanding = 0; b.rejected = 0;
    assert(b.heap);
    printf("budget=%ld heap=%p\n", i, b.heap);
    L = lj_state_newstate(budget_alloc, &b);
    if (L) { successes++; lua_close(L); }
    else { failures++; assert(b.rejected); }
    assert(!b.outstanding);
    lj_alloc_destroy(b.heap);
  }
  if (argc == 1) assert(failures && successes);
  printf("PASS initialization failure cleanup: failures=%u successes=%u\n", failures, successes);
  return 0;
}
