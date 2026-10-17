#!/usr/bin/env python3
"""Run the production anonymous guest-memory classifier against mock views.

These sanitizer checks cover address ownership and range boundaries. They do
not execute guest code, allocate executable host memory, or prove device SMC.
"""

from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "build/ntdll-unix/virtual_ios.c"


def function(source: str, signature: str) -> str:
    """Extract the named production definition, including its function body."""
    start = source.index(signature)
    brace = source.index("{", start)
    if ";" in source[start:brace]:
        raise RuntimeError(f"Expected a function definition: {signature}")
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


source = SOURCE.read_text()
definitions = [
    function(source, "static int ios_guest_rwx_data_enabled("),
    function(source, "static int ios_guest_anon_rwx_view_ok("),
    function(source, "static int ios_wow_rwx_plain_mode("),
    function(source, "static struct ios_wow_window *ios_wow_rwx_plain_window("),
    function(source, "static int ios_wow_rwx_view_ok("),
    function(source, "static int ios_guest_anon_rwx_is_host_data("),
]

preamble = r"""
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
#define VPROT_EXEC 0x04
#define IOS_WOW_WINDOW_SIZE ((uintptr_t)0x100000000ull)
#define host_page_mask ((uintptr_t)0x3fff)
#define BASE ((uintptr_t)0x7100000000ull)
struct file_view { void *base; size_t size; unsigned int protect; };
struct ios_wow_window { uintptr_t base; unsigned wine_mono, dead, leaked; };
static struct file_view views[4], *forced_view;
static struct ios_wow_window window;
static int nviews, ios_alloc_ec_code;
static struct file_view *arm64ec_view;
static uintptr_t alias_base, alias_end;
void *ios_jit_rx_base_global;
size_t ios_jit_pool_size_global;

static inline int is_view_valloc(const struct file_view *view)
{
    return !(view->protect & (SEC_FILE | SEC_RESERVE | SEC_COMMIT));
}

static struct file_view *find_view(const void *addr, size_t size)
{
    uintptr_t a = (uintptr_t)addr;
    int i;
    (void)size;
    if (forced_view) return forced_view;
    for (i = 0; i < nviews; i++)
        if (a >= (uintptr_t)views[i].base &&
            a - (uintptr_t)views[i].base < views[i].size) return &views[i];
    return NULL;
}

static struct ios_wow_window *ios_wow_live_slot_for_addr(const void *addr)
{
    uintptr_t a = (uintptr_t)addr;
    if (!window.base || window.dead || window.leaked) return NULL;
    return a >= window.base && a - window.base < IOS_WOW_WINDOW_SIZE ? &window : NULL;
}

int ios_jit_anon_alias_find_cover(void *addr, size_t size, void **rw, void **rx)
{
    uintptr_t b = (uintptr_t)addr;
    (void)rw; (void)rx;
    return alias_base && b >= alias_base && size <= alias_end - b && b < alias_end;
}

int ios_jit_anon_alias_overlaps(void *addr, size_t size, uintptr_t *b, uintptr_t *e)
{
    uintptr_t start = (uintptr_t)addr;
    (void)b; (void)e;
    return alias_base && start < alias_end && alias_base < start + size;
}
"""

cases = r"""
static void reset(void)
{
    memset(views, 0, sizeof(views));
    window = (struct ios_wow_window){ BASE, 0, 0, 0 };
    views[0] = (struct file_view){ (void *)(BASE + 0x10000), 0x10000, VPROT_WRITE | VPROT_EXEC };
    nviews = 1; forced_view = NULL; ios_alloc_ec_code = 0; arm64ec_view = NULL;
    alias_base = alias_end = 0; ios_jit_rx_base_global = NULL; ios_jit_pool_size_global = 0;
}

static int classify(uintptr_t base, size_t size)
{
    return ios_guest_anon_rwx_is_host_data((const void *)base, size);
}

#define EXPECT(name, wanted, base, size) do { \
    int actual = classify((uintptr_t)(base), (size)); \
    if (actual != (wanted)) { \
        fprintf(stderr, "FAIL: %s expected %d, got %d\n", name, wanted, actual); \
        failures++; \
    } \
    checks++; \
} while (0)

int main(int argc, char **argv)
{
    int checks = 0, failures = 0;
    if (argc != 2) return 2;
    reset();
    if (!strcmp(argv[1], "disabled"))
    {
        window.wine_mono = 1;
        EXPECT("explicit rollback", 0, BASE + 0x10000, 0x10000);
    }
    else if (!strcmp(argv[1], "default"))
    {
        EXPECT("default live window without Wine Mono", 1, BASE + 0x10000, 0x10000);
        window.wine_mono = 1;
        EXPECT("default live Wine Mono window", 1, BASE + 0x10000, 0x10000);
        reset(); window.dead = 1;
        EXPECT("default excludes dead windows", 0, BASE + 0x10000, 0x10000);
        reset(); window.leaked = 1;
        EXPECT("default excludes leaked windows", 0, BASE + 0x10000, 0x10000);
        reset(); window.base = 0;
        EXPECT("default excludes unknown 32-bit ownership", 0, BASE + 0x10000, 0x10000);
    }
    else if (!strcmp(argv[1], "data-disabled"))
    {
        window.base = 0; arm64ec_view = (struct file_view *)1; views[0].size = 0x41000;
        EXPECT("64-bit data opt-out", 0, BASE + 0x10000, 0x4000);
    }
    else if (!strcmp(argv[1], "ranges"))
    {
        EXPECT("live 32-bit code chunk without Wine Mono", 1, BASE + 0x10000, 0x10000);
        EXPECT("interior page", 1, BASE + 0x14000, 0x4000);
        reset(); views[0].protect = 0;
        EXPECT("32-bit protection transition", 1, BASE + 0x10000, 0x4000);
        reset(); ios_alloc_ec_code = 1;
        EXPECT("native EC allocation", 0, BASE + 0x10000, 0x4000);
        reset(); window.dead = 1;
        EXPECT("dead window", 0, BASE + 0x10000, 0x4000);
        reset(); window.leaked = 1;
        EXPECT("leaked window", 0, BASE + 0x10000, 0x4000);
        reset(); window.base = 0;
        EXPECT("unknown window", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= SEC_IMAGE;
        EXPECT("image view", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= SEC_FILE;
        EXPECT("file view", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= SEC_RESERVE;
        EXPECT("reserved section", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= SEC_COMMIT;
        EXPECT("committed section", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= VPROT_ARM64EC;
        EXPECT("native EC view", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].protect |= VPROT_SYSTEM;
        EXPECT("system view", 0, BASE + 0x10000, 0x4000);
        reset(); EXPECT("empty request", 0, BASE + 0x10000, 0);
        reset(); EXPECT("request overflow", 0, UINTPTR_MAX - 0xfff, 0x2000);
        reset(); nviews = 0;
        EXPECT("missing view", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].base = (void *)(BASE + 0x14000);
        EXPECT("leading hole", 0, BASE + 0x10000, 0x8000);
        reset(); views[0].size = 0x1000;
        EXPECT("final host-page padding", 1, BASE + 0x10000, 0x4000);
        EXPECT("unowned pages after padding", 0, BASE + 0x10000, 0x8000);
        reset(); views[0].size = 0x1000;
        views[1] = (struct file_view){ (void *)(BASE + 0x18000), 0x4000, 0 }; nviews = 2;
        EXPECT("interior hole before a qualifying view", 0, BASE + 0x10000, 0xc000);
        views[1].base = (void *)(BASE + 0x14000);
        EXPECT("adjacent qualifying host pages", 1, BASE + 0x10000, 0x8000);
        views[1].protect = VPROT_ARM64EC;
        EXPECT("adjacent disallowed view", 0, BASE + 0x10000, 0x8000);
        reset(); alias_base = BASE + 0x10000; alias_end = BASE + 0x20000;
        EXPECT("full alias", 0, BASE + 0x10000, 0x4000);
        reset(); alias_base = BASE + 0x11000; alias_end = BASE + 0x12000;
        EXPECT("interior alias", 0, BASE + 0x10000, 0x4000);
        reset(); alias_base = BASE + 0xf000; alias_end = BASE + 0x11000;
        EXPECT("alias crossing start", 0, BASE + 0x10000, 0x4000);
        reset(); alias_base = BASE + 0x13000; alias_end = BASE + 0x15000;
        EXPECT("alias crossing end", 0, BASE + 0x10000, 0x4000);
        reset(); alias_base = BASE + 0x14000; alias_end = BASE + 0x15000;
        EXPECT("alias adjacent to end", 1, BASE + 0x10000, 0x4000);
        reset(); ios_jit_rx_base_global = (void *)(BASE + 0x20000); ios_jit_pool_size_global = 0x4000;
        EXPECT("pool adjacent to end", 1, BASE + 0x10000, 0x10000);
        EXPECT("request entering pool", 0, BASE + 0x1c000, 0x8000);
        reset(); ios_jit_rx_base_global = (void *)(UINTPTR_MAX - 0xfff); ios_jit_pool_size_global = 0x2000;
        EXPECT("pool extent overflow", 0, BASE + 0x10000, 0x4000);
        reset(); views[0].base = (void *)(BASE + IOS_WOW_WINDOW_SIZE - 0x4000); views[0].size = 0x8000;
        EXPECT("range crossing window end", 0, BASE + IOS_WOW_WINDOW_SIZE - 0x4000, 0x8000);
        views[0].size = 0x11000; arm64ec_view = (struct file_view *)1;
        EXPECT("cross-window range cannot use the 64-bit data fallback", 0,
               BASE + IOS_WOW_WINDOW_SIZE - 0x4000, 0x8000);
        reset(); window.base = UINTPTR_MAX - 0x1ffff;
        views[0].base = (void *)(window.base + 0x1000);
        EXPECT("window extent overflow", 0, window.base + 0x1000, 0x4000);
        reset(); forced_view = &views[0]; views[0].size = 0;
        EXPECT("zero-sized bookkeeping view", 0, BASE + 0x10000, 0x4000);
        reset(); forced_view = &views[0]; views[0].base = (void *)(BASE + 0x14000);
        EXPECT("bookkeeping base beyond requested address", 0, BASE + 0x10000, 0x4000);
        reset(); forced_view = &views[0]; views[0].size = SIZE_MAX;
        EXPECT("bookkeeping view overflow", 0, BASE + 0x10000, 0x4000);
        reset(); forced_view = &views[0]; views[0].base = (void *)(BASE + 0x8000); views[0].size = 0x1000;
        EXPECT("bookkeeping view ends before requested address", 0, BASE + 0x10000, 0x4000);
        /* The existing 64-bit data shape stays distinct from WoW code chunks. */
        reset(); window.base = 0; arm64ec_view = (struct file_view *)1;
        EXPECT("64-bit 64 KB code chunk", 0, BASE + 0x10000, 0x10000);
        views[0].size = 0x41000;
        EXPECT("64-bit odd-sized RWX data", 1, BASE + 0x10000, 0x44000);
        views[0].protect &= ~VPROT_WRITE;
        EXPECT("64-bit data requires write", 0, BASE + 0x10000, 0x4000);
        reset(); window.base = 0; arm64ec_view = (struct file_view *)1; views[0].size = 0x41000;
        alias_base = BASE + 0x11000; alias_end = BASE + 0x12000;
        EXPECT("64-bit data partial alias", 0, BASE + 0x10000, 0x4000);
        reset(); window.base = 0; arm64ec_view = (struct file_view *)1; views[0].size = 0x11000;
        views[1] = (struct file_view){ (void *)(BASE + 0x28000), 0x11000, VPROT_WRITE | VPROT_EXEC }; nviews = 2;
        EXPECT("64-bit data interior hole", 0, BASE + 0x10000, 0x28000);
        reset(); window.base = 0; arm64ec_view = (struct file_view *)1;
        views[0].base = (void *)(UINTPTR_MAX - 0x13fff); views[0].size = 0x11000;
        EXPECT("host-page rounding overflow", 0, (uintptr_t)views[0].base, 0x1000);
    }
    else return 2;
    printf("%s: %d checks, %d failures\n", argv[1], checks, failures);
    return failures != 0;
}
"""

with tempfile.TemporaryDirectory(prefix="madeira-wow-rwx-") as directory:
    c_source = Path(directory) / "check.c"
    binary = Path(directory) / "check"
    c_source.write_text(preamble + "\n" + "\n".join(definitions) + "\n" + cases)
    subprocess.run(
        [
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-pragma-pack",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(ROOT / "wine/include"), str(c_source), "-o", str(binary),
        ],
        check=True, timeout=60,
    )
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"MADEIRA_WOW_RWX_PLAIN", "MADEIRA_GUEST_RWX_DATA"}
    }
    results = []
    for name, overrides in [
        ("ranges", {"MADEIRA_WOW_RWX_PLAIN": "1"}),
        ("disabled", {"MADEIRA_WOW_RWX_PLAIN": "0"}),
        ("default", {}),
        ("default", {"MADEIRA_WOW_RWX_PLAIN": ""}),
        ("data-disabled", {"MADEIRA_WOW_RWX_PLAIN": "1", "MADEIRA_GUEST_RWX_DATA": "0"}),
    ]:
        try:
            result = subprocess.run([str(binary), name], env=dict(env, **overrides), timeout=5)
            results.append(result.returncode)
        except subprocess.TimeoutExpired:
            print(f"FAIL: {name}: production classifier failed to complete within 5 seconds")
            results.append(1)
    if any(results):
        raise SystemExit(1)

print("PASS: check-wow-rwx-host-data")
