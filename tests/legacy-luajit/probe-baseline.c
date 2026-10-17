/* Owned baseline harness; LuaJIT itself is the unmodified pinned 2.0.4. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/mman.h>
#endif
#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"

/* Upstream x64 public lua_newstate rejects custom allocators outright. This
 * separately named internal entry establishes the actual checkptr32 failure. */
extern lua_State *lj_state_newstate(lua_Alloc f, void *ud);

typedef struct {
  void *mapping;
  size_t capacity;
  unsigned allocations, frees;
  size_t requested;
} HighAllocator;

static int release_mapping(HighAllocator *a)
{
  if (!a->mapping) return 0;
#ifdef _WIN32
  if (!VirtualFree(a->mapping, 0, MEM_RELEASE)) return -1;
#else
  if (munmap(a->mapping, a->capacity)) return -1;
#endif
  a->mapping = NULL;
  return 0;
}

static void *high_allocate(void *ud, void *ptr, size_t oldsize, size_t newsize)
{
  HighAllocator *a = (HighAllocator *)ud;
  (void)oldsize;
  if (!newsize) {
    a->frees++;
    if (ptr == a->mapping && release_mapping(a)) abort();
    return NULL;
  }
  a->allocations++;
  a->requested = newsize;
  /* This probe intentionally permits only the real initial GG_State request. */
  if (ptr || a->allocations != 1 || newsize > a->capacity) return NULL;
  return a->mapping;
}

static int high_probe(int internal)
{
  HighAllocator a = {0};
  lua_State *L;
  uintptr_t address;
  a.capacity = 128u * 1024u;
#ifdef _WIN32
  a.mapping = VirtualAlloc((void *)(uintptr_t)0x200000000ULL, a.capacity,
                          MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
#else
  a.mapping = mmap((void *)(uintptr_t)0x200000000ULL, a.capacity,
                   PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  if (a.mapping == MAP_FAILED) a.mapping = NULL;
#endif
  address = (uintptr_t)a.mapping;
  if (!a.mapping || address <= UINT32_MAX) {
    if (release_mapping(&a)) return 2;
    fprintf(stderr, "INFRA_ERROR: real allocation above4GiB unavailable\n");
    return 2;
  }
  memset(a.mapping, 0xa5, a.capacity); /* Verify a real writable allocation. */
  L = internal ? lj_state_newstate(high_allocate, &a)
               : lua_newstate(high_allocate, &a);
  printf("constructor=%s allocation=%p state=%p callback_allocs=%u "
         "callback_frees=%u requested=%zu\n",
         internal ? "internal_lj_state_newstate" : "public_lua_newstate",
         (void *)address, (void *)L, a.allocations, a.frees, a.requested);
  if (L) lua_close(L);
  /* Exact upstream rejects before allocator cleanup; the harness owns cleanup. */
  if (release_mapping(&a)) {
    fprintf(stderr, "INFRA_ERROR: harness mapping cleanup failed\n");
    return 2;
  }
  puts("harness_mapping_cleanup=complete");
  if (L || a.allocations != (internal ? 1u : 0u) || a.frees != 0u) {
    fputs("UNEXPECTED: original constructor behavior changed\n", stderr);
    return 2;
  }
  puts(internal ? "RUNTIME_RED: checkptr32 rejects real high allocation"
                : "RUNTIME_RED: original public API rejects custom allocator");
  return 1; /* A compiled probe running and rejecting the new runtime contract. */
}

static int normal_probe(const char *dump_path)
{
  lua_State *L = luaL_newstate();
  FILE *out;
  const char *owned =
    "local function twice(x) return x*2 end "
    "return twice(19)+4, 'owned-v1-reference'";
  if (!L) { fputs("normal state creation failed\n", stderr); return 2; }
  luaL_openlibs(L);
  if (luaL_loadstring(L, owned)) goto lua_error;
  if (dump_path) {
    /* string.dump emits the exact upstream bytecode format without running it. */
    size_t size;
    const char *bytes;
    lua_getglobal(L, "string");
    lua_getfield(L, -1, "dump");
    lua_pushvalue(L, 1);
    if (lua_pcall(L, 1, 1, 0)) goto lua_error;
    bytes = lua_tolstring(L, -1, &size);
    if (!bytes || size < 4 || memcmp(bytes, "\033LJ\001", 4)) {
      fputs("owned bytecode is not version1\n", stderr); lua_close(L); return 2;
    }
    out = fopen(dump_path, "wb");
    if (!out) { perror("owned bytecode open"); lua_close(L); return 2; }
    if (fwrite(bytes, 1, size, out) != size) {
      fclose(out); lua_close(L); return 2;
    }
    if (fclose(out)) { lua_close(L); return 2; }
    printf("owned_bytecode_version=1 size=%zu path=%s\n", size, dump_path);
    lua_settop(L, 1);
  }
  if (lua_pcall(L, 0, 2, 0)) goto lua_error;
  if (lua_tonumber(L, -2) != 42 || !lua_isstring(L, -1) ||
      strcmp(lua_tostring(L, -1), "owned-v1-reference")) {
    fputs("owned arithmetic mismatch\n", stderr); lua_close(L); return 2;
  }
  printf("NORMAL_PASS: create/openlibs/owned arithmetic42/close state=%p\n", (void *)L);
  lua_close(L);
  return 0;
lua_error:
  fprintf(stderr, "reference Lua error: %s\n", lua_tostring(L, -1));
  lua_close(L);
  return 2;
}

int main(int argc, char **argv)
{
  if (argc >= 2 && !strcmp(argv[1], "--normal"))
    return normal_probe(argc == 3 ? argv[2] : NULL);
  if (argc == 2 && !strcmp(argv[1], "--high-public")) return high_probe(0);
  if (argc == 2 && !strcmp(argv[1], "--high-core")) return high_probe(1);
  fputs("usage: probe-baseline --normal [owned-bytecode-output] | --high-public | --high-core\n", stderr);
  return 2;
}
