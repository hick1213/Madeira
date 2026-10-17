#!/usr/bin/env python3
"""Run production image placement against bounded address-space fixtures.

The regression fixture has a 355 MiB relocatable 64-bit DLL, only 123 MiB
contiguous below Madeira's advisory furniture ceiling, and a valid kernel
placement above it. The actual map_image_view, map_view, and reserved-area
classifier run unchanged; only OS mapping and unrelated diagnostics are stubbed.
No test allocates hundreds of MiB or requires an iOS device.
"""

from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    # Skip forward declarations without replacing any production decision.
    while source.index(";", start) < source.index("{", start):
        start = source.index(signature, start + len(signature))
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def production_code() -> str:
    source = (ROOT / "build/ntdll-unix/virtual_ios.c").read_text()
    private = (ROOT / "wine/dlls/ntdll/unix/unix_private.h").read_text()
    return "\n".join(
        [
            function(private, "static inline BOOL is_machine_64bit("),
            function(source, "static inline BOOL is_beyond_limit("),
            function(source, "static int fex_arena_covers("),
            function(source, "static void *map_reserved_area_inner("),
            "#define map_reserved_area map_reserved_area_inner",
            function(source, "static NTSTATUS map_view("),
            function(source, "static NTSTATUS map_image_view("),
        ]
    )


PRELUDE = r"""
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <windef.h>
#include <winnt.h>
#include <ntstatus.h>
#include <wine/list.h>
#include <wine/server_protocol.h>

#define WINE_IOS 1
#define VPROT_READ 0x01
#define VPROT_EXEC 0x04
#define VPROT_WRITECOPY 0x08
#define VPROT_COMMITTED 0x20
#define VPROT_WRITEWATCH 0x40
#define VPROT_PLACEHOLDER 0x0400
#define VPROT_FREE_PLACEHOLDER 0x0800
#define ROUND_SIZE(a,b,m) (((uintptr_t)(a) + (uintptr_t)(b) + (m)) & ~(uintptr_t)(m))
#define ROUND_ADDR(a,m) ((void *)((uintptr_t)(a) & ~(uintptr_t)(m)))
#define TRACE(...) ((void)0)
#define ERR(...) ((void)0)
#define IMAGE_SIZE ((size_t)0x1630e000)
#define LOW_GAP ((size_t)123 * 1024 * 1024)
#define LOW_PLACEMENT ((ULONG_PTR)0x7100000000)
#define HIGH_PLACEMENT ((ULONG_PTR)0x7470000000)
#define HOST_END ((ULONG_PTR)0x8000000000)

struct file_view { void *base; size_t size; unsigned int protect; };
struct reserved_area { struct list entry; void *base; size_t size; unsigned int fex_only; };
static struct list reserved_areas = LIST_INIT(reserved_areas);
static struct reserved_area fex_area;
static struct file_view made_view;
static const size_t host_page_mask = 0x3fff, granularity_mask = 0xffff;
static const ULONG_PTR limit_2g = 0x80000000, limit_4g = 0x100000000;
static const ULONG_PTR ios_furniture_ceiling = 0x73ffff0000;
static const ULONG_PTR ios_usable_va_floor = 0x7038000000;
static void *address_space_start = (void *)0x10000;
static void *user_space_limit = (void *)HOST_END;
static void *host_addr_space_limit = (void *)HOST_END;
static void *address_space_limit = (void *)0x7fffffff0000;
static void *working_set_limit = (void *)HOST_END;
static ULONG_PTR ios_spill_cap, ios_fex_arena_base_unix, ios_fex_arena_end_unix;
static ULONG_PTR wow_base, wow_ceiling = 0x100000000;
static int use_kernel_writewatch, ios_wow_window_count, ios_wow_placeholder_count;
static int ios_wow_laa_synth, ios_va_pressure, ios_exe_win_stripped_request;
static unsigned int ios_va_scan_tries, ios_va_scan_skips, ios_scan_views;
static void *ios_scan_base, *ios_scan_end, *ios_scan_fail_addr;
static size_t ios_scan_maxgap, ios_scan_tailgap;
static int ios_scan_stop, ios_scan_fail_errno;
static struct { int stage; } ios_af;
static int scan_calls, kernel_calls, fixed_calls, reserved_find_calls, reserved_fixed_calls;
static int unmap_calls, create_calls, committed_claims;
static NTSTATUS create_status = STATUS_SUCCESS, fixed_status = STATUS_CONFLICTING_ADDRESSES;
static ULONG_PTR scans_low[8], scans_high[8], kernel_candidates[12];
static unsigned int kernel_candidate_count;
static size_t low_gap = LOW_GAP;

static void require(int condition, const char *message)
{
    if (!condition) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
}

static int get_unix_prot(unsigned int vprot) { (void)vprot; return PROT_READ | PROT_WRITE | PROT_EXEC; }
static struct file_view *find_view(void *base, size_t size) { (void)base; (void)size; return NULL; }
static void set_vprot(struct file_view *view, void *base, size_t size, unsigned int prot)
{ (void)view; (void)base; (void)size; (void)prot; }
static void kernel_writewatch_register_range(struct file_view *view, void *base, size_t size)
{ (void)view; (void)base; (void)size; }
static void reset_write_watches(void *base, size_t size) { (void)base; (void)size; }
static NTSTATUS map_fixed_area(void *base, size_t size, int prot)
{
    (void)base; (void)size;
    require(!(prot & PROT_EXEC), "image reservation never asks host OS for EXEC");
    fixed_calls++;
    return fixed_status;
}
static void *map_free_area(void *start, void *end, size_t size, int top_down, int prot, size_t mask)
{
    ULONG_PTR p = wow_base ? wow_base + 0x100000 : LOW_PLACEMENT;
    (void)top_down;
    require(scan_calls < 8, "scan attempts remain bounded");
    require(!(prot & PROT_EXEC), "free-area scan never asks host OS for EXEC");
    scans_low[scan_calls] = (ULONG_PTR)start;
    scans_high[scan_calls++] = (ULONG_PTR)end;
    ios_scan_base = start; ios_scan_end = end;
    ios_scan_maxgap = low_gap; ios_scan_tailgap = 0;
    ios_scan_fail_errno = EEXIST; ios_scan_stop = 9;
    p = (p + mask) & ~mask;
    if (size <= low_gap && p >= (ULONG_PTR)start && p + size <= (ULONG_PTR)end) return (void *)p;
    if (HIGH_PLACEMENT >= (ULONG_PTR)start && HIGH_PLACEMENT + size <= (ULONG_PTR)end)
        return (void *)HIGH_PLACEMENT;
    return NULL;
}
static void *find_reserved_free_area_outside_preloader(void *start, void *end, size_t size,
                                                       int top_down, size_t mask)
{
    ULONG_PTR p = ((ULONG_PTR)start + mask) & ~mask;
    (void)top_down;
    reserved_find_calls++;
    return p < (ULONG_PTR)end && size <= (ULONG_PTR)end - p ? (void *)p : NULL;
}
static void *anon_mmap_fixed(void *ptr, size_t size, int prot, int flags)
{
    (void)size; (void)prot; (void)flags;
    reserved_fixed_calls++;
    return ptr;
}
static void *anon_mmap_alloc(size_t size, int prot)
{
    (void)size;
    require(!(prot & PROT_EXEC), "kernel fallback never asks host OS for EXEC");
    require(kernel_calls < 12, "kernel attempts remain bounded");
    if ((unsigned int)kernel_calls < kernel_candidate_count)
        return (void *)kernel_candidates[kernel_calls++];
    kernel_calls++; errno = ENOMEM; return MAP_FAILED;
}
static void ios_af_set(int stage, int error, void *base, size_t size)
{ (void)error; (void)base; (void)size; ios_af.stage = stage; }
static void ios_furniture_census(void) {}
static void ios_va_describe(void *ptr, char *out, size_t size) { (void)ptr; (void)out; (void)size; }
static const char *ios_scan_stop_name(int stop) { (void)stop; return "fixture fragmentation"; }
static int ios_storm_gate(unsigned long *counter) { (void)counter; return 0; }
static ULONG_PTR ios_wow_base(void) { return wow_base; }
static ULONG_PTR ios_wow_image_ceiling(struct pe_image_info *info) { (void)info; return wow_ceiling; }
static ULONG_PTR get_wow_user_space_limit(void) { return limit_4g; }
static int ios_wow_laa_high_images(void) { return 0; }
static int ios_wow_laa_low_first(void) { return 0; }
static int ios_wow_limits_in_window(ULONG_PTR low, ULONG_PTR high)
{ return wow_base && low >= wow_base && high <= wow_base + wow_ceiling; }
static void ios_wow_exclude_windows(void **start, void **end) { (void)start; (void)end; }
static void *ios_wow_bias_end(void *start, void *end) { (void)start; return end; }
static int ios_cage_release_on_exhaustion(void *start, void *end, size_t size)
{ (void)start; (void)end; (void)size; return 0; }
static void ios_exe_win_commit_claim(void *base, size_t size, int success)
{ (void)base; (void)size; committed_claims += success; }
static void *wine_server_get_ptr(client_ptr_t ptr) { return (void *)(ULONG_PTR)ptr; }
static void ios_pool_va_warn(const char *operation, void *base, size_t size)
{ (void)operation; (void)base; (void)size; }
static size_t unmap_area_above_user_limit(void *base, size_t size) { (void)base; return size; }
static int fixture_munmap(void *base, size_t size) { (void)base; (void)size; unmap_calls++; return 0; }
#define munmap fixture_munmap
static void *unmap_extra_space(void *base, size_t view_size, size_t size, size_t mask)
{ (void)view_size; (void)size; return (void *)(((ULONG_PTR)base + mask) & ~mask); }
static void unmap_area(void *base, size_t size) { (void)base; (void)size; unmap_calls++; }
static NTSTATUS create_view(struct file_view **out, void *base, size_t size, unsigned int protect)
{
    create_calls++;
    if (create_status) return create_status;
    made_view = (struct file_view){base, size, protect}; *out = &made_view;
    return STATUS_SUCCESS;
}
#define IOS_WOW_GUEST_FLOOR ((ULONG_PTR)0x10000)
"""


DRIVER = r"""
int main(int argc, char **argv)
{
    struct pe_image_info info = {0};
    struct file_view *view = NULL;
    NTSTATUS status;
    ULONG_PTR low = 0, high = 0;
    ULONG alloc_type = 0;
    const char *name;
    require(argc == 2, "one scenario argument");
    name = argv[1];
    info.base = 0x180000000; info.map_addr = 0x6fffe0650000;
    info.machine = IMAGE_FILE_MACHINE_AMD64;
    info.image_charact = IMAGE_FILE_DLL | IMAGE_FILE_EXECUTABLE_IMAGE | IMAGE_FILE_LARGE_ADDRESS_AWARE;
    info.image_flags = IMAGE_FLAGS_ImageDynamicallyRelocated;
    kernel_candidates[0] = HIGH_PLACEMENT; kernel_candidate_count = 1;
    list_init(&reserved_areas);

    if (!strcmp(name, "caller-high")) high = ios_furniture_ceiling - 1;
    if (!strcmp(name, "caller-wide-high")) high = HOST_END - 1;
    if (!strcmp(name, "caller-low")) low = 0x20000;
    if (!strcmp(name, "caller-redundant-low")) low = 0x10000;
    if (!strcmp(name, "caller-subfloor-low")) low = 0x8000;
    if (!strcmp(name, "stripped")) info.image_charact |= IMAGE_FILE_RELOCS_STRIPPED;
    if (!strcmp(name, "nonrelocatable")) info.image_flags = 0;
    if (!strcmp(name, "replace-placeholder")) alloc_type = MEM_REPLACE_PLACEHOLDER;
    if (!strcmp(name, "unknown-allocation-flag")) alloc_type = MEM_RESERVE;
    if (!strcmp(name, "top-down")) alloc_type = MEM_TOP_DOWN;
    if (!strcmp(name, "wow")) { wow_base = 0x7100000000; info.machine = IMAGE_FILE_MACHINE_I386; info.map_addr = 0x400000; }
    if (!strcmp(name, "x86-no-window")) info.machine = IMAGE_FILE_MACHINE_I386;
    if (!strcmp(name, "small-image")) info.map_size = 0x100000;
    if (!strcmp(name, "low-area-success") || !strcmp(name, "other-error")) low_gap = IMAGE_SIZE + 0x10000;
    if (!strcmp(name, "other-error")) create_status = STATUS_INVALID_PARAMETER;
    if (!strcmp(name, "preferred-success")) { info.map_addr = LOW_PLACEMENT; fixed_status = STATUS_SUCCESS; }
    if (!strcmp(name, "host-limit")) host_addr_space_limit = (void *)0x7200000000;
    if (!strcmp(name, "user-limit")) user_space_limit = (void *)0x7200000000;
    if (!strcmp(name, "host-below-user-wide")) host_addr_space_limit = (void *)0x7400000000;
    if (!strcmp(name, "kernel-failure")) kernel_candidate_count = 0;
    if (!strcmp(name, "kernel-above-user"))
    {
        kernel_candidates[0] = HOST_END + 0x10000; kernel_candidates[1] = HIGH_PLACEMENT;
        kernel_candidate_count = 2;
    }
    if (!strcmp(name, "kernel-spill-cap"))
    {
        ios_spill_cap = 0x7b00000000;
        kernel_candidates[0] = 0x7c00000000; kernel_candidates[1] = HIGH_PLACEMENT;
        kernel_candidate_count = 2;
    }
    if (!strcmp(name, "fex-reserved") || !strcmp(name, "fex-explicit") ||
        !strcmp(name, "generic-fex-wide"))
    {
        ios_fex_arena_base_unix = 0x7c00000000; ios_fex_arena_end_unix = HOST_END;
        fex_area = (struct reserved_area){.base = (void *)ios_fex_arena_base_unix,
            .size = ios_fex_arena_end_unix - ios_fex_arena_base_unix, .fex_only = 1};
        list_add_tail(&reserved_areas, &fex_area.entry);
    }
    if (!strcmp(name, "fex-explicit"))
    {
        status = map_view(&view, NULL, IMAGE_SIZE, 0, VPROT_READ, ios_fex_arena_base_unix,
                          ios_fex_arena_end_unix - 1, 0);
        require(status == STATUS_SUCCESS && (ULONG_PTR)view->base == 0x7c00000000,
                "an explicitly contained FEX allocation keeps its own arena");
        require(reserved_find_calls == 1 && reserved_fixed_calls == 1 && kernel_calls == 0,
                "FEX-contained request uses the production reserved-area selector");
        puts("PASS: fex-explicit"); return 0;
    }
    if (!strcmp(name, "generic-fex-wide"))
    {
        status = map_view(&view, NULL, IMAGE_SIZE, MEM_TOP_DOWN, VPROT_READ, 0x10000, HOST_END - 1, 0);
        require(status == STATUS_SUCCESS && (ULONG_PTR)view->base == HIGH_PLACEMENT,
                "a wide generic range cannot take a reserved FEX_ONLY arena");
        require(reserved_find_calls == 0 && reserved_fixed_calls == 0 && kernel_calls == 0,
                "production reserved-area guard rejects FEX before consulting its free ranges");
        puts("PASS: generic-fex-wide"); return 0;
    }

    status = map_image_view(&view, &info, info.map_size ? info.map_size : IMAGE_SIZE,
                            low, high, alloc_type, FALSE);
    if (!strcmp(name, "caller-high") || !strcmp(name, "caller-low") ||
        !strcmp(name, "stripped") || !strcmp(name, "nonrelocatable") ||
        !strcmp(name, "wow") || !strcmp(name, "x86-no-window") ||
        !strcmp(name, "replace-placeholder") || !strcmp(name, "unknown-allocation-flag") ||
        !strcmp(name, "host-limit") || !strcmp(name, "user-limit") ||
        !strcmp(name, "host-below-user-wide"))
    {
        require(status == STATUS_NO_MEMORY, "hard constraints still fail rather than silently widening");
        require(kernel_calls == 0 && view == NULL, "a constrained image never invokes an unrestricted kernel allocation");
        require(scan_calls <= 2, "hard constraints do not trigger an advisory retry");
        if (!strcmp(name, "wow"))
            for (int i = 0; i < scan_calls; i++)
                require(scans_low[i] >= wow_base && scans_high[i] <= wow_base + wow_ceiling + 1,
                        "every 32-bit image attempt stays inside its guest window");
        if (!strcmp(name, "caller-high"))
            for (int i = 0; i < scan_calls; i++)
                require(scans_high[i] <= ios_furniture_ceiling,
                        "an explicit high limit identical to the advisory value remains a hard limit");
    }
    else if (!strcmp(name, "other-error"))
    {
        require(status == STATUS_INVALID_PARAMETER && kernel_calls == 0 && create_calls == 2,
                "only address exhaustion permits the advisory retry, never other statuses");
    }
    else if (!strcmp(name, "kernel-failure"))
    {
        require(status == STATUS_NO_MEMORY && kernel_calls == 1 && view == NULL,
                "real kernel allocation failure is still reported and stays bounded");
    }
    else
    {
        require(status == STATUS_SUCCESS && view != NULL,
                "relocatable 355 MiB image escapes a fragmented advisory band to valid higher VA");
        require(view->protect & SEC_IMAGE, "successful mapping preserves SEC_IMAGE bookkeeping");
        require((ULONG_PTR)view->base >= (ULONG_PTR)address_space_start &&
                (ULONG_PTR)view->base + view->size <= (ULONG_PTR)user_space_limit &&
                (ULONG_PTR)view->base + view->size <= (ULONG_PTR)host_addr_space_limit,
                "successful image remains within actual user and host address limits");
        if (!strcmp(name, "preferred-success"))
            require(fixed_calls == 1 && committed_claims == 1 && scan_calls == 0 && kernel_calls == 0 &&
                    (ULONG_PTR)view->base == LOW_PLACEMENT,
                    "valid preferred base remains the first and only attempt");
        else if (!strcmp(name, "low-area-success") || !strcmp(name, "small-image"))
            require(scan_calls == 1 && kernel_calls == 0 && (ULONG_PTR)view->base == LOW_PLACEMENT,
                    "a successful low-area placement has no new retry");
        else if (!strcmp(name, "caller-wide-high"))
            require((ULONG_PTR)view->base == HIGH_PLACEMENT && kernel_calls == 0 && scan_calls == 1 &&
                    scans_high[0] == HOST_END,
                    "an explicit wider caller range is searched as given and never relaxed");
        else
        {
            require((ULONG_PTR)view->base == HIGH_PLACEMENT && kernel_calls >= 1,
                    "fragmented low band eventually uses the existing high kernel placement");
            require(scans_high[0] == ios_furniture_ceiling && fixed_calls == 0,
                    "low packing and rejection of an impossible 128 TiB preferred base remain first");
        }
        if (!strcmp(name, "kernel-above-user") || !strcmp(name, "kernel-spill-cap"))
            require(kernel_calls == 2 && unmap_calls == 1,
                    "the original user-limit and spill-cap retry guards remain active");
        if (!strcmp(name, "fex-reserved"))
            require(reserved_find_calls == 0 && reserved_fixed_calls == 0,
                    "generic image retry never borrows the FEX_ONLY reserved arena");
    }
    printf("PASS: %s\n", name);
    return 0;
}
"""


SCENARIOS = (
    "fragmented-large-image", "caller-high", "caller-wide-high", "caller-low",
    "caller-redundant-low", "caller-subfloor-low", "stripped", "nonrelocatable",
    "replace-placeholder", "unknown-allocation-flag", "top-down", "wow",
    "x86-no-window", "small-image", "low-area-success", "other-error",
    "preferred-success", "host-limit", "user-limit", "host-below-user-wide", "kernel-failure",
    "kernel-above-user", "kernel-spill-cap", "fex-reserved", "fex-explicit", "generic-fex-wide",
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="madeira-image-ceiling-") as directory:
        source = Path(directory) / "check.c"
        source.write_text(PRELUDE + production_code() + DRIVER)
        for name, options in (
            ("asan-ubsan", ["-O1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]),
            ("optimized", ["-O2"]),
        ):
            binary = Path(directory) / name
            subprocess.run(
                ["cc", "-std=gnu11", "-D__WINESRC__", "-Wall", "-Wextra", "-Werror", "-Wno-pragma-pack",
                 *options, "-I", str(ROOT / "wine/include"), str(source), "-o", str(binary)],
                check=True, timeout=30,
            )
            for scenario in SCENARIOS:
                result = subprocess.run([str(binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                if result.returncode:
                    raise RuntimeError(f"{name}/{scenario}: exit {result.returncode}\n{result.stderr}")
            print(f"PASS: {name}: {len(SCENARIOS)} production image-placement scenarios")


if __name__ == "__main__":
    main()
