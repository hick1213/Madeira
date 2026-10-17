#!/usr/bin/env python3
"""Exercise production virtual_free_teb across pseudo-process window owners.

The session-wide prev_teb handoff can free another process's TEB. Compile the
actual free function and registry lookups, stubbing only the OS, signal and list
boundaries. No Wine, FEX, fixed-address mappings or guest execution is needed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "build/ntdll-unix/virtual_ios.c"


def extract(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    if ";" in source[start:brace]:
        raise RuntimeError(f"Expected a function definition: {signature}")
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


PREAMBLE = r"""
#define _POSIX_C_SOURCE 200809L
#define WINE_IOS 1
#ifndef __aarch64__
#define __aarch64__ 1
#endif
#include <inttypes.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uintptr_t ULONG_PTR;
typedef size_t SIZE_T;
#define ULongToPtr(value) ((void *)(uintptr_t)(uint32_t)(value))
#define MEM_RELEASE 0x8000u
#define IOS_WOW_WINDOW_SIZE ((ULONG_PTR)UINT64_C(0x100000000))
#define B72 ((ULONG_PTR)UINT64_C(0x7200000000))
#define B73 ((ULONG_PTR)UINT64_C(0x7300000000))
#define GUEST_STACK UINT32_C(0x1930000)
#define NATIVE_STACK ((void *)(uintptr_t)UINT64_C(0x13dfb0000))
#define CPU_AREA ((void *)(uintptr_t)UINT64_C(0x13deb0000))
#define KERNEL_STACK ((void *)(uintptr_t)UINT64_C(0x13de00000))

struct list { struct list *next, *prev; };
struct ntdll_thread_data { struct list entry; void *kernel_stack; };
typedef struct { uint32_t DeallocationStack; } WOW_TEB;
typedef struct
{
    void *free_link;
    void *DeallocationStack;
    void *ChpeV2CpuAreaInfo;
    struct ntdll_thread_data GdiTebBatch;
    WOW_TEB *wow;
} TEB;
struct ios_wow_window
{
    ULONG_PTR base;
    unsigned leaked, dead;
    void **next_free_teb;
};
static struct ios_wow_window ios_wow_windows[2];
static unsigned ios_wow_window_count;
static struct { ULONG_PTR base; } ios_wow_placeholders[1];
static unsigned ios_wow_placeholder_count;
static void **next_free_teb;
static const int is_win64 = 1;
static const size_t teb_offset = 0x2000;
static int virtual_mutex;

static const char *case_name;
static unsigned checks, failures;
static unsigned release_count, enter_count, leave_count, signal_count, remove_count;
static int in_section;
static uintptr_t released[8], current_base;
static TEB *target_teb;

static void expect(int condition, const char *message)
{
    checks++;
    if (!condition)
    {
        fprintf(stderr, "FAIL: %s: %s\n", case_name, message);
        failures++;
    }
}

/* External linkage permits this current-thread boundary to become unused
 * when production no longer consults it during the target's stack release. */
ULONG_PTR ios_wow_base(void) { return current_base; }
static WOW_TEB *get_wow_teb(TEB *teb) { return teb->wow; }
static void *GetCurrentProcess(void) { return (void *)(uintptr_t)0xcafe; }

static int NtFreeVirtualMemory(void *process, void **base, SIZE_T *size, unsigned type)
{
    expect(process == GetCurrentProcess(), "release keeps the current host process handle");
    expect(*size == 0 && type == MEM_RELEASE, "release uses zero size and MEM_RELEASE");
    expect(!in_section, "stack releases precede TEB-list locking");
    expect(release_count < sizeof(released) / sizeof(released[0]), "release record capacity");
    if (release_count < sizeof(released) / sizeof(released[0]))
        released[release_count++] = (uintptr_t)*base;
    *base = NULL;
    return 0;
}

static void server_enter_uninterrupted_section(int *mutex, sigset_t *set)
{
    (void)set;
    expect(mutex == &virtual_mutex && !in_section, "enter the original virtual mutex");
    enter_count++;
    in_section = 1;
}

static void signal_free_thread(TEB *teb)
{
    expect(in_section && teb == target_teb, "signal cleanup receives the target TEB while locked");
    signal_count++;
}

static void list_remove(struct list *entry)
{
    expect(in_section && entry == &target_teb->GdiTebBatch.entry,
           "remove the target's thread-data entry while locked");
    expect(signal_count == 1, "signal cleanup precedes list removal");
    remove_count++;
}

static void server_leave_uninterrupted_section(int *mutex, sigset_t *set)
{
    (void)set;
    expect(mutex == &virtual_mutex && in_section, "leave the original virtual mutex");
    expect(signal_count == 1 && remove_count == 1, "complete both TEB cleanup boundaries");
    leave_count++;
    in_section = 0;
}
"""


CASES = r"""
enum ownership { LIVE_B72, LIVE_B73, SESSION, DEAD, LEAKED, PLACEHOLDER };
enum caller { CALLER_B72, CALLER_B73, CALLER_NATIVE, CALLER_OWNER };

static void run_case(const char *name, enum ownership ownership, enum caller caller,
                     uint32_t guest_stack, int has_wow)
{
    TEB teb;
    WOW_TEB wow = { guest_stack };
    const uintptr_t owner_base = (uintptr_t)&teb - 0x20000;
    void **const session_head = (void **)(uintptr_t)0x10001;
    void **const b72_head = (void **)(uintptr_t)0x10002;
    void **const b73_head = (void **)(uintptr_t)0x10003;
    void *const original_link = (void *)(uintptr_t)0x10004;
    int target_slot = -1;
    int drops_block = ownership == DEAD || ownership == LEAKED || ownership == PLACEHOLDER;
    unsigned before = failures;
    unsigned expected_releases = 3;
    uintptr_t expected_guest = 0;

    case_name = name;
    memset(&teb, 0, sizeof(teb));
    memset(released, 0, sizeof(released));
    memset(ios_wow_windows, 0, sizeof(ios_wow_windows));
    memset(ios_wow_placeholders, 0, sizeof(ios_wow_placeholders));
    release_count = enter_count = leave_count = signal_count = remove_count = 0;
    in_section = 0;
    target_teb = &teb;
    teb.free_link = original_link;
    teb.DeallocationStack = NATIVE_STACK;
    teb.ChpeV2CpuAreaInfo = CPU_AREA;
    teb.GdiTebBatch.kernel_stack = KERNEL_STACK;
    teb.wow = has_wow ? &wow : NULL;
    next_free_teb = session_head;
    ios_wow_window_count = 2;
    ios_wow_placeholder_count = 0;
    ios_wow_windows[0].base = B72;
    ios_wow_windows[0].next_free_teb = b72_head;
    ios_wow_windows[1].base = B73;
    ios_wow_windows[1].next_free_teb = b73_head;
    if (ownership == LIVE_B72 || ownership == LIVE_B73)
    {
        target_slot = ownership == LIVE_B72 ? 0 : 1;
        ios_wow_windows[target_slot].base = owner_base;
    }
    else if (ownership == DEAD || ownership == LEAKED)
    {
        ios_wow_windows[1].base = owner_base;
        ios_wow_windows[1].dead = ownership == DEAD;
        ios_wow_windows[1].leaked = ownership == LEAKED;
    }
    else if (ownership == PLACEHOLDER)
    {
        ios_wow_placeholder_count = 1;
        ios_wow_placeholders[0].base = owner_base;
    }
    current_base = caller == CALLER_NATIVE ? 0 :
                   caller == CALLER_OWNER ? owner_base :
                   ios_wow_windows[caller == CALLER_B72 ? 0 : 1].base;
    if (guest_stack && has_wow && !drops_block)
    {
        expected_guest = guest_stack + (target_slot >= 0 ? owner_base : 0);
        expected_releases++;
    }

    virtual_free_teb(&teb);

    expect(release_count == expected_releases, "release exactly the target's native and eligible guest blocks");
    expect(released[0] == (uintptr_t)NATIVE_STACK, "preserve native stack release");
    expect(released[1] == (uintptr_t)CPU_AREA, "preserve ARM64 CPU-area release");
    expect(released[2] == (uintptr_t)KERNEL_STACK, "preserve kernel-stack release");
    if (expected_guest)
    {
        if (released[3] != expected_guest)
            fprintf(stderr, "DETAIL: %s: guest release=%#" PRIxPTR
                    " expected=%#" PRIxPTR " caller-base=%#" PRIxPTR "\n",
                    name, released[3], expected_guest, current_base);
        expect(released[3] == expected_guest, "release the guest stack in the target TEB's owner window");
    }
    expect(teb.DeallocationStack == NULL && teb.ChpeV2CpuAreaInfo == NULL &&
           teb.GdiTebBatch.kernel_stack == NULL, "preserve OS release output handling");
    expect(wow.DeallocationStack == guest_stack, "guest TEB field retains its stored guest address");
    expect(enter_count == 1 && leave_count == 1 && signal_count == 1 &&
           remove_count == 1 && !in_section, "preserve one complete TEB cleanup section");
    expect(next_free_teb == (ownership == SESSION ? (void **)&teb : session_head),
           "only a session TEB reaches the session free list");
    expect(ios_wow_windows[0].next_free_teb == (target_slot == 0 ? (void **)&teb : b72_head),
           "preserve the B72 free-list owner");
    expect(ios_wow_windows[1].next_free_teb == (target_slot == 1 ? (void **)&teb : b73_head),
           "preserve the B73 free-list owner");
    expect(teb.free_link == (drops_block ? original_link :
           ownership == SESSION ? (void *)session_head :
           target_slot == 0 ? (void *)b72_head : (void *)b73_head),
           "link live blocks to their original pool and leave dropped blocks untouched");
    printf("%s: %s\n", failures == before ? "PASS" : "FAIL", name);
}

int main(void)
{
    run_case("current B72 frees B73 TEB", LIVE_B73, CALLER_B72, GUEST_STACK, 1);
    run_case("native current thread frees B72 TEB", LIVE_B72, CALLER_NATIVE, GUEST_STACK, 1);
    run_case("current B73 frees session identity TEB", SESSION, CALLER_B73, GUEST_STACK, 1);
    run_case("current owner frees its own TEB", LIVE_B73, CALLER_OWNER, GUEST_STACK, 1);
    run_case("dead owner drops guest release and TEB block", DEAD, CALLER_B72, GUEST_STACK, 1);
    run_case("leaked owner drops guest release and TEB block", LEAKED, CALLER_B72, GUEST_STACK, 1);
    run_case("placeholder drops guest release and TEB block", PLACEHOLDER, CALLER_B72, GUEST_STACK, 1);
    run_case("zero guest stack keeps native cleanup and owner pool", LIVE_B73, CALLER_B72, 0, 1);
    run_case("absent WOW TEB keeps native cleanup and owner pool", LIVE_B73, CALLER_B72, GUEST_STACK, 0);
    printf("%u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    source = SOURCE.read_text()
    definitions = "\n\n".join(
        extract(source, signature)
        for signature in (
            "static int ios_wow_addr_in_any_window(",
            "static struct ios_wow_window *ios_wow_live_slot_for_addr(",
            "void virtual_free_teb(",
        )
    )
    code = PREAMBLE + "\n" + definitions + "\n" + CASES
    result = {"source": str(SOURCE.relative_to(ROOT)),
              "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
        (args.evidence / "production-functions.c").write_text(definitions + "\n")
        (args.evidence / "contract.c").write_text(code)
    with tempfile.TemporaryDirectory(prefix="madeira-teb-stack-owner-") as td:
        fixture = Path(td) / "contract.c"
        fixture.write_text(code)
        for name, flags in (
            ("sanitizers", ["-O1", "-g", "-fsanitize=address,undefined",
                            "-fno-sanitize-recover=all", "-fno-omit-frame-pointer"]),
            ("optimized", ["-O2"]),
        ):
            binary = Path(td) / f"contract-{name}"
            command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                       *flags, str(fixture), "-o", str(binary)]
            compilation = subprocess.run(command, capture_output=True, text=True, timeout=60)
            entry = {"compile_command": command,
                     "compile_exit_code": compilation.returncode,
                     "compile_stdout": compilation.stdout,
                     "compile_stderr": compilation.stderr}
            result[name] = entry
            if compilation.returncode:
                print(f"{name}: compilation failed\n{compilation.stdout}{compilation.stderr}", end="")
                continue
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            entry.update(exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr)
            print(f"{name}: exit {run.returncode}\n{run.stdout}{run.stderr}", end="")
    if args.evidence:
        (args.evidence / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return int(any(result[name].get("exit_code", 1) for name in ("sanitizers", "optimized")))


if __name__ == "__main__":
    raise SystemExit(main())
