#!/usr/bin/env python3
"""Compile production per-process keyed-event helpers and wrappers with server mocks."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
server = (ROOT / 'build/ntdll-unix/server_ios.c').read_text()
sync = (ROOT / 'build/ntdll-unix/sync_ios.c').read_text()
state = server.split('/* IOS_DEFAULT_KEYED_STATE_BEGIN */')[1].split('/* IOS_DEFAULT_KEYED_STATE_END */')[0]
helpers = server.split('/* IOS_DEFAULT_KEYED_HELPERS_BEGIN */')[1].split('/* IOS_DEFAULT_KEYED_HELPERS_END */')[0]
wrappers = sync.split('/* IOS_DEFAULT_KEYED_WRAPPERS_BEGIN */')[1]
loader = (ROOT / 'build/ntdll-unix/loader_ios.c').read_text()
build = (ROOT / 'build/ntdll-unix/build.sh').read_text()
assert '#include "../../wine/dlls/ntdll/unix/sync.c"' in sync
assert 'compile_one "$BUILD_DIR/sync_ios.c" "sync"' in build
child = loader[loader.index('startup_info_size = server_init_process_child( child_fd_socket );'):]
assert child.index('ios_init_child_keyed_event();') < child.index('unix_init_startup_info')
assert 'process_exit_wrapper( status );' in child[:child.index('unix_init_startup_info')]
exit_path = server[server.index('void process_exit_wrapper( int status )'):]
assert exit_path.index('ios_release_child_keyed_event( i );') < exit_path.index('close( ios_proc_sockets[i].fd );')
assert 'NtClose(' not in helpers

code = r'''
#include <assert.h>
#include <stdint.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#define WINAPI
#define STATUS_SUCCESS 0
#define STATUS_INVALID_HANDLE 0xc0000008u
#define STATUS_NO_MEMORY 0xc0000017u
#define GENERIC_READ 1
#define GENERIC_WRITE 2
#define FALSE 0
#define TRUE 1
typedef void *HANDLE;
typedef uint32_t DWORD, NTSTATUS;
typedef int BOOL;
typedef unsigned char BOOLEAN;
typedef struct { long long QuadPart; } LARGE_INTEGER;
''' + state + r'''
struct { void *peb; int fd; BOOL exiting; struct ios_keyed_event_state keyed; } ios_proc_sockets[2];
static int ios_proc_socket_count=2;
static void *cur_peb=(void*)0x1000;
static DWORD cur_pid=4;
static int create_count, raw_calls;
static NTSTATUS create_status, raw_status;
static HANDLE forwarded;
static void *ios_jit_current_peb(void) { return cur_peb; }
typedef uintptr_t ULONG_PTR;
typedef struct { struct { HANDLE UniqueProcess; } ClientId; } TEB;
static TEB *NtCurrentTeb(void) { static _Thread_local TEB teb;teb.ClientId.UniqueProcess=(HANDLE)(uintptr_t)cur_pid;return cur_pid?&teb:NULL; }
static int ios_proc_socket_index(void) {
 for(int i=0;i<ios_proc_socket_count;i++) if(ios_proc_sockets[i].peb==cur_peb) return i;
 return -1;
}
static NTSTATUS NtCreateKeyedEvent(HANDLE *out, DWORD access, const void *attr, DWORD flags) {
 assert(access==(GENERIC_READ|GENERIC_WRITE));assert(!attr&&!flags);
 create_count++;*out=create_status?NULL:(void*)(uintptr_t)(0x100+create_count);return create_status;
}
static sigset_t server_block_set;
static _Thread_local volatile sig_atomic_t quit_seen, quit_under_lock, lock_held;
static void quit_probe(int signal) {
 (void)signal;
 if (lock_held) quit_under_lock++;
 quit_seen++;
}
static int lock_with_pending_quit(pthread_mutex_t *lock) {
 int result=pthread_mutex_lock(lock);
 if (!result) { lock_held=1; assert(!raise(SIGQUIT)); }
 return result;
}
static int unlock_with_probe(pthread_mutex_t *lock) {
 int result=pthread_mutex_unlock(lock);
 if (!result) lock_held=0;
 return result;
}
#define pthread_mutex_lock lock_with_pending_quit
#define pthread_mutex_unlock unlock_with_probe
''' + helpers + r'''
#undef pthread_mutex_lock
#undef pthread_mutex_unlock
static NTSTATUS ios_upstream_NtWaitForKeyedEvent(HANDLE h,const void *key,BOOLEAN alert,const LARGE_INTEGER *timeout) {
 assert(key==(void*)0x2000 && alert==1 && timeout->QuadPart==42);forwarded=h;raw_calls++;return raw_status;
}
static NTSTATUS ios_upstream_NtReleaseKeyedEvent(HANDLE h,const void *key,BOOLEAN alert,const LARGE_INTEGER *timeout) {
 return ios_upstream_NtWaitForKeyedEvent(h,key,alert,timeout);
}
''' + wrappers + r'''
static void *init_worker(void *unused) { (void)unused; assert(ios_init_child_keyed_event()==STATUS_SUCCESS); assert(quit_seen==1 && !quit_under_lock); return NULL; }
int main(void) {
 HANDLE h=NULL; LARGE_INTEGER timeout={42};
 struct sigaction action={0};action.sa_handler=quit_probe;sigemptyset(&action.sa_mask);
 assert(!sigaction(SIGQUIT,&action,NULL));
 sigemptyset(&server_block_set);sigaddset(&server_block_set,SIGQUIT);
 assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 assert(quit_seen==1 && quit_under_lock==0);
 ios_register_parent_keyed_event((void*)0x44,STATUS_SUCCESS);
 assert(ios_default_keyed_event(&h)==STATUS_SUCCESS && h==(void*)0x44);
 raw_status=0x102; assert(NtWaitForKeyedEvent(NULL,(void*)0x2000,1,&timeout)==raw_status && forwarded==(void*)0x44);
 cur_peb=(void*)0x3000;cur_pid=12;ios_proc_sockets[0].peb=cur_peb;
 assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 sigset_t prior,pending;
 assert(!pthread_sigmask(SIG_BLOCK,&server_block_set,&prior));
 sig_atomic_t seen=quit_seen;
 assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 assert(quit_seen==seen);assert(!sigpending(&pending));assert(sigismember(&pending,SIGQUIT));
 assert(!pthread_sigmask(SIG_SETMASK,&prior,NULL));assert(quit_seen==seen+1 && !quit_under_lock);
 assert(ios_init_child_keyed_event()==STATUS_SUCCESS && create_count==1);
 assert(ios_init_child_keyed_event()==STATUS_SUCCESS && create_count==1);
 assert(NtReleaseKeyedEvent(NULL,(void*)0x2000,1,&timeout)==raw_status && forwarded==(void*)0x101);
 assert(NtWaitForKeyedEvent((void*)0x77,(void*)0x2000,1,&timeout)==raw_status && forwarded==(void*)0x77);
 cur_pid=13;h=(void*)0x777;assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE && h==NULL);
 assert(ios_init_child_keyed_event()==STATUS_INVALID_HANDLE && create_count==1);
 cur_pid=0;assert(ios_init_child_keyed_event()==STATUS_INVALID_HANDLE && create_count==1);
 cur_pid=12;ios_release_child_keyed_event(0);assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 cur_pid=16;
 pthread_t workers[8]; for(int i=0;i<8;i++) assert(!pthread_create(&workers[i],NULL,init_worker,NULL));
 for(int i=0;i<8;i++) assert(!pthread_join(workers[i],NULL)); assert(create_count==2);
 assert(ios_default_keyed_event(&h)==STATUS_SUCCESS && h==(void*)0x102);
 cur_peb=(void*)0x4000;cur_pid=20;ios_proc_sockets[1].peb=cur_peb;create_status=STATUS_NO_MEMORY;
 assert(ios_init_child_keyed_event()==STATUS_NO_MEMORY);
 assert(ios_init_child_keyed_event()==STATUS_NO_MEMORY && create_count==3);
 int calls=raw_calls;assert(NtWaitForKeyedEvent(NULL,(void*)0x2000,1,&timeout)==STATUS_NO_MEMORY && raw_calls==calls);
 cur_peb=(void*)0x5000;cur_pid=24;assert(ios_init_child_keyed_event()==STATUS_INVALID_HANDLE && create_count==3);
 assert(NtReleaseKeyedEvent(NULL,(void*)0x2000,1,&timeout)==STATUS_INVALID_HANDLE && raw_calls==calls);
 assert(NtReleaseKeyedEvent((void*)0x99,(void*)0x2000,1,&timeout)==raw_status && forwarded==(void*)0x99);
 cur_peb=(void*)0x1000;cur_pid=4;assert(ios_default_keyed_event(&h)==STATUS_SUCCESS && h==(void*)0x44);
 cur_pid=8;assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 cur_pid=4;assert(ios_register_parent_keyed_event(NULL,STATUS_SUCCESS)==STATUS_INVALID_HANDLE);
 assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 assert(ios_register_parent_keyed_event(NULL,STATUS_NO_MEMORY)==STATUS_NO_MEMORY);
 h=(void*)0x888;assert(ios_default_keyed_event(&h)==STATUS_NO_MEMORY && h==NULL);
 cur_pid=0;assert(ios_register_parent_keyed_event((void*)0x44,STATUS_SUCCESS)==STATUS_INVALID_HANDLE);
 h=(void*)0x777;assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE && !h);
 cur_pid=4;cur_peb=NULL;assert(ios_register_parent_keyed_event((void*)0x44,STATUS_SUCCESS)==STATUS_INVALID_HANDLE);
 assert(ios_default_keyed_event(&h)==STATUS_INVALID_HANDLE);
 assert(quit_seen>20 && quit_under_lock==0);
 puts("PASS: default keyed-event parent/child ownership, explicit handles, create-once, failure, cleanup and PID generations");
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-keyed-event-') as td:
    source = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    source.write_text(code)
    for flags in [['-fsanitize=address,undefined', '-fno-sanitize-recover=all'], ['-O2']]:
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', *flags,
                        str(source), '-o', str(binary)], check=True, timeout=60)
        subprocess.run([str(binary)], check=True, timeout=30)
