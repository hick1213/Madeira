/* Generic x64 Win32 INIT_ONCE callback / contention diagnostic.
 * Build with the pinned llvm-mingw x86_64 compiler, -O2 -static -lkernel32.
 * No files, registry, Steam, .NET or graphics dependencies are used.
 */
#define _WIN32_WINNT 0x0600
#include <windows.h>
#include <stdio.h>
#include <stdint.h>

#define WORKERS 8
#define ROUNDS 32
#define WAIT_MS 5000

struct round {
    INIT_ONCE once;
    HANDLE start;
    HANDLE ready;
    volatile LONG ready_count;
    volatile LONG entering;
    volatile LONG calls;
    LONG expected;
    _Alignas(16) LONG token;
};

struct worker {
    struct round *round;
    BOOL ok;
    DWORD error;
    void *context;
};

static void report(const char *stage, unsigned round, DWORD error)
{
    char line[192];
    snprintf(line, sizeof line, "[runonce-stress] %s round=%u WinError=%lu\n",
             stage, round, (unsigned long)error);
    fputs(line, stdout);
    fflush(stdout);
    OutputDebugStringA(line);
}

static int fail(const char *stage, unsigned round, DWORD error)
{
    report(stage, round, error);
    /* A waiter may still refer to the round. Never reset/free it on failure. */
    ExitProcess(1);
    return 1;
}

static BOOL CALLBACK initialize(PINIT_ONCE once, PVOID parameter, PVOID *context)
{
    struct round *r = parameter;
    ULONGLONG deadline = GetTickCount64() + WAIT_MS;
    if (once != &r->once || !context) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    InterlockedIncrement(&r->calls);
    while (InterlockedCompareExchange(&r->entering, 0, 0) < r->expected) {
        if (GetTickCount64() >= deadline) {
            SetLastError(ERROR_TIMEOUT);
            return FALSE;
        }
        Sleep(1);
    }
    if (r->expected > 1) Sleep(50); /* Keep the initializer active for waiters. */
    r->token = 0x12345678;
    *context = &r->token; /* DWORD-aligned; INIT_ONCE reserved low bits stay zero. */
    return TRUE;
}

static DWORD WINAPI run_worker(void *parameter)
{
    struct worker *w = parameter;
    struct round *r = w->round;
    DWORD result;
    if (InterlockedIncrement(&r->ready_count) == WORKERS && !SetEvent(r->ready))
        return GetLastError();
    result = WaitForSingleObject(r->start, WAIT_MS);
    if (result != WAIT_OBJECT_0)
        return result == WAIT_FAILED ? GetLastError() : ERROR_TIMEOUT;
    InterlockedIncrement(&r->entering);
    w->ok = InitOnceExecuteOnce(&r->once, initialize, r, &w->context);
    w->error = w->ok ? ERROR_SUCCESS : GetLastError();
    return w->error;
}

static DWORD WINAPI watchdog(void *parameter)
{
    DWORD result = WaitForSingleObject(parameter, 60000);
    if (result != WAIT_OBJECT_0) {
        /* Avoid a CRT stdout lock held by the thread whose progress stopped. */
        OutputDebugStringA("[runonce-stress] FAIL watchdog: wait did not complete; exit=124\n");
        ExitProcess(124);
    }
    return 0;
}

int main(void)
{
    struct round single = {0};
    void *context = NULL;
    LARGE_INTEGER counter;
    HANDLE done = CreateEventA(NULL, TRUE, FALSE, NULL);
    HANDLE guard;
    if (!done) return fail("FAIL watchdog event", 0, GetLastError());
    guard = CreateThread(NULL, 0, watchdog, done, 0, NULL);
    if (!guard) return fail("FAIL watchdog thread", 0, GetLastError());
    report("START native imports / single callback", 0, 0);
    if (!GetCurrentProcessId() || !QueryPerformanceCounter(&counter))
        return fail("FAIL native imports", 0, GetLastError());
    single.expected = 1;
    single.entering = 1;
    InitOnceInitialize(&single.once);
    for (unsigned i = 0; i < 2; i++) {
        if (!InitOnceExecuteOnce(&single.once, initialize, &single, &context))
            return fail("FAIL single callback", 0, GetLastError());
        if (context != &single.token || single.token != 0x12345678 || single.calls != 1)
            return fail("FAIL single result", 0, ERROR_INVALID_DATA);
    }
    report("PASS single callback", 0, 0);

    for (unsigned n = 1; n <= ROUNDS; n++) {
        struct round r = {0};
        struct worker workers[WORKERS] = {0};
        HANDLE threads[WORKERS];
        DWORD result;
        r.expected = WORKERS;
        if ((uintptr_t)&r.token & ((1u << INIT_ONCE_CTX_RESERVED_BITS) - 1u))
            return fail("FAIL context alignment", n, ERROR_INVALID_DATA);
        InitOnceInitialize(&r.once);
        r.start = CreateEventA(NULL, TRUE, FALSE, NULL);
        if (!r.start) return fail("FAIL start event creation", n, GetLastError());
        r.ready = CreateEventA(NULL, TRUE, FALSE, NULL);
        if (!r.ready) return fail("FAIL ready event creation", n, GetLastError());
        report("START contention", n, 0);
        for (unsigned i = 0; i < WORKERS; i++) {
            workers[i].round = &r;
            threads[i] = CreateThread(NULL, 0, run_worker, &workers[i], 0, NULL);
            if (!threads[i]) return fail("FAIL worker creation", n, GetLastError());
        }
        result = WaitForSingleObject(r.ready, WAIT_MS);
        if (result != WAIT_OBJECT_0)
            return fail("FAIL worker readiness", n, result == WAIT_FAILED ? GetLastError() : ERROR_TIMEOUT);
        if (!SetEvent(r.start)) return fail("FAIL start event", n, GetLastError());
        result = WaitForMultipleObjects(WORKERS, threads, TRUE, WAIT_MS);
        if (result != WAIT_OBJECT_0)
            return fail("FAIL waiter completion", n, result == WAIT_FAILED ? GetLastError() : ERROR_TIMEOUT);
        for (unsigned i = 0; i < WORKERS; i++) {
            DWORD code;
            if (!GetExitCodeThread(threads[i], &code))
                return fail("FAIL worker status", n, GetLastError());
            if (code || !workers[i].ok)
                return fail("FAIL worker initialization", n, code ? code : workers[i].error);
            if (workers[i].context != &r.token || r.token != 0x12345678)
                return fail("FAIL worker context", n, ERROR_INVALID_DATA);
            CloseHandle(threads[i]);
        }
        if (r.calls != 1) return fail("FAIL callback count", n, ERROR_INVALID_DATA);
        CloseHandle(r.start);
        CloseHandle(r.ready);
        /* Every worker is joined before this INIT_ONCE stack slot is reused. */
        report("PASS contention", n, 0);
    }
    if (!SetEvent(done)) return fail("FAIL watchdog stop", ROUNDS, GetLastError());
    if (WaitForSingleObject(guard, WAIT_MS) != WAIT_OBJECT_0)
        return fail("FAIL watchdog join", ROUNDS, ERROR_TIMEOUT);
    CloseHandle(guard);
    CloseHandle(done);
    report("PASS all: 32 rounds / 8 workers / single callback", ROUNDS, 0);
    return 0;
}
