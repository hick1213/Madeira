#!/usr/bin/env python3
"""Compile the real pool retirement and EC clear with a bounded Darwin fixture.

--old-red invokes the destructive callback in the original unsafe caller order,
reproducing the captured live-worker retirement failure with current source.
The fixture owns its synthetic bitmap and pool metadata; no game memory is used.
"""
from pathlib import Path
import argparse
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--old-red', action='store_true')
    args = parser.parse_args()
    source = (ROOT / 'build/ntdll-unix/virtual_ios.c').read_text()
    server = (ROOT / 'build/ntdll-unix/server_ios.c').read_text()
    thread = (ROOT / 'build/ntdll-unix/thread_ios.c').read_text()
    process = (ROOT / 'build/ntdll-unix/process_ios.c').read_text()
    bridge = (ROOT / 'app/Madeira/WineProcessBridge.m').read_text()
    exit_wrapper = function(server, 'void process_exit_wrapper( int status )')
    for destructive in ['ios_jit_reclaim_quiescent', 'ios_wow_window_release',
                        'ios_subfloor_release_owner', 'ios_exe_win_mark_ready',
                        'ios_retire_own_fixed_base_image']:
        assert destructive not in exit_wrapper, f'logical exit calls {destructive}'
    assert 'ios_native_retire_process' in exit_wrapper
    creation = function(thread, 'NTSTATUS WINAPI NtCreateThreadEx(')
    assert creation.index('ios_native_thread_reserve') < creation.index('virtual_alloc_teb')
    assert creation.index('ios_native_thread_reserve') < creation.index('pthread_create')
    assert 'ios_native_creation_failed' in creation
    spawn = function(process, 'static NTSTATUS spawn_process(')
    assert spawn.index('ios_native_process_reserve(') < spawn.index('ret = pthread_create(')
    assert spawn.index('pthread_sigmask( SIG_BLOCK') < spawn.index('ios_native_process_reserve(')
    finish = function(bridge, 'static void wine_process_finish_session(void)')
    assert '__atomic_store_n(&g_wine_running, 0' not in finish
    assert 'ios_native_retire_current' in finish
    retire = function(source, 'static void ios_jit_reclaim_quiescent( void *peb )')
    retire = retire.replace('static void ios_jit_reclaim_quiescent(', 'void ios_jit_reclaim_process(', 1)
    clear = function(source, 'static void clear_arm64ec_range( const void *addr, size_t size )\n{')
    code = r'''
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
typedef uint64_t UINT64;
#define ROUND_SIZE(a,b,m) (((a)+(b)+(m))&~(m))
#define ROUND_ADDR(a,m) ((void *)((uintptr_t)(a)&~(m)))
#define VPROT_READ 1
#define VPROT_WRITE 2
#define VPROT_COMMITTED 4
#define IOS_POOL_FREE_MAX 4
#define IOS_POOL_REUSE_GRACE_SEC 3
static const size_t page_mask = 0x3fff;
static UINT64 bitmap[8];
static struct { void *base; } bitmap_view = {bitmap}, *arm64ec_view = &bitmap_view;
static UINT64 maskbits(size_t bit) { return ~(UINT64)0 << (bit & 63); }
static void set_vprot(void *a, void *b, size_t c, int d) { (void)a;(void)b;(void)c;(void)d; }
static void *ios_jit_rx_base_global = (void *)0x10000;
static void *ios_jit_alias_pushback_peb;
static void (*ios_jit_alias_pushback_cb)(unsigned long long,unsigned long long,unsigned long long);
static pthread_mutex_t ios_jit_alias_callback_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t ios_hold_release_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { void *peb, *cb; } ios_hold_release[4];
static int ios_hold_release_n;
static pthread_mutex_t ios_pool_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { void *peb; size_t off, size; } ios_pool_ledger[4];
static int ios_pool_ledger_count;
static struct { void *pe_base, *jit_base; size_t size; } ios_jit_mappings[4];
static int ios_jit_mapping_count;
static struct { uintptr_t jit_rx_alias, user_va_end, user_va; } ios_jit_anon_aliases[4];
static int ios_jit_anon_alias_count;
static struct { size_t off, size; time_t freed_at; int advised; } ios_pool_freelist[4];
static int ios_pool_free_count;
static size_t jit_pool_offset;
static void ios_mono_alias_retire(uintptr_t va) { (void)va; }
static int ios_pool_range_execable(size_t a,size_t b,unsigned *c,unsigned *d)
{ (void)a;(void)b;(void)c;(void)d;return 1; }
static int reclaims;
static void ios_exe_win_note_dead_peb(void *peb) { (void)peb; reclaims++; }
static void clear_arm64ec_range(const void *,size_t);
'''
    code += clear + retire
    fixture = code + r'''
void fixture_reset(void) {
    bitmap[0] = (UINT64)1 << 16;
    ios_pool_ledger[0].peb = (void *)1;
    ios_pool_ledger[0].size = 0x1000;
    ios_pool_ledger_count = 1;
    ios_jit_mappings[0].pe_base = (void *)0x20000;
    ios_jit_mappings[0].jit_base = ios_jit_rx_base_global;
    ios_jit_mappings[0].size = 0x1000;
    ios_jit_mapping_count = 1;
    ios_pool_free_count = 0;
    reclaims = 0;
}
int fixture_valid(void) {
    return !!(bitmap[0] & ((UINT64)1 << 16)) && ios_jit_mappings[0].pe_base && !ios_pool_free_count;
}
void fixture_reclaim(void *peb) { ios_jit_reclaim_process(peb); }
'''
    old_red = code + r'''
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static int ready, release_worker, valid;
static void *worker(void *arg) {
    (void)arg;
    pthread_mutex_lock(&lock);
    ready = 1;
    pthread_cond_broadcast(&cond);
    while (!release_worker) pthread_cond_wait(&cond, &lock);
    /* The ARM64EC call classifier reads the actual bit cleared by production.
     * Do not jump to the MZ fallback; report that routing result instead. */
    valid = !!(bitmap[0] & ((UINT64)1 << 16));
    pthread_mutex_unlock(&lock);
    return NULL;
}
int main(void) {
    pthread_t thread;
    bitmap[0] = (UINT64)1 << 16;
    ios_pool_ledger[0].peb = (void *)1;
    ios_pool_ledger[0].size = 0x1000;
    ios_pool_ledger_count = 1;
    ios_jit_mappings[0].pe_base = (void *)0x20000;
    ios_jit_mappings[0].jit_base = ios_jit_rx_base_global;
    ios_jit_mappings[0].size = 0x1000;
    ios_jit_mapping_count = 1;
    if (pthread_create(&thread, NULL, worker, NULL)) return 2;
    pthread_mutex_lock(&lock);
    while (!ready) pthread_cond_wait(&cond, &lock);
    ios_jit_reclaim_process((void *)1);
    release_worker = 1;
    pthread_cond_broadcast(&cond);
    pthread_mutex_unlock(&lock);
    pthread_join(thread, NULL);
    if (!valid && reclaims == 1 && ios_pool_free_count == 1 && !ios_jit_mappings[0].pe_base) {
        fputs("RED: native worker still alive when EC classification, mapping and pool ownership were retired\n", stderr);
        return 1;
    }
    return valid ? 0 : 2;
}
'''
    with tempfile.TemporaryDirectory(prefix='madeira-native-retirement-') as temp:
        folder = Path(temp)
        args_start = process.index('struct ios_child_args {')
        args_end = process.index('\n};', args_start) + 3
        (folder / 'native-child-args.inc').write_text(process[args_start:args_end])
        wrappers = function(process, 'static void ios_child_native_cleanup( void *arg )')
        wrappers += function(process, 'static void *ios_child_thread_entry( void *arg )')
        wrappers += function(bridge, 'static void wine_process_finish_session(void)')
        wrappers += function(bridge, 'static void wine_process_native_cleanup(void *unused)')
        wrappers += function(bridge, 'static void *wine_process_thread(void *arg)')
        (folder / 'native-bootstrap-wrappers.inc').write_text(wrappers)
        harness = folder / 'check.c'
        executable = folder / 'check'
        harness.write_text(old_red if args.old_red else fixture)
        common = ['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror', str(harness)]
        if not args.old_red:
            common += [str(ROOT / 'build/ntdll-unix/ios_native_lifetime.c'),
                       str(ROOT / 'tests/host/native-retirement.c')]
        subprocess.run(common + ['-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                 '-g', '-o', str(executable)], check=True, timeout=60)
        result = subprocess.run([str(executable)], check=False, timeout=50,
                                capture_output=args.old_red, text=True)
        if args.old_red:
            print(result.stdout, end='')
            print(result.stderr, end='')
            marker = 'RED: native worker still alive when EC classification, mapping and pool ownership were retired'
            if result.returncode != 1 or marker not in result.stderr.splitlines():
                raise SystemExit('old-source RED was not reproduced')
        elif result.returncode:
            raise SystemExit(result.returncode)
        else:
            subprocess.run(common + ['-O2', '-o', str(executable)], check=True, timeout=60)
            subprocess.run([str(executable)], check=True, timeout=50)
            for flags in [['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-g'], ['-O2']]:
                subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                str(ROOT / 'tests/host/native-retirement-faults.c'),
                                *flags, '-o', str(executable)], check=True, timeout=60)
                subprocess.run([str(executable)], check=True, timeout=25)
                subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                str(ROOT / 'tests/host/native-retirement-races.c'),
                                *flags, '-o', str(executable)], check=True, timeout=60)
                subprocess.run([str(executable), 'startup'], check=True, timeout=25)
                subprocess.run([str(executable), 'signal'], check=True, timeout=25)
                subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                '-I', str(folder), str(ROOT / 'tests/host/native-bootstrap-cleanup.c'),
                                str(ROOT / 'build/ntdll-unix/ios_native_lifetime.c'),
                                *flags, '-o', str(executable)], check=True, timeout=60)
                subprocess.run([str(executable)], check=True, timeout=30)


if __name__ == '__main__':
    main()
