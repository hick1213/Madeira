#!/usr/bin/env python3
# Copyright 2026 125hz. GPL-3.0-or-later, like the rest of this repository.
"""Exercise the production iOS monitor probes with real Darwin VM protection.

Extract the waiter registry, diagnostic functions and guest-exception logger.
Only Wine types, the clock/TEB accessors, and the external SRW reaper are adapted.
Mach reads, mmap, mprotect, munmap, msync and concurrent protection changes are
real host APIs. Every scenario runs in a separate, bounded child with core dumps
disabled, in both optimized and AddressSanitizer/UndefinedBehaviorSanitizer builds.
"""

from pathlib import Path
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def function(source: str, signature: str) -> str:
    """Use the repository's column-zero closing-brace extraction convention."""
    start = source.index(signature)
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def production_code() -> str:
    sync = (ROOT / "wine/dlls/ntdll/unix/sync.c").read_text()
    signal = (ROOT / "build/ntdll-unix/signal_arm64_ios.c").read_text()
    start = sync.index("#define IOS_ALERT_WAITER_MAX")
    end = sync.index("static int ios_alert_waiter_slot", start)
    registry = sync[start:end]
    helper = ""
    signature = "static int ios_alert_waiter_read("
    if signature in sync:
        helper = function(sync, signature)
    return "\n".join(
        [
            registry,
            helper,
            function(sync, "static int ios_alert_waiter_slot("),
            function(sync, "int ios_alert_waiter_lookup("),
            function(sync, "void ios_alert_waiter_dump("),
            function(sync, "void ios_orphan_check("),
            function(signal, "static void ios_log_guest_exception("),
        ]
    )


PRELUDE = r"""
#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>

typedef uintptr_t ULONG_PTR;
typedef uint64_t ULONG64;
typedef unsigned long long ULONGLONG;
typedef long long LONGLONG;
typedef uint16_t WCHAR;
typedef struct { LONGLONG QuadPart; } LARGE_INTEGER;
typedef struct { void *UniqueProcess, *UniqueThread; } CLIENT_ID;
typedef struct { CLIENT_ID ClientId; } TEB;
typedef struct {
    uint32_t ExceptionCode, ExceptionFlags;
    void *ExceptionAddress;
    uint32_t NumberParameters;
    ULONG_PTR ExceptionInformation[15];
} EXCEPTION_RECORD;

static TEB teb;
static TEB *current_teb;
static TEB *NtCurrentTeb(void) { return current_teb; }
static void NtQuerySystemTime(LARGE_INTEGER *out) { out->QuadPart = 100LL * 10000000; }
static void *InterlockedCompareExchangePointer(void *volatile *dest, void *value, void *old)
{
    return __sync_val_compare_and_swap(dest, old, value);
}
static int reaped;
static void ios_srw_reap_exclusive(unsigned long long lock, unsigned long long dead)
{
    (void)lock; (void)dead; reaped++;
}
static void require(int condition, const char *what)
{
    if (!condition) { fprintf(stderr, "SETUP/ASSERT: %s errno=%d\n", what, errno); exit(2); }
}
"""


DRIVER = r"""
static void register_waiters(const void *addr, int count, int old)
{
    int i;
    memset(ios_alert_waiters, 0, sizeof(ios_alert_waiters));
    for (i = 0; i < count; i++)
    {
        void *tid = (void *)(uintptr_t)(0x1230 + i);
        int slot = ios_alert_waiter_slot(tid);
        require(slot >= 0, "registry has a waiter slot");
        ios_alert_waiters[slot].addr = addr;
        ios_alert_waiters[slot].since = (100ULL - (old ? 65 : 1)) * 10000000;
        ios_alert_waiters[slot].inf = 1;
    }
    {
        const void *found = NULL;
        int age = -1, inf = 0;
        require(ios_alert_waiter_lookup(0x1230, &found, &age, &inf), "lookup finds real registry row");
        require(found == addr && age == (old ? 65 : 1) && inf == 1, "lookup keeps address/age/infinite flag");
    }
}

struct race_state { unsigned char *page; size_t size; atomic_int stop, changes, started; };
static void *protect_race(void *arg)
{
    struct race_state *state = arg;
    while (!atomic_load(&state->stop))
    {
        require(mprotect(state->page, state->size, PROT_NONE) == 0, "race removes read permission");
        require(mprotect(state->page, state->size, PROT_READ | PROT_WRITE) == 0, "race restores read permission");
        atomic_fetch_add(&state->changes, 1);
        atomic_store(&state->started, 1); /* First complete protection cycle makes the thread ready. */
    }
    return NULL;
}

int main(int argc, char **argv)
{
    struct rlimit limit = {0, 0};
    const char *name;
    size_t page, size;
    unsigned char *memory, *addr;
    int hot, word, orphan;
    require(setrlimit(RLIMIT_CORE, &limit) == 0, "core dumps disabled");
    require(argc == 2, "one scenario name");
    name = argv[1];
    if (!strncmp(name, "logger-", 7))
    {
        EXCEPTION_RECORD rec = {0};
        teb.ClientId.UniqueThread = (void *)0xabcd;
        current_teb = !strcmp(name, "logger-null-teb") ? NULL : &teb;
        rec.ExceptionCode = 0xc0000005;
        rec.ExceptionFlags = 2;
        rec.ExceptionAddress = (void *)0x1234;
        rec.NumberParameters = 2;
        rec.ExceptionInformation[0] = 0x44;
        rec.ExceptionInformation[1] = 0x55;
        ios_log_guest_exception("host-probe", &rec, 0x5678);
        if (!strcmp(name, "logger-readable-strings"))
        {
            const char ansi[] = "readable ANSI control";
            const WCHAR wide[] = {'r','e','a','d','a','b','l','e',' ','W','I','D','E',0};
            rec.ExceptionCode = 0x40010006;
            rec.ExceptionInformation[0] = sizeof(ansi) - 1;
            rec.ExceptionInformation[1] = (ULONG_PTR)ansi;
            ios_log_guest_exception("host-probe", &rec, 0x5678);
            rec.ExceptionCode = 0x4001000a;
            rec.ExceptionInformation[0] = sizeof(wide) / sizeof(wide[0]) - 1;
            rec.ExceptionInformation[1] = (ULONG_PTR)wide;
            ios_log_guest_exception("host-probe", &rec, 0x5678);
        }
        puts("CASE_OK");
        return 0;
    }

    page = (size_t)getpagesize();
    require(page == 0x4000, "run on the 16 KiB Darwin host matching iOS page guards");
    size = page * 4;
    /* A hint, never MAP_FIXED: do not overwrite an existing host mapping. The
     * production lock-word guard intentionally requires an address below 512 GB. */
    memory = mmap((void *)0x2000000000ULL, size, PROT_READ | PROT_WRITE,
                  MAP_PRIVATE | MAP_ANON, -1, 0);
    require(memory != MAP_FAILED, "real anonymous mapping");
    require((uintptr_t)memory > 0x10000 && (uintptr_t)(memory + size) < 0x8000000000ULL,
            "mapping enters the production address-range guards");
    memset(memory, 0x5a, size);
    addr = memory + page + 0x180;
    hot = !strncmp(name, "hot-", 4);
    word = !strncmp(name, "word-", 5);
    orphan = !strncmp(name, "orphan-", 7);
    require(hot || word || orphan || !strcmp(name, "protect-race"), "known scenario");
    if (orphan || !strcmp(name, "protect-race")) addr += 2; /* SRW-exclusive wait cookie: lock + 2. */
    if (!strcmp(name, "hot-boundary-next-none")) addr = memory + page * 2 - 0x20;
    if (!strcmp(name, "hot-boundary-prev-none")) addr = memory + page + 0x80;
    if (!strcmp(name, "word-boundary-next-none")) addr = memory + page * 2 - 2;
    if (!strcmp(name, "word-two-aligned")) addr += 2;
    if (!strcmp(name, "orphan-readable")) *(uint32_t *)(addr - 2) = 0x5a5a5a5b;
    if (!strcmp(name, "orphan-live-stamp")) *(uint32_t *)(addr - 2) = 0x00010007;
    register_waiters(addr, hot || orphan || !strcmp(name, "protect-race") ? 3 : 1,
                     word || !strcmp(name, "protect-race"));
    if (strstr(name, "prot-none"))
        require(mprotect(memory, size, PROT_NONE) == 0, "real mapping made unreadable");
    if (strstr(name, "boundary-next-none"))
        require(mprotect(memory + page * 2, page, PROT_NONE) == 0, "next page is unreadable");
    if (strstr(name, "boundary-prev-none"))
        require(mprotect(memory, page, PROT_NONE) == 0, "previous page is unreadable");
    if (strstr(name, "unmapped"))
        require(munmap(memory, size) == 0, "wait cookie now points to a real unmapped range");

    if (orphan)
    {
        unsigned long long stamp = (unsigned long long)(uintptr_t)(addr - 2);
        int i;
        for (i = 0; i < 4; i++)
            ios_orphan_check(!strcmp(name, "orphan-live-stamp") ? &stamp : NULL,
                             !strcmp(name, "orphan-live-stamp") ? 1 : 0);
        require(reaped == 0, "inaccessible/incoherent/live-stamped lock must not be reaped");
    }
    else if (!strcmp(name, "protect-race"))
    {
        struct race_state state = { .page = memory + page, .size = page };
        pthread_t thread;
        int i;
        require(pthread_create(&thread, NULL, protect_race, &state) == 0, "start real protection race");
        while (!atomic_load(&state.started)) sched_yield();
        /* Use /dev/null for repeated diagnostic output; each deterministic
         * scenario separately checks readable and unreadable output contents. */
        require(freopen("/dev/null", "w", stderr) != NULL, "bound race logging");
        for (i = 0; i < 200; i++)
        {
            ios_alert_waiter_dump();
            ios_orphan_check(NULL, 0);
        }
        atomic_store(&state.stop, 1);
        require(pthread_join(thread, NULL) == 0, "join protection-race thread");
        require(atomic_load(&state.changes) > 0, "race changed actual protection");
        require(reaped == 0, "race must not invent a held SRW");
    }
    else ios_alert_waiter_dump();
    if (!strstr(name, "unmapped")) require(munmap(memory, size) == 0, "release only harness mapping");
    puts("CASE_OK");
    return 0;
}
"""


SCENARIOS = (
    "hot-readable", "hot-prot-none", "hot-unmapped",
    "hot-boundary-next-none", "hot-boundary-prev-none",
    "word-readable", "word-two-aligned", "word-prot-none", "word-unmapped",
    "word-boundary-next-none", "orphan-readable", "orphan-live-stamp",
    "orphan-prot-none", "orphan-unmapped", "protect-race",
    "logger-valid-teb", "logger-null-teb", "logger-readable-strings",
)


def output_failure(name: str, stderr: str) -> str | None:
    if name.startswith("hot-") and "[hot-lock] addr=" not in stderr:
        return "three real registry waiters did not enter hot-lock diagnostic"
    if name in {"hot-readable", "hot-boundary-next-none", "hot-boundary-prev-none"}:
        if "5a5a5a5a5a5a5a5a" not in stderr:
            return "readable hot-lock rows did not retain actual mapped bytes"
    if name in {"hot-prot-none", "hot-unmapped"} and "5a5a5a5a5a5a5a5a" in stderr:
        return "unreadable memory was reported as readable data"
    if name.startswith("word-"):
        if "over60s=1" not in stderr or "age=65s INF" not in stderr:
            return "real >60-second registry waiter was not dumped"
        want = "w0=deaddead w1=deaddead" if name in {"word-prot-none", "word-unmapped"} else (
            "w0=5a5a5a5a w1=deaddead" if name == "word-boundary-next-none" else
            "w0=5a5a5a5a w1=5a5a5a5a"
        )
        if want not in stderr:
            return "lock-word diagnostic did not preserve real bytes or unreadable sentinel: " + want
    if name == "orphan-readable" and "word=5a5a5a5b" not in stderr:
        return "incoherent-lock control did not inspect the actual mapped word"
    if name.startswith("logger-"):
        want_tid = "tid=0000" if name == "logger-null-teb" else "tid=abcd"
        if want_tid not in stderr or "code=c0000005" not in stderr or "p0=44 p1=55" not in stderr:
            return "guest-exception logger lost code/params or null-safe/valid TEB thread ID"
        if name == "logger-readable-strings" and (
            'OutputDebugStringA: "readable ANSI control"' not in stderr or
            'OutputDebugStringW: "readable WIDE"' not in stderr
        ):
            return "valid guest debug-string controls no longer log their real text"
    return None


def run_bounded(command: list[str], env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    """Capture bounded tails from files, rather than accumulating crash output."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, env=env, timeout=timeout, check=False)
        tails = []
        for stream in (stdout, stderr):
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tails.append(stream.read().decode("utf-8", errors="replace"))
        return result.returncode, tails[0], tails[1]


def main() -> int:
    if platform.system() != "Darwin":
        print("FAIL: requires Darwin; real Mach VM APIs are part of this regression test")
        return 1
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    compiler = shutil.which("clang")
    if not compiler:
        print("FAIL: clang is required")
        return 1
    code = PRELUDE + production_code() + DRIVER
    failures = 0
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = "detect_leaks=0:abort_on_error=1:disable_coredump=1"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    with tempfile.TemporaryDirectory(prefix="madeira-alert-waiter-") as temporary:
        source = Path(temporary) / "probes.c"
        source.write_text(code)
        for mode, flags in (
            ("O2", ["-O2"]),
            ("ASan-UBSan", ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]),
        ):
            binary = Path(temporary) / mode
            command = [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", *flags,
                       str(source), "-o", str(binary)]
            try:
                status, _, errors = run_bounded(command, env, 30)
            except subprocess.TimeoutExpired:
                print(f"FAIL: {mode} compiler timed out")
                failures += 1
                continue
            if status:
                print(f"FAIL: {mode} real production functions compile: {errors[-3000:]}")
                failures += 1
                continue
            print(f"PASS: {mode} compiled actual production probes with real Mach/Darwin APIs")
            for name in SCENARIOS:
                try:
                    status, stdout, stderr = run_bounded([str(binary), name], env, 15)
                except subprocess.TimeoutExpired:
                    print(f"FAIL: {mode} {name}: timed out")
                    failures += 1
                    continue
                reason = f"child exit {status}" if status else (
                    "child completion marker missing" if "CASE_OK" not in stdout else output_failure(name, stderr)
                )
                if reason:
                    failures += 1
                    print(f"FAIL: {mode} {name}: {reason}")
                    print(stderr[-1800:])
                else:
                    print(f"PASS: {mode} {name}")
    print(f"{'FAIL' if failures else 'PASS'}: {len(SCENARIOS) * 2} diagnostic-probe scenarios; {failures} failures")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
