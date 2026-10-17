/*
** Bounded process-wide arena for the isolated LuaJIT 2.0.4 prototype.
** The reservation is stable until library retirement. No Lua state owns it.
** Each map owns an extent; guard granules prevent dlmalloc merging extents
** belonging to independent states. Decommit never releases the reservation.
*/
#include "lj_compat_arena.h"
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <errno.h>
#include <sys/mman.h>
#include <sched.h>
#endif

#define ARENA_CAP ((size_t)0x80000000)
#define ARENA_ALIGN UINT64_C(0x100000000)
#define ARENA_UNIT ((size_t)65536)
#define ARENA_SLOTS (ARENA_CAP / ARENA_UNIT)
#define ARENA_FAIL ((void *)(uintptr_t)-1)

uintptr_t lj_compat_arena_base;
static uint32_t arena_owner[ARENA_SLOTS];
static volatile int arena_lock;

static void lock_arena(void)
{
  while (__sync_lock_test_and_set(&arena_lock, 1)) {
#ifdef _WIN32
    Sleep(0);
#else
    sched_yield();
#endif
  }
}

static void unlock_arena(void)
{
  __sync_lock_release(&arena_lock);
}

static int reserve_arena(void)
{
  if (lj_compat_arena_base) return 1;
#ifdef _WIN32
  {
    uintptr_t base;
    /* Probe exact aligned reservations, never replace existing mappings. */
    for (base = ARENA_ALIGN; base < UINT64_C(0x800000000000); base += ARENA_ALIGN) {
      void *p = VirtualAlloc((void *)base, ARENA_CAP, MEM_RESERVE, PAGE_NOACCESS);
      if (p) {
        if ((uintptr_t)p != base) { VirtualFree(p, 0, MEM_RELEASE); continue; }
        lj_compat_arena_base = base;
        return 1;
      }
    }
  }
#else
  {
    size_t total = ARENA_CAP + (size_t)ARENA_ALIGN;
    void *p = mmap(NULL, total, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
    uintptr_t start, base;
    size_t prefix, suffix;
    if (p == MAP_FAILED) return 0;
    start = (uintptr_t)p;
    base = (start + ARENA_ALIGN - 1) & ~(uintptr_t)(ARENA_ALIGN - 1);
    if (base < ARENA_ALIGN) { munmap(p, total); return 0; }
    prefix = (size_t)(base - start);
    suffix = total - prefix - ARENA_CAP;
    if (prefix) munmap(p, prefix);
    if (suffix) munmap((void *)(base + ARENA_CAP), suffix);
    lj_compat_arena_base = base;
    return 1;
  }
#endif
  return 0;
}

/* Codecs validate representation, not liveness: GC may retain dead hash keys,
** and stack/bytecode cursors may be one past their allocated object. */
uint32_t lj_compat_ref_encode(const void *p)
{
  uintptr_t offset;
  if (!p) return 0;
  offset = (uintptr_t)p - lj_compat_arena_base;
  if (!lj_compat_arena_base || offset < ARENA_UNIT || offset > ARENA_CAP)
    abort();
  return (uint32_t)offset;
}

void *lj_compat_ref_decode(uint32_t ref)
{
  if (!ref) return NULL;
  if (!lj_compat_arena_base || ref < ARENA_UNIT || ref > ARENA_CAP)
    abort();
  return (void *)(lj_compat_arena_base + ref);
}

int lj_compat_contains(const void *p, size_t size)
{
  uintptr_t offset;
  size_t first, last, i;
  int ok = 0;
  lock_arena();
  offset = (uintptr_t)p - lj_compat_arena_base;
  if (!lj_compat_arena_base || offset < ARENA_UNIT || offset > ARENA_CAP ||
      size > ARENA_CAP - offset) goto done;
  if (!size) { ok = 1; goto done; }
  first = offset / ARENA_UNIT;
  last = (offset + size - 1) / ARENA_UNIT;
  if (!arena_owner[first]) goto done;
  for (i = first; i <= last; i++)
    if (arena_owner[i] != arena_owner[first]) goto done;
  ok = 1;
done:
  unlock_arena();
  return ok;
}

void *lj_compat_arena_map(size_t size)
{
  size_t count, i, start = 0, run = 0;
  void *p = ARENA_FAIL;
#ifdef _WIN32
  DWORD olderr = GetLastError();
#else
  int olderr = errno;
#endif
  if (!size || size > ARENA_CAP - 2 * ARENA_UNIT) goto done;
  count = (size + ARENA_UNIT - 1) / ARENA_UNIT;
  lock_arena();
  if (!reserve_arena()) goto unlock;
  for (i = 1; i < ARENA_SLOTS; i++) {
    if (arena_owner[i] || (i > 1 && arena_owner[i-1])) { run = 0; continue; }
    if (!run) start = i;
    run++;
    if (run == count && (i + 1 == ARENA_SLOTS || !arena_owner[i+1])) {
      p = (void *)(lj_compat_arena_base + start * ARENA_UNIT);
#ifdef _WIN32
      if (!VirtualAlloc(p, count * ARENA_UNIT, MEM_COMMIT, PAGE_READWRITE))
#else
      if (mprotect(p, count * ARENA_UNIT, PROT_READ | PROT_WRITE) != 0)
#endif
      { p = ARENA_FAIL; goto unlock; }
      for (i = start; i < start + count; i++) arena_owner[i] = (uint32_t)start;
      goto unlock;
    }
  }
unlock:
  unlock_arena();
done:
#ifdef _WIN32
  SetLastError(olderr);
#else
  errno = olderr;
#endif
  return p;
}

int lj_compat_arena_unmap(void *p, size_t size)
{
  uintptr_t offset;
  size_t first, count, i;
  int result = -1;
#ifdef _WIN32
  DWORD olderr = GetLastError();
#else
  int olderr = errno;
#endif
  lock_arena();
  offset = (uintptr_t)p - lj_compat_arena_base;
  if (!lj_compat_arena_base || offset < ARENA_UNIT || offset >= ARENA_CAP ||
      offset % ARENA_UNIT || !size ||
      size > ARENA_CAP - offset) goto done;
  size = (size + ARENA_UNIT - 1) & ~(ARENA_UNIT - 1);
  first = offset / ARENA_UNIT;
  count = size / ARENA_UNIT;
  if (!arena_owner[first]) goto done;
  for (i = first; i < first + count; i++)
    if (arena_owner[i] != arena_owner[first]) goto done;
#ifdef _WIN32
  if (!VirtualFree(p, size, MEM_DECOMMIT)) goto done;
#else
  /* MAP_FIXED replaces only our validated private extent, restoring a zeroed,
  ** inaccessible reservation in one syscall without an address-space hole. */
  if (mmap(p, size, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0) == MAP_FAILED)
    goto done;
#endif
  for (i = first; i < first + count; i++) arena_owner[i] = 0;
  result = 0;
done:
  unlock_arena();
#ifdef _WIN32
  SetLastError(olderr);
#else
  errno = olderr;
#endif
  return result;
}

/* Release at DLL/shared-object unload only. Ordinary lua_close retains the
** single bounded reservation, so repeated states do not reserve more arenas.
** Unloading a library with live states is invalid, like the upstream ABI. */
__attribute__((destructor)) static void retire_arena(void)
{
  if (!lj_compat_arena_base) return;
#ifdef _WIN32
  VirtualFree((void *)lj_compat_arena_base, 0, MEM_RELEASE);
#else
  munmap((void *)lj_compat_arena_base, ARENA_CAP);
#endif
}
