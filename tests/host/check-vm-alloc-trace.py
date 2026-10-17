#!/usr/bin/env python3
"""Compile actual bounded allocation trace with hostile Mach-read boundary fixtures."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'build/ntdll-unix/virtual_ios.c').read_text()
assert '/* vm-alloc trace begin */' in source, 'allocation trace is missing'
core = source.split('/* vm-alloc trace begin */', 1)[1].split('/* vm-alloc trace end */', 1)[0]
prelude = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef size_t SIZE_T;
typedef unsigned int ULONG;
typedef unsigned int NTSTATUS;
typedef uint64_t mach_vm_address_t;
typedef uint64_t mach_vm_size_t;
typedef uintptr_t ULONG_PTR;
typedef struct { struct { void *UniqueThread; } ClientId; } TEB;
struct file_view { void *base; size_t size; };
#define KERN_SUCCESS 0
#define MEM_COMMIT 0x1000
#define MEM_DECOMMIT 0x4000
static TEB teb;
static unsigned long reads, native_reads, syscall_reads, active_reads;
static int mode;
static TEB *NtCurrentTeb(void) { return mode == 9 ? NULL : &teb; }
int ios_is_arena_addr(const void *base) { return base == (void *)0x600000; }
#define __builtin_frame_address(depth) ((void *)(uintptr_t)(mode == 21 ? UINTPTR_MAX - 7 : mode == 22 ? 0x300001 : mode == 23 ? 1 : 0x300000))
uint64_t ios_jit_reverse_translate(uint64_t lr, uint64_t *module)
{
    *module = lr == 0x700000 ? 0x7300000000 : 0;
    return *module ? *module + 0x1234 : 0;
}
static int mach_task_self(void) { return 1; }
static int mach_vm_read_overwrite(int task, mach_vm_address_t addr, mach_vm_size_t size,
                                  mach_vm_address_t dst, mach_vm_size_t *got)
{
    int stage;
    (void)task;
    if (addr == (uintptr_t)&teb + 0x378 && size == 8)
    {
        syscall_reads++;
        *(uint64_t *)(uintptr_t)dst = mode == 28 ? 1 : mode == 29 ? UINTPTR_MAX - 7 : 0x900000;
        *got = mode == 25 ? 7 : 8; errno = EIO;
        return mode == 24 ? 1 : KERN_SUCCESS;
    }
    if (addr == 0x9000e8 && size == 32)
    {
        uint64_t *frame = (uint64_t *)(uintptr_t)dst;
        syscall_reads++; frame[0] = 0xa00000; frame[1] = 0x700000;
        frame[2] = 0xa00080; frame[3] = 0x700000;
        *got = mode == 27 ? 31 : 32; errno = EIO;
        return mode == 26 ? 1 : KERN_SUCCESS;
    }
    if (size == 16 && addr >= 0xa00000 && addr <= 0xa00020 && !(addr & 15))
    {
        uint64_t *frame = (uint64_t *)(uintptr_t)dst;
        active_reads++; frame[0] = addr == 0xa00020 ? 0 : addr + 16;
        frame[1] = 0x700000; *got = 16; errno = EIO; return KERN_SUCCESS;
    }
    if (size == 16 && addr >= 0x300000 && addr <= 0x300100 && !(addr & 15))
    {
        uint64_t *frame = (uint64_t *)(uintptr_t)dst;
        native_reads++;
        frame[0] = mode == 14 ? addr : mode == 15 ? addr - 16 : mode == 16 ? 0x400010 : mode == 19 ? UINTPTR_MAX - 7 : mode == 20 ? addr + 16 : addr == 0x300020 ? 0 : addr + 16;
        frame[1] = addr == 0x300000 ? 0x700000 : 0x800000;
        *got = mode == 17 ? 15 : 16;
        errno = EIO;
        return mode == 18 ? 1 : KERN_SUCCESS;
    }
    reads++;
    errno = EIO;
    stage = addr == (uintptr_t)&teb + 0x1788 ? 1 : addr == 0x100000 + 0x50 + 0xf8 ? 2 : addr == 0x100000 + 0x50 + 0x98 ? 3 : 4;
    if (addr == (uintptr_t)&teb + 0x1788 && size == 8) *(uint64_t *)(uintptr_t)dst = mode == 10 ? 1 : mode == 11 ? UINTPTR_MAX : 0x100000;
    else if (addr == 0x100000 + 0x50 + 0xf8 && size == 8) *(uint64_t *)(uintptr_t)dst = 0x7301234567;
    else if (addr == 0x100000 + 0x50 + 0x98 && size == 8) *(uint64_t *)(uintptr_t)dst = mode == 12 ? 0x200001 : mode == 13 ? UINT64_MAX - 7 : 0x200000;
    else if (addr == 0x200000 && size == 128) memset((void *)(uintptr_t)dst, 0x42, size);
    else { fprintf(stderr, "FAIL unexpected/target read\n"); exit(1); }
    *got = mode == stage + 4 ? size - 1 : size;
    return mode == stage ? 1 : KERN_SUCCESS;
}
'''
driver = r'''
int main(int argc, char **argv)
{
    struct file_view view = { (void *)0x400000, 0x100000000 };
    mode = argc > 1 ? atoi(argv[1]) : 0;
    teb.ClientId.UniqueThread = (void *)42;
    errno = EBUSY;
    for (int i = 0; i < 10000; i++)
    {
        ios_vm_alloc_trace(0, (void *)0x600000, 0x4000, MEM_COMMIT, 4, &view, 0, 0, 0);
        ios_vm_alloc_trace(1, (void *)0x600000, 0x100000, MEM_DECOMMIT, 0, &view, -1, -1, 0);
        ios_vm_alloc_trace(0, (void *)0x500000, 0x8000, MEM_COMMIT, 4, &view, 0, 0, 0);
        ios_vm_alloc_trace(0, (void *)0x500000, 0x4000, MEM_COMMIT, 4, &view, 1, 0, 0);
        ios_vm_alloc_trace(0, (void *)0x500000, 0x4000, MEM_COMMIT, 4, &view, 0, 1, 0);
        ios_vm_alloc_trace(1, (void *)0x500000, 0x4000, MEM_DECOMMIT, 0, &view, -1, -1, 0);
    }
    if (reads || native_reads || syscall_reads || active_reads) { fprintf(stderr, "FAIL excluded reads\n"); return 1; }
    for (int i = 0; i < 10000; i++) ios_vm_alloc_trace(0, (void *)0x500000, 0x4000, MEM_COMMIT, 4, &view, 0, 0, 0);
    for (int i = 0; i < 10000; i++) ios_vm_alloc_trace(1, (void *)0x500000, 0x100000, MEM_DECOMMIT, 0, &view, -1, -1, 0);
    if (errno != EBUSY) { fprintf(stderr, "FAIL errno changed\n"); return 1; }
    printf("reads=%lu native=%lu syscall=%lu active=%lu\n", reads, native_reads, syscall_reads, active_reads);
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-alloc-trace-', dir='/private/tmp') as tmp:
    c, exe = Path(tmp) / 'trace.c', Path(tmp) / 'trace'
    c.write_text(prelude + core + driver)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', str(c), '-o', str(exe)], check=True, timeout=60)
    for value in (None, '0', '', 'yes', '1junk', '01', ' 1', '1'):
        for mode in range(30):
            env = dict(os.environ)
            env.pop('MADEIRA_VM_ALLOC_TRACE', None)
            if value is not None:
                env['MADEIRA_VM_ALLOC_TRACE'] = value
            run = subprocess.run([str(exe), str(mode)], env=env, capture_output=True, text=True, timeout=15)
            assert run.returncode == 0, (value, mode, run.stdout, run.stderr)
            enabled = value == '1'
            reads_per_sample = 4 if mode == 0 or mode >= 14 else ((mode - 1) % 4 + 1) if mode < 9 else {9: 0, 10: 1, 11: 1, 12: 3, 13: 3}[mode]
            expected = 12 * reads_per_sample if enabled else 0
            native_per_sample = 3 if mode < 14 or mode >= 24 else 16 if mode == 20 else 0 if mode >= 21 else 1
            syscall_per_sample = 0 if mode == 9 else 1 if mode in (24, 25, 28, 29) else 2
            active_per_sample = 0 if mode == 9 or mode >= 24 else 3
            assert run.stdout == f'reads={expected} native={12 * native_per_sample if enabled else 0} syscall={12 * syscall_per_sample if enabled else 0} active={12 * active_per_sample if enabled else 0}\n', (value, mode, run.stdout)
            records = [line for line in run.stderr.splitlines() if line.startswith('[vm-alloc-trace]')]
            assert len(records) == (12 if enabled else 0), (value, mode, records)
            if enabled:
                for kind in ('commit', 'decommit'):
                    lines = [line for line in records if f'kind={kind} ' in line]
                    assert [int(line.split(' seq=')[1].split()[0]) for line in lines] == [1, 2, 3, 4, 4096, 8192]
                assert all(('tid=0 ' if mode == 9 else 'tid=42 ') in line and 'stack=raw-candidates-not-callstack' in line for line in records)
                assert run.stderr.count('[vm-alloc-stack]') == (12 if mode == 0 or mode >= 14 else 0)
                saved = mode in (0, 4, 8, 12, 13) or mode >= 14
                native = [line for line in run.stderr.splitlines() if line.startswith('[vm-alloc-native]') and 'origin=host ' in line]
                frames = 0 if mode in (17, 18, 21, 22, 23) else native_per_sample
                assert len(native) == 12 * frames, (mode, native)
                if frames:
                    assert 'raw_lr=0x700000 pe=0x7300001234 module=0x7300000000 offset=0x1234' in native[0], native[0]
                active = [line for line in run.stderr.splitlines() if line.startswith('[vm-alloc-native]') and 'origin=syscall ' in line]
                assert len(active) == 12 * active_per_sample, (mode, active)
                syscall = [line for line in run.stderr.splitlines() if line.startswith('[vm-alloc-syscall]')]
                assert len(syscall) == 12, (mode, syscall)
                for line in syscall:
                    ok = mode != 9 and mode < 24
                    assert f'read={int(ok)} ' in line, line
                    if ok:
                        assert 'fp=0xa00000 sp=0xa00080 pc=0x700000 lr=0x700000' in line, line
                        assert 'pc_pe=0x7300001234 pc_module=0x7300000000 pc_offset=0x1234' in line, line
                        assert 'lr_pe=0x7300001234 lr_module=0x7300000000 lr_offset=0x1234' in line, line
                for line in records:
                    assert 'context=saved-context-not-current' in line, line
                    assert f'saved={int(saved)} ' in line and f'stack_read={int(mode == 0 or mode >= 14)}' in line, line
                    expected_rsp = '200001' if mode == 12 else 'fffffffffffffff8' if mode == 13 else '200000'
                    assert ('rip=0x7301234567 ' if saved else 'rip=0x0 ') in line, line
                    assert (f'rsp=0x{expected_rsp} ' if saved else 'rsp=0x0 ') in line, line
print('PASS: opt-in exact parsing, independent sampling, failed/short safe reads, 16-qword bound, no target reads')
