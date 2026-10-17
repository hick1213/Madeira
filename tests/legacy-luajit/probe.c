/* Owned public-API probe; no game script is ever executed by --load-manifest. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"
#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#endif

static int expect_high;
static int native_pointer;

static void check_pointer(const void *p, const char *name)
{
  if (!p || (expect_high && (uintptr_t)p <= UINT32_MAX)) {
    fprintf(stderr, "POINTER_FAIL %s=%p\n", name, p);
    abort();
  }
}

static int copy_callback(lua_State *L)
{
  size_t size;
  const char *source=luaL_checklstring(L,1,&size);
  char *dest;
  check_pointer(L,"callback-state");
  check_pointer(source,"callback-string");
  dest=(char *)lua_newuserdata(L,size+2);
  check_pointer(dest,"userdata");
  memcpy(dest,source,size);
  memmove(dest+1,dest,size);
  if (memcmp(dest+1,source,size)) return luaL_error(L,"native copy mismatch");
  lua_pushlstring(L,dest+1,size);
  return 1;
}

static int reenter_callback(lua_State *L)
{
  check_pointer(L,"reenter-state");
  luaL_checktype(L,1,LUA_TFUNCTION);
  lua_pushvalue(L,1); lua_pushvalue(L,2);
  if (lua_pcall(L,1,1,0)) return lua_error(L);
  return 1;
}

static int fail_callback(lua_State *L)
{
  check_pointer(L,"error-state");
  (void)luaL_checkstring(L,1);
  return luaL_error(L,"owned-native-error");
}

static int pointer_callback(lua_State *L)
{
  lua_pushlightuserdata(L,&native_pointer);
  return 1;
}

static lua_State *new_state(void)
{
  lua_State *L=luaL_newstate();
  if (!L) { fputs("STATE_FAIL\n",stderr); return NULL; }
  check_pointer(L,"state");
  luaL_openlibs(L);
  lua_register(L,"c_copy",copy_callback);
  lua_register(L,"c_reenter",reenter_callback);
  lua_register(L,"c_fail",fail_callback);
  lua_register(L,"c_pointer",pointer_callback);
  return L;
}

static int lua_failure(lua_State *L)
{
  const char *message=lua_tostring(L,-1);
  fprintf(stderr,"LUA_FAIL %s\n",message ? message : "non-string error");
  lua_close(L);
  return 1;
}

static int corpus(const char *path)
{
  lua_State *L=new_state();
  int checks;
  if (!L) return 1;
  if (luaL_loadfile(L,path) || lua_pcall(L,0,1,0)) return lua_failure(L);
  checks=(int)lua_tointeger(L,-1);
  if (checks != 61) { fputs("CORPUS_COUNT_FAIL\n",stderr); lua_close(L); return 1; }
  printf("API_PASS checks=%d state=%p high_required=%d\n",checks,(void *)L,expect_high);
  lua_close(L);
  return 0;
}

static int dump_writer(lua_State *L, const void *data, size_t size, void *ud)
{
  (void)L;
  return fwrite(data,1,size,(FILE *)ud) == size ? 0 : 1;
}

static int dump_owned(const char *input, const char *output)
{
  lua_State *L=new_state();
  FILE *out;
  int failed;
  if (!L) return 1;
  if (luaL_loadfile(L,input)) return lua_failure(L);
  out=fopen(output,"wb");
  if (!out) { perror("dump output"); lua_close(L); return 1; }
  failed=lua_dump(L,dump_writer,out);
  if (fclose(out)) failed=1;
  lua_close(L);
  if (failed) return 1;
  puts("OWNED_DUMP_PASS (loaded, not executed)");
  return 0;
}

static int load_manifest(const char *manifest)
{
  lua_State *L=new_state();
  FILE *in;
  char path[8192];
  unsigned count=0;
  if (!L) return 1;
  in=fopen(manifest,"r");
  if (!in) { perror("manifest"); lua_close(L); return 1; }
  while (fgets(path,sizeof(path),in)) {
    size_t length=strlen(path);
    if (!length || path[length-1]!='\n') {
      fputs("invalid or overlong manifest line\n",stderr);
      fclose(in); lua_close(L); return 1;
    }
    path[--length]=0;
    if (length && path[length-1]=='\r') path[--length]=0;
    if (!length) { fclose(in); lua_close(L); return 1; }
    /* luaL_loadfile parses only: deliberately no lua_call/lua_pcall here. */
    if (luaL_loadfile(L,path)) { fclose(in); return lua_failure(L); }
    lua_settop(L,0); count++;
    if (!(count%25)) lua_gc(L,LUA_GCCOLLECT,0);
  }
  if (ferror(in) || fclose(in)) { lua_close(L); return 1; }
  lua_close(L);
  printf("LOAD_ONLY_PASS count=%u\n",count);
  return count ? 0 : 1;
}

typedef struct { unsigned index; int failed; } Worker;

static void stress_worker(Worker *worker)
{
  unsigned round;
  for (round=0;round<12;round++) {
    lua_State *L=new_state();
    if (!L) { worker->failed=1; return; }
    if (luaL_dostring(L,"local t={}; for i=1,2000 do t[i]=string.rep(tostring(i),32) end "
                        "collectgarbage(); assert(t[2000]==string.rep('2000',32)); "
                        "local c=coroutine.create(function() coroutine.yield(19); return 23 end); "
                        "local ok,v=coroutine.resume(c); assert(ok and v==19); "
                        "ok,v=coroutine.resume(c); assert(ok and v==23)")) {
      worker->failed=lua_failure(L); return;
    }
    lua_close(L);
  }
}

#ifdef _WIN32
static DWORD WINAPI thread_entry(void *ud) { stress_worker((Worker *)ud); return 0; }
#else
static void *thread_entry(void *ud) { stress_worker((Worker *)ud); return NULL; }
#endif

static int stress(void)
{
  Worker workers[4]={{0,0},{1,0},{2,0},{3,0}};
  unsigned i,started=0;
  int failed=0;
#ifdef _WIN32
  HANDLE threads[4];
#else
  pthread_t threads[4];
#endif
  for (i=0;i<4;i++) {
#ifdef _WIN32
    threads[i]=CreateThread(NULL,0,thread_entry,&workers[i],0,NULL);
    if (!threads[i]) break;
#else
    if (pthread_create(&threads[i],NULL,thread_entry,&workers[i])) break;
#endif
    started++;
  }
  for (i=0;i<started;i++) {
#ifdef _WIN32
    if (WaitForSingleObject(threads[i],INFINITE)!=WAIT_OBJECT_0) failed=1;
    if (!CloseHandle(threads[i])) failed=1;
#else
    if (pthread_join(threads[i],NULL)) failed=1;
#endif
    if (workers[i].failed) failed=1;
  }
  if (started!=4 || failed) return 1;
  puts("STRESS_PASS threads=4 states=48");
  return 0;
}

int main(int argc, char **argv)
{
  int first=1;
  setvbuf(stdout,NULL,_IONBF,0);
  if (argc>1 && !strcmp(argv[1],"--expect-high")) { expect_high=1; first++; }
  if (argc-first==2 && !strcmp(argv[first],"--corpus")) return corpus(argv[first+1]);
  if (argc-first==3 && !strcmp(argv[first],"--dump")) return dump_owned(argv[first+1],argv[first+2]);
  if (argc-first==2 && !strcmp(argv[first],"--load-manifest")) return load_manifest(argv[first+1]);
  if (argc-first==1 && !strcmp(argv[first],"--stress")) return stress();
  fputs("usage: probe [--expect-high] --corpus file | --dump input output | --load-manifest file | --stress\n",stderr);
  return 2;
}
