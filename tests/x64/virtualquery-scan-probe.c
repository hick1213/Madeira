/* Generic SEC_RESERVE / cross-process VirtualQuery progress regression.
 * The child asks about the parent's numeric address in its OWN namespace.
 * No mapped bytes, files, registry values or application data are accessed.
 */
#define _WIN32_WINNT 0x0600
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <errno.h>
#include <string.h>

#define SECTION_BYTES 0x20000u
#define COMMIT_BYTES 0x4000u
#define WAIT_MS 12000u

static void report(const char *stage, uintptr_t addr, SIZE_T size, DWORD error)
{
    char line[224];
    snprintf(line, sizeof line, "VQUERY-SCAN %s address=0x%llx size=0x%llx WinError=%lu\n",
             stage, (unsigned long long)addr, (unsigned long long)size, (unsigned long)error);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
}

static int fail(const char *stage, uintptr_t addr, DWORD error)
{
    report(stage, addr, 0, error);
    return 1;
}

static DWORD WINAPI watchdog(void *done)
{
    if (WaitForSingleObject(done, 15000) != WAIT_OBJECT_0) {
        /* Avoid CRT locks if another thread stopped inside logging. */
        OutputDebugStringA("VQUERY-SCAN FAIL watchdog exit=124\n");
        ExitProcess(124);
    }
    return 0;
}

static BOOL query_progress(uintptr_t addr, MEMORY_BASIC_INFORMATION *info, uintptr_t *next)
{
    SIZE_T result = VirtualQuery((void *)addr, info, sizeof(*info));
    uintptr_t base;
    if (result != sizeof(*info)) {
        fail("FAIL query", addr, GetLastError());
        return FALSE;
    }
    base = (uintptr_t)info->BaseAddress;
    if (!info->RegionSize || base > addr || info->RegionSize > UINTPTR_MAX - base ||
        (*next = base + info->RegionSize) <= addr) {
        fail("FAIL non-progress", addr, ERROR_INVALID_DATA);
        return FALSE;
    }
    return TRUE;
}

static int child_scan(uintptr_t base, SIZE_T size)
{
    uintptr_t end, current = base;
    unsigned steps = 0;
    if (!size || size > SECTION_BYTES || base > UINTPTR_MAX - size)
        return fail("FAIL child arguments", base, ERROR_INVALID_PARAMETER);
    end = base + size;
    report("START child own-namespace scan", base, size, 0);
    while (current < end && steps < SECTION_BYTES / 0x1000u + 1u) {
        MEMORY_BASIC_INFORMATION info;
        uintptr_t next;
        if (!query_progress(current, &info, &next)) return 1;
        /* Windows can map something unrelated here in the child. The contract
         * tested is successful positive progress, not parent's visibility. */
        report("child positive region", current, info.RegionSize, 0);
        current = next;
        ++steps;
    }
    if (current < end) return fail("FAIL child step limit", current, ERROR_TIMEOUT);
    report("PASS child", base, size, 0);
    return 0;
}

static int parent_scan(void)
{
    HANDLE section = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE | SEC_RESERVE,
                                        0, SECTION_BYTES, NULL);
    unsigned char *view;
    MEMORY_BASIC_INFORMATION info;
    uintptr_t next;
    char executable[1024], command[1200];
    STARTUPINFOA startup = {0};
    PROCESS_INFORMATION child = {0};
    DWORD length, waited, code;
    int result = 1;
    if (!section) return fail("FAIL create section", 0, GetLastError());
    view = MapViewOfFile(section, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, SECTION_BYTES);
    if (!view) {
        DWORD error = GetLastError();
        CloseHandle(section);
        return fail("FAIL map parent", 0, error);
    }
    if (VirtualAlloc(view, COMMIT_BYTES, MEM_COMMIT, PAGE_READWRITE) != view) {
        fail("FAIL commit parent", (uintptr_t)view, GetLastError());
        goto cleanup;
    }
    if (!query_progress((uintptr_t)view, &info, &next)) goto cleanup;
    if (info.State != MEM_COMMIT || info.Type != MEM_MAPPED ||
        info.Protect != PAGE_READWRITE || next != (uintptr_t)view + COMMIT_BYTES) {
        fail("FAIL parent commit boundary", (uintptr_t)view, ERROR_INVALID_DATA);
        goto cleanup;
    }
    if (!query_progress((uintptr_t)view + COMMIT_BYTES, &info, &next)) goto cleanup;
    if (info.State != MEM_RESERVE || info.Type != MEM_MAPPED ||
        next != (uintptr_t)view + SECTION_BYTES) {
        fail("FAIL parent reserve boundary", (uintptr_t)view + COMMIT_BYTES, ERROR_INVALID_DATA);
        goto cleanup;
    }
    report("PASS parent boundaries", (uintptr_t)view, SECTION_BYTES, 0);
    length = GetModuleFileNameA(NULL, executable, sizeof executable);
    if (!length || length >= sizeof executable) {
        fail("FAIL executable path", 0, GetLastError());
        goto cleanup;
    }
    snprintf(command, sizeof command, "\"%s\" --child 0x%llx 0x%x",
             executable, (unsigned long long)(uintptr_t)view, SECTION_BYTES);
    startup.cb = sizeof startup;
    if (!CreateProcessA(executable, command, NULL, NULL, FALSE, 0, NULL, NULL, &startup, &child)) {
        fail("FAIL child creation", (uintptr_t)view, GetLastError());
        goto cleanup;
    }
    waited = WaitForSingleObject(child.hProcess, WAIT_MS);
    if (waited != WAIT_OBJECT_0) {
        DWORD error = waited == WAIT_FAILED ? GetLastError() : ERROR_TIMEOUT;
        report("FAIL child wait", (uintptr_t)view, SECTION_BYTES, error);
        TerminateProcess(child.hProcess, 124);
        /* Never explicitly unmap the parent's section until its child exits. */
        if (WaitForSingleObject(child.hProcess, 2000) != WAIT_OBJECT_0) ExitProcess(1);
        goto child_cleanup;
    }
    if (!GetExitCodeProcess(child.hProcess, &code)) {
        fail("FAIL child status", (uintptr_t)view, GetLastError());
        goto child_cleanup;
    }
    if (code) {
        report("FAIL child nonzero", (uintptr_t)view, SECTION_BYTES, code);
        goto child_cleanup;
    }
    result = 0;
    report("PASS parent child joined", (uintptr_t)view, SECTION_BYTES, 0);
child_cleanup:
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
cleanup:
    if (!UnmapViewOfFile(view)) result = fail("FAIL unmap parent", (uintptr_t)view, GetLastError());
    if (!CloseHandle(section)) result = fail("FAIL close section", 0, GetLastError());
    return result;
}

int main(int argc, char **argv)
{
    HANDLE done = CreateEventA(NULL, TRUE, FALSE, NULL), timer;
    int result;
    if (!done) return fail("FAIL watchdog event", 0, GetLastError());
    timer = CreateThread(NULL, 0, watchdog, done, 0, NULL);
    if (!timer) {
        DWORD error = GetLastError();
        CloseHandle(done);
        return fail("FAIL watchdog thread", 0, error);
    }
    if (argc == 4 && !strcmp(argv[1], "--child")) {
        char *a, *b;
        unsigned long long base, size;
        errno = 0;
        base = strtoull(argv[2], &a, 0);
        size = strtoull(argv[3], &b, 0);
        if (errno || !*argv[2] || !*argv[3] || *a || *b || base > UINTPTR_MAX || size > SIZE_MAX)
            result = fail("FAIL parse child", 0, ERROR_INVALID_PARAMETER);
        else result = child_scan((uintptr_t)base, (SIZE_T)size);
    } else if (argc == 1) result = parent_scan();
    else result = fail("FAIL arguments", 0, ERROR_INVALID_PARAMETER);
    if (!SetEvent(done) || WaitForSingleObject(timer, 2000) != WAIT_OBJECT_0) ExitProcess(1);
    CloseHandle(timer);
    CloseHandle(done);
    if (!result) report("PASS terminal", 0, 0, 0);
    return result;
}
