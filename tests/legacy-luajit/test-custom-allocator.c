/* Link the actual lj_gc.c allocator functions; dead-strip unrelated GC code. */
#include "lj_obj.h"
#include "lj_alloc.h"
#include "lj_gc.h"
#include "lj_err.h"
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#ifndef _WIN32
#include <sys/wait.h>
#include <unistd.h>
#include <signal.h>
#endif

enum { SHRINK_ONLY, SHRINK_MOVE, GROW_NULL, GROW_OUTSIDE, GROW_VALID,
       SHRINK_NULL, SHRINK_OUTSIDE };
static void *heap, *outside;
static lua_State *L;
static global_State *g;
static unsigned char *original;
static int mode;
static unsigned fresh_calls, resize_calls, old_frees, outside_frees;
static jmp_buf oom;

static void *controlled_alloc(void *ud, void *p, size_t osz, size_t nsz)
{
  (void)ud;
  if (!nsz) {
    if (p == outside && outside) {
      free(outside); outside = NULL; outside_frees++;
    } else {
      if (p == original) old_frees++;
      lj_alloc_f(heap, p, osz, 0);
    }
    return NULL;
  }
  if (!p) {
    fresh_calls++;
    if (mode == GROW_OUTSIDE) return outside = malloc(nsz);
    if (mode == GROW_VALID) return lj_alloc_f(heap, NULL, 0, nsz);
    return NULL;  /* The shrinking allocator has no fresh capacity. */
  }
  resize_calls++;
  assert(nsz <= osz);
  if (mode == SHRINK_NULL) return NULL;
  if (mode == SHRINK_MOVE || mode == SHRINK_OUTSIDE) {
    void *q = mode == SHRINK_MOVE ? lj_alloc_f(heap, NULL, 0, nsz) :
                                  (outside = malloc(nsz));
    assert(q);
    memcpy(q, p, nsz);
    controlled_alloc(ud, p, osz, 0);
    return q;
  }
  return p;
}

LJ_FUNC_NORET void lj_err_mem(lua_State *state)
{
  (void)state;
  longjmp(oom, 1);
}

static void reset_request(int allocator_mode)
{
  mode = allocator_mode;
  original = lj_alloc_f(heap, NULL, 0, 64);
  assert(original);
  memset(original, 0x5a, 64);
  g->gc.total = 64;
  fresh_calls = resize_calls = old_frees = outside_frees = 0;
}

static void reject_growth(int allocator_mode)
{
  reset_request(allocator_mode);
  if (!setjmp(oom)) {
    lj_mem_realloc(L, original, 64, 128);
    assert(!"invalid growth must raise memory error");
  }
  assert(fresh_calls == 1 && resize_calls == 0 && old_frees == 0);
  assert(g->gc.total == 64 && original[0] == 0x5a && original[63] == 0x5a);
  assert(outside_frees == (unsigned)(allocator_mode == GROW_OUTSIDE));
  assert(!outside);
  lj_alloc_f(heap, original, 64, 0);
}

#ifndef _WIN32
static void reject_shrink(int allocator_mode)
{
  pid_t child = fork();
  int status;
  assert(child >= 0);
  if (!child) {
    reset_request(allocator_mode);
    /* An invalid shrink must not return or raise a recoverable memory error. */
    if (setjmp(oom)) _exit(77);
    lj_mem_realloc(L, original, 64, 32);
    _exit(78);
  }
  assert(waitpid(child, &status, 0) == child);
  assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGABRT);
}
#endif

int main(void)
{
  unsigned char *result;
  heap = lj_alloc_create();
  assert(heap);
  L = lj_alloc_f(heap, NULL, 0, sizeof(*L));
  g = lj_alloc_f(heap, NULL, 0, sizeof(*g));
  assert(L && g);
  memset(L, 0, sizeof(*L)); memset(g, 0, sizeof(*g));
  setmref(L->glref, g); g->allocf = controlled_alloc;

  if (setjmp(oom)) {
    fprintf(stderr, "unexpected OOM: mode=%d fresh=%u resize=%u\n",
            mode, fresh_calls, resize_calls);
    return 1;
  }
  reset_request(SHRINK_ONLY);
  result = lj_mem_realloc(L, original, 64, 32);
  assert(result == original && result[31] == 0x5a && g->gc.total == 32);
  assert(!fresh_calls && resize_calls == 1);
  result = lj_mem_realloc(L, result, 32, 32);
  assert(result == original && !fresh_calls && resize_calls == 2);
  assert(lj_mem_realloc(L, result, 32, 0) == NULL && g->gc.total == 0);

  reset_request(SHRINK_MOVE);
  result = lj_mem_realloc(L, original, 64, 32);
  assert(result != original && lj_compat_contains(result, 32));
  assert(result[31] == 0x5a && g->gc.total == 32);
  assert(!fresh_calls && resize_calls == 1 && old_frees == 1);
  lj_alloc_f(heap, result, 32, 0);

  reset_request(GROW_VALID);
  result = lj_mem_realloc(L, original, 64, 128);
  assert(lj_compat_contains(result, 128) && result[63] == 0x5a);
  assert(g->gc.total == 128 && fresh_calls == 1 && old_frees == 1);
  lj_alloc_f(heap, result, 128, 0);

  reject_growth(GROW_NULL);
  reject_growth(GROW_OUTSIDE);
#ifndef _WIN32
  reject_shrink(SHRINK_NULL);
  reject_shrink(SHRINK_OUTSIDE);
#endif
  lj_alloc_destroy(heap);
  puts("custom allocator: shrink/equal/moving-shrink/growth/rejected-growth-preservation PASS");
  return 0;
}
