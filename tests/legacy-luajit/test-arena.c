#include "lj_compat_arena.h"
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <signal.h>
#ifdef TEST_DLMALLOC
#include "lj_alloc.h"
#include "lj_obj.h"
#endif

#define UNIT ((size_t)65536)
#define CAP ((size_t)0x80000000)
#define BAD ((void *)(uintptr_t)-1)

static void dies(int which)
{
  pid_t p = fork();
  int status;
  assert(p >= 0);
  if (!p) {
    if (which == 0) lj_compat_ref_encode((void *)42);
    if (which == 1) lj_compat_ref_decode(42);
    if (which == 2) lj_compat_ref_decode(0x80000001u);
    if (which == 3) *(volatile char *)lj_compat_arena_base = 1;
    if (which == 4) {
      char *p = lj_compat_arena_map(UNIT);
      assert(p != BAD);
      assert(!lj_compat_arena_unmap(p, UNIT));
      *(volatile char *)p = 1;
    }
    _exit(0);
  }
  assert(waitpid(p, &status, 0) == p);
  assert(WIFSIGNALED(status));
}

#ifdef TEST_DLMALLOC
static void *allocator_test(void *arg)
{
  void *state = lj_alloc_create();
  unsigned char *p, *q;
  size_t i;
  (void)arg;
  assert(state && lj_compat_contains(state, 1));
  p = lj_alloc_f(state, NULL, 0, 64);
  assert(p);
  memset(p, 0x6d, 64);
  assert(lj_alloc_f(state, p, 64, CAP) == NULL);
  assert(p[0] == 0x6d && p[63] == 0x6d);
  assert(lj_alloc_f(state, p, 64, 0) == NULL);
  assert(sizeof(MRef) == 4 && sizeof(GCRef) == 4 && sizeof(TValue) == 8);
  {
    MRef ref;
    GCRef gcrefslot;
    setmref(ref, state);
    setgcrefp(gcrefslot, state);
    assert(mref(ref, char) == state && gcrefp(gcrefslot, char) == state);
    assert(ref.ptr32 == (uintptr_t)state - lj_compat_arena_base);
    setmref(ref, NULL);
    setgcrefp(gcrefslot, NULL);
    assert(!mref(ref, char) && !gcrefp(gcrefslot, char));
  }
  for (i = 1; i < 100; i++) {
    size_t small = i * 791, large = UNIT * 4 + i * 17;
    p = lj_alloc_f(state, NULL, 0, small);
    assert(p && lj_compat_contains(p, small));
    memset(p, (int)i, small);
    q = lj_alloc_f(state, p, small, large);
    assert(q && lj_compat_contains(q, large));
    assert(q[0] == (unsigned char)i && q[small - 1] == (unsigned char)i);
    memset(q, (int)i, large);
    p = lj_alloc_f(state, q, large, 19);
    assert(p && lj_compat_contains(p, 19) && p[18] == (unsigned char)i);
    assert(lj_alloc_f(state, p, 19, 0) == NULL);
  }
  lj_alloc_destroy(state);
  return NULL;
}
#endif

static void *thread_test(void *arg)
{
  unsigned i;
  unsigned char value = (unsigned char)(uintptr_t)arg;
  for (i = 0; i < 300; i++) {
    unsigned char *p = lj_compat_arena_map(UNIT * 2);
    assert(p != BAD);
    memset(p, value, UNIT * 2);
    assert(p[0] == value && p[UNIT * 2 - 1] == value);
    assert(lj_compat_ref_decode(lj_compat_ref_encode(p + 123)) == p + 123);
    assert(lj_compat_arena_unmap(p, UNIT * 2) == 0);
  }
  return NULL;
}

int main(void)
{
  unsigned char *a, *b, *c;
  pthread_t threads[8];
  unsigned i;
  assert(lj_compat_ref_encode(NULL) == 0);
  assert(lj_compat_ref_decode(0) == NULL);
  assert(!lj_compat_contains((void *)42, 1));
  assert(lj_compat_arena_map(0) == BAD);
  assert(lj_compat_arena_map(CAP) == BAD);
  assert(lj_compat_arena_map((size_t)-1) == BAD);
  errno = EDOM;
  a = lj_compat_arena_map(3 * UNIT);
  assert(a != BAD && errno == EDOM);
  assert(lj_compat_arena_base >= UINT64_C(0x100000000));
  assert((lj_compat_arena_base & UINT64_C(0xffffffff)) == 0);
  assert((uintptr_t)a >= lj_compat_arena_base + UNIT);
  assert(lj_compat_contains(a, 3 * UNIT));
  assert(!lj_compat_contains(a, (size_t)-1));
  assert(!lj_compat_contains((void *)lj_compat_arena_base, 1));
  assert(lj_compat_ref_decode(lj_compat_ref_encode(a + 3 * UNIT)) == a + 3 * UNIT);
  assert(lj_compat_ref_decode(0x80000000u) == (void *)(lj_compat_arena_base + CAP));
  b = lj_compat_arena_map(UNIT);
  assert(b != BAD && b != a);
  memset(a, 0x3c, 3 * UNIT);
  memcpy(b, a, UNIT);
  memmove(a + 17, a, UNIT);
  assert(b[UNIT - 1] == 0x3c && a[UNIT + 16] == 0x3c);
  assert(lj_compat_arena_unmap(a + 1, UNIT) == -1);
  errno = EDOM;
  assert(lj_compat_arena_unmap(a, (size_t)((uintptr_t)b - (uintptr_t)a) + UNIT) == -1);
  assert(errno == EDOM);
  assert(b[UNIT - 1] == 0x3c);
  assert(lj_compat_arena_unmap(a + 2 * UNIT, UNIT) == 0);
  assert(!lj_compat_contains(a + 2 * UNIT, 1));
  assert(lj_compat_contains(a, 2 * UNIT));
  assert(lj_compat_arena_unmap(a, 2 * UNIT) == 0);
  assert(lj_compat_arena_unmap(a, UNIT) == -1);
  c = lj_compat_arena_map(3 * UNIT);
  assert(c == a && c[0] == 0 && c[3 * UNIT - 1] == 0);
  assert(b[0] == 0x3c && b[UNIT - 1] == 0x3c);
  assert(lj_compat_arena_unmap(c, 3 * UNIT) == 0);
  assert(lj_compat_arena_unmap(b, UNIT) == 0);
  c = lj_compat_arena_map(37);
  assert(c != BAD && lj_compat_contains(c, 37));
  c[36] = 0x7a;
  assert(lj_compat_arena_unmap(c, 37) == 0);
  c = lj_compat_arena_map(CAP - 2 * UNIT);
  assert(c != BAD);
  assert(lj_compat_arena_map(UNIT) == BAD);
  c[0] = 7;
  c[CAP - 2 * UNIT - 1] = 9;
  assert(lj_compat_arena_unmap(c, CAP - 2 * UNIT) == 0);
  for (i = 0; i < 5; i++) dies(i);
  for (i = 0; i < 8; i++) assert(!pthread_create(&threads[i], NULL, thread_test, (void *)(uintptr_t)(i + 1)));
  for (i = 0; i < 8; i++) assert(!pthread_join(threads[i], NULL));
#ifdef TEST_DLMALLOC
  for (i = 0; i < 8; i++) assert(!pthread_create(&threads[i], NULL, allocator_test, NULL));
  for (i = 0; i < 8; i++) assert(!pthread_join(threads[i], NULL));
  puts("original dlmalloc: concurrent independent heaps/create/realloc/free/destroy PASS");
#endif
  puts("arena: null/bounds/high-address/one-past/native-copy/reuse/ownership/threads PASS");
  return 0;
}
