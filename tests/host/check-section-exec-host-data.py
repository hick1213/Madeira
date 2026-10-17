#!/usr/bin/env python3
"""Compile the proposed executable-section classifier against mock views.

The CoreCLR section-sharing mechanism is a hypothesis pending the exact
SEC_RESERVE dual-view fixture and a matched device test. These host checks
verify classification and safety boundaries, not runtime sharing.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

if not __debug__:
    raise RuntimeError('Run this regression check without Python optimization; integration assertions are required.')

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'build/ntdll-unix/virtual_ios.c'


def function(source: str, name: str, return_type: str = 'int') -> str:
    start = source.index('static ' + return_type + ' ' + name + '(')
    while source.index(';', start) < source.index('{', start):
        start = source.index('static ' + return_type + ' ' + name + '(', start + 1)
    brace = source.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


source = SOURCE.read_text()

# Integration ordering inside mprotect_exec: the image rule and the anonymous
# RWX rule keep priority, the section rule is the last host-data branch, and it
# only ever removes the host EXEC bit (Wine's VPROT_EXEC bookkeeping stays).
body = source[source.index('static inline int mprotect_exec('):]
body = body[:body.index('/* ml247: catch WHO narrows maxprot on pool pages.')]
image_at = body.index('ios_guest_image_is_host_data( base, size )')
anon_at = body.index('ios_guest_anon_rwx_is_host_data( base, size )')
section_at = body.index('ios_guest_exec_section_is_host_data( base, size )')
assert image_at < anon_at < section_at
tail = body[section_at:]
assert 'unix_prot &= ~PROT_EXEC;' in tail and 'if (!unix_prot) unix_prot = PROT_READ;' in tail
assert 'MADEIRA_GUEST_SECTION_EXEC_DATA' in source
# The pool path must report when it is about to detach a section view.
assert '[sec-pool] ml1400' in source
# The mock below must mirror the production definition of a section view.
assert 'return !(view->protect & (SEC_FILE | SEC_RESERVE | SEC_COMMIT));' in source

enabled = function(source, 'ios_guest_section_exec_data_enabled')
view_ok = function(source, 'ios_guest_section_exec_view_ok')
classifier = function(source, 'ios_guest_exec_section_is_host_data')

preamble = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windef.h>
#include <winnt.h>
#define WINE_IOS 1
#define VPROT_ARM64EC 0x100
#define VPROT_SYSTEM 0x200
#define VPROT_WRITE 0x02
struct file_view { void *base; size_t size; unsigned int protect; };
static struct file_view views[4];
static int nviews, alias_cover;
static uintptr_t alias_base, alias_end;
#define host_page_mask ((uintptr_t)0x3fff)
static struct file_view *arm64ec_view = (struct file_view *)1;
static int ios_alloc_ec_code;
void *ios_jit_rx_base_global;
size_t ios_jit_pool_size_global;
static inline int is_view_valloc( const struct file_view *view )
{
    return !(view->protect & (SEC_FILE | SEC_RESERVE | SEC_COMMIT));
}
static struct file_view *find_view( const void *addr, size_t size )
{
    int i;
    (void)size;
    for (i = 0; i < nviews; i++)
        if ((uintptr_t)addr >= (uintptr_t)views[i].base &&
            (uintptr_t)addr < (uintptr_t)views[i].base + views[i].size) return &views[i];
    return NULL;
}
int ios_jit_anon_alias_overlaps(void *a, size_t s, uintptr_t *b, uintptr_t *e)
{
    uintptr_t start = (uintptr_t)a;
    (void)b; (void)e;
    return alias_cover || (alias_base && start < alias_end && alias_base < start + s);
}
int ios_jit_anon_alias_find_cover( void *a, size_t s, void **rw, void **rx )
{
    (void)a; (void)s; (void)rw; (void)rx;
    return alias_cover;
}
'''

cases = r'''
#define BASE ((void *)0x7190ec0000ull)
static void reset(void)
{
    memset(views, 0, sizeof(views)); nviews = 0; alias_cover = 0; alias_base = alias_end = 0; ios_alloc_ec_code = 0;
    arm64ec_view = (struct file_view *)1; ios_jit_rx_base_global = NULL; ios_jit_pool_size_global = 0;
    views[0] = (struct file_view){ BASE, 0x10000, SEC_COMMIT }; nviews = 1;
}
int main(int argc, char **argv)
{
    int disabled = argc > 1 && !strcmp(argv[1], "disabled");
    reset();
    if (disabled)
    {
        /* MADEIRA_GUEST_SECTION_EXEC_DATA=0 must restore the pool path everywhere. */
        assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
        puts("PASS: MADEIRA_GUEST_SECTION_EXEC_DATA=0 restores the pool path");
        return 0;
    }
    /* the CoreCLR shape: a 64 KB executable view of an anonymous section */
    assert(ios_guest_exec_section_is_host_data(BASE, 0x10000));
    assert(ios_guest_exec_section_is_host_data((char *)BASE + 0x4000, 0x4000));
    views[0].protect = SEC_RESERVE; assert(ios_guest_exec_section_is_host_data(BASE, 0x10000));
    views[0].protect = SEC_COMMIT | VPROT_WRITE; assert(ios_guest_exec_section_is_host_data(BASE, 0x10000));
    /* everything that must keep its current path */
    reset(); views[0].protect = 0; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));      /* VirtualAlloc view */
    reset(); views[0].protect = SEC_IMAGE | SEC_COMMIT; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); views[0].protect = SEC_FILE | SEC_COMMIT; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); views[0].protect |= VPROT_ARM64EC; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); views[0].protect |= VPROT_SYSTEM; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); ios_alloc_ec_code = 1; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); arm64ec_view = NULL; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    reset(); alias_cover = 1; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));
    /* Partial aliases cannot be detached by classifying their wider host page. */
    reset(); alias_base = (uintptr_t)BASE + 0x1000; alias_end = alias_base + 0x1000;
    assert(!ios_guest_exec_section_is_host_data(BASE, 0x4000));
    reset(); alias_base = (uintptr_t)BASE - 0x1000; alias_end = (uintptr_t)BASE + 0x1000;
    assert(!ios_guest_exec_section_is_host_data(BASE, 0x4000));
    reset(); alias_base = (uintptr_t)BASE + 0x3000; alias_end = alias_base + 0x2000;
    assert(!ios_guest_exec_section_is_host_data(BASE, 0x4000));
    reset(); assert(!ios_guest_exec_section_is_host_data(BASE, 0));                                    /* empty range */
    reset(); nviews = 0; assert(!ios_guest_exec_section_is_host_data(BASE, 0x10000));                  /* no view at all */
    /* the JIT pool itself, whatever it is made of, is never reclassified */
    reset(); ios_jit_rx_base_global = (void *)0x7190ed0000ull; ios_jit_pool_size_global = 0x100000;
    assert(ios_guest_exec_section_is_host_data(BASE, 0x10000));                                        /* below the pool */
    assert(!ios_guest_exec_section_is_host_data((char *)BASE + 0xc000, 0x8000));                       /* crosses into it */
    /* a range spanning views: every one must qualify, a rounding gap is fine */
    reset(); views[0].size = 0x1000; views[1] = (struct file_view){ (char *)BASE + 0x4000, 0x4000, SEC_COMMIT }; nviews = 2;
    assert(ios_guest_exec_section_is_host_data(BASE, 0x8000));
    views[1].protect = 0; assert(!ios_guest_exec_section_is_host_data(BASE, 0x8000));
    views[1].protect = SEC_IMAGE | SEC_COMMIT; assert(!ios_guest_exec_section_is_host_data(BASE, 0x8000));
    /* Missing ranges must not borrow a later qualifying view. */
    reset(); views[0].base = (char *)BASE + 0x4000;
    assert(!ios_guest_exec_section_is_host_data(BASE, 0x8000));
    reset(); views[0].size = 0x1000;
    assert(!ios_guest_exec_section_is_host_data(BASE, 0x8000));
    reset(); assert(!ios_guest_exec_section_is_host_data((void *)(UINTPTR_MAX - 0xfff), 0x2000));
    reset(); views[0] = (struct file_view){ (void *)(UINTPTR_MAX - 0xfff), 0x2000, SEC_COMMIT };
    assert(!ios_guest_exec_section_is_host_data(views[0].base, 0x800));
    /* 4 KB view inside a 16 KB host-page request */
    reset(); views[0].size = 0x1000; assert(ios_guest_exec_section_is_host_data(BASE, 0x4000));
    puts("PASS: production classifier section/valloc/image/file/EC/system/pool/alias/range/disabled");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='madeira-section-exec-') as td:
    c = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    c.write_text(preamble + '\n' + enabled + '\n' + view_ok + '\n' + classifier + '\n' + cases)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-pragma-pack',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(ROOT / 'wine/include'), str(c), '-o', str(binary)], check=True)
    env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_GUEST_SECTION_EXEC_DATA'}
    subprocess.run([str(binary)], check=True, env=env)
    subprocess.run([str(binary), 'disabled'], check=True, env=dict(env, MADEIRA_GUEST_SECTION_EXEC_DATA='0'))
print('PASS: check-section-exec-host-data')
sys.exit(0)
