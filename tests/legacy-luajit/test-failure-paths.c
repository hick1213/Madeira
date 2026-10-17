#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"
#include "lj_obj.h"
#include "lj_alloc.h"
#include "lj_state.h"
#include "lj_compat_arena.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define UNIT ((size_t)65536)
#define CAP ((size_t)0x80000000)
#define BAD ((void *)(uintptr_t)-1)
typedef struct Budget {
  void *heap;
  long left;
  size_t outstanding;
  unsigned rejected;
} Budget;

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

static int request_large(lua_State *L)
{
  lua_newuserdata(L, UNIT * 8);
  return 1;
}

static int call_native_callback(lua_State *L)
{
  int (*fn)(int) = (int (*)(int))(uintptr_t)luaL_checknumber(L, 1);
  int n = (int)luaL_checkinteger(L, 2);
  assert(fn && !lj_compat_contains((const void *)fn, 1));
  lua_pushinteger(L, fn(n));
  return 1;
}

static void check_lua(lua_State *L, const char *s)
{
  if (luaL_dostring(L, s)) {
    fprintf(stderr, "Lua fixture failed: %s\n", lua_tostring(L, -1));
    abort();
  }
  lua_settop(L, 0);
}

static void partial_states(void)
{
  long i;
  unsigned failures = 0, successes = 0;
  for (i = 0; i < 96; i++) {
    Budget b;
    lua_State *L;
    fprintf(stderr, "partial-state budget=%ld\n", i);
    b.heap = lj_alloc_create(); b.left = i; b.outstanding = 0; b.rejected = 0;
    assert(b.heap);
    L = lj_state_newstate(budget_alloc, &b);
    if (L) { successes++; lua_close(L); }
    else { failures++; assert(b.rejected); }
    assert(!b.outstanding);
    lj_alloc_destroy(b.heap);
  }
  assert(failures && successes);
  printf("PASS partial initialization: %u failpoints and %u successful states; every tracked allocation freed\n", failures, successes);
}

static void runtime_failure(void)
{
  lua_State *L = luaL_newstate();
  lua_Alloc original;
  void *ud;
  Budget b;
  MSize before;
  int status;
  assert(L);
  luaL_openlibs(L);
  check_lua(L, "marker={42}; co=coroutine.create(function() coroutine.yield(9); return marker[1] end); assert(select(2,coroutine.resume(co))==9)");
  lua_pushcfunction(L, request_large);
  original = lua_getallocf(L, &ud);
  b.heap = ud; b.left = 0; b.outstanding = G(L)->gc.total; b.rejected = 0;
  before = G(L)->gc.total;
  lua_setallocf(L, budget_alloc, &b);
  status = lua_pcall(L, 0, 1, 0);
  assert(status == LUA_ERRMEM && b.rejected);
  assert(G(L)->gc.total == before && b.outstanding == before);
  lua_setallocf(L, original, ud);
  lua_settop(L, 0);
  check_lua(L, "assert(marker[1]==42); assert(select(2,coroutine.resume(co))==42)");
  lua_pushcfunction(L, request_large);
  assert(lua_pcall(L, 0, 1, 0) == 0 && lua_type(L, -1) == LUA_TUSERDATA);
  assert(lj_compat_contains(lua_touserdata(L, -1), UNIT * 8));
  lua_close(L);
  puts("PASS protected runtime OOM: original coroutine/references/accounting preserved; retry succeeds");
}

static void callback_other_state_lifetime(void)
{
  lua_State *L = luaL_newstate();
  unsigned i;
  uintptr_t base;
  assert(L);
  luaL_openlibs(L);
  lua_register(L, "call_native_callback", call_native_callback);
  check_lua(L, "ffi=require('ffi'); cb=ffi.cast('int(*)(int)',function(n) return n+100 end); cbaddr=tonumber(ffi.cast('intptr_t',cb)); assert(call_native_callback(cbaddr,23)==123)");
  base = lj_compat_arena_base;
  for (i = 0; i < 80; i++) {
    lua_State *other = luaL_newstate();
    assert(other && lj_compat_arena_base == base);
    luaL_openlibs(other);
    check_lua(other, "local a={}; for i=1,200 do a[i]=string.rep('z',i) end; collectgarbage('collect')");
    lua_close(other);
    check_lua(L, "assert(call_native_callback(cbaddr,23)==123); collectgarbage('collect'); assert(call_native_callback(cbaddr,27)==127)");
  }
  lua_close(L);
  assert(lj_compat_arena_base == base);
  puts("PASS native FFI callback retained across 80 unrelated state closes/reuse and owner GC");
}

static void exhaust_reservation(void)
{
  lua_State *L = luaL_newstate();
  void *large, *small[128];
  unsigned n = 0, i;
  uintptr_t base;
  MSize before;
  assert(L);
  luaL_openlibs(L);
  check_lua(L, "retained={a=79}; c=coroutine.create(function() coroutine.yield(37); return retained.a end); assert(select(2,coroutine.resume(c))==37)");
  lua_pushcfunction(L, request_large);
  base = lj_compat_arena_base;
  large = lj_compat_arena_map(CAP - 64 * UNIT);
  assert(large != BAD);
  while (n < 128) {
    void *p = lj_compat_arena_map(UNIT);
    if (p == BAD) break;
    small[n++] = p;
  }
  assert(n < 128);
  for (i = 0; i < 200; i++) assert(luaL_newstate() == NULL);
  before = G(L)->gc.total;
  assert(lua_pcall(L, 0, 1, 0) == LUA_ERRMEM);
  assert(G(L)->gc.total == before);
  assert(lj_compat_arena_unmap(large, CAP - 64 * UNIT) == 0);
  for (i = 0; i < n; i++) assert(lj_compat_arena_unmap(small[i], UNIT) == 0);
  lua_settop(L, 0);
  check_lua(L, "assert(retained.a==79); assert(select(2,coroutine.resume(c))==79)");
  lua_pushcfunction(L, request_large);
  assert(lua_pcall(L, 0, 1, 0) == 0);
  lua_close(L);
  large = lj_compat_arena_map(CAP - 2 * UNIT);
  assert(large == (void *)(base + UNIT));
  assert(lj_compat_arena_unmap(large, CAP - 2 * UNIT) == 0);
  printf("PASS arena exhaustion: 200 refused states, protected OOM, live-state retry, complete capacity reusable; fillers=%u\n", n);
}

int main(void)
{
  setvbuf(stdout, NULL, _IONBF, 0);
  partial_states();
  runtime_failure();
  callback_other_state_lifetime();
  exhaust_reservation();
  puts("PASS independent runtime security failure/lifetime fixtures");
  return 0;
}
