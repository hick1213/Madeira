/* Generic x64 host diagnostic: never executes the copied instruction bytes.
 * Build with the pinned x86_64-w64-mingw32-clang, -O2 -fno-builtin-memcpy.
 * Only terminal ASCII diagnostics use OutputDebugStringA; no files or game data.
 */
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <emmintrin.h>

#define COPY_SIZE 256
#define REGION_SIZE 0x10000

typedef void *(__cdecl *memcpy_fn)(void *, const void *, size_t);
static volatile LONG finished;
static unsigned int cases;

__attribute__((naked, noinline, used, aligned(16)))
static void text_copy_slot(void)
{
    __asm__ volatile(".rept 256\n.byte 0x90\n.endr\nret\n");
}

static char *append(char *out, const char *text)
{
    while (*text) *out++ = *text++;
    return out;
}

static char *hex(char *out, uintptr_t value)
{
    static const char digits[] = "0123456789abcdef";
    unsigned int n = sizeof(value) * 2;
    out = append(out, "0x");
    while (n) { --n; *out++ = digits[(value >> (n * 4)) & 15]; }
    return out;
}

static int failure(const char *reason, const char *target, const char *method,
                   unsigned int phase, size_t offset, size_t index,
                   unsigned int expected, unsigned int actual)
{
    char message[512], *p = message;
    p = append(p, "RWX-COPY FAIL "); p = append(p, reason);
    p = append(p, " target="); p = append(p, target);
    p = append(p, " method="); p = append(p, method);
    p = append(p, " phase="); p = hex(p, phase);
    p = append(p, " offset="); p = hex(p, offset);
    p = append(p, " index="); p = hex(p, index);
    p = append(p, " expected="); p = hex(p, expected);
    p = append(p, " actual="); p = hex(p, actual);
    *p = 0;
    InterlockedExchange(&finished, 1);
    OutputDebugStringA(message);
    return 1;
}

static DWORD WINAPI watchdog(void *unused)
{
    (void)unused;
    Sleep(15000);
    if (!InterlockedCompareExchange(&finished, 0, 0))
    {
        OutputDebugStringA("RWX-COPY FAIL timeout=15000ms");
        ExitProcess(90);
    }
    return 0;
}

__attribute__((noinline))
static void byte_copy(volatile unsigned char *destination, const unsigned char *source)
{
    for (size_t i = 0; i < COPY_SIZE; ++i) destination[i] = source[i];
}

__attribute__((noinline))
static void sse_copy(unsigned char *destination, const unsigned char *source)
{
    for (size_t i = 0; i < COPY_SIZE; i += 16)
        _mm_storeu_si128((__m128i *)(destination + i),
                        _mm_loadu_si128((const __m128i *)(source + i)));
}

static int copy_check(unsigned char *destination, const unsigned char *source,
                      memcpy_fn native_copy, const char *target,
                      unsigned int phase, size_t offset, unsigned int method)
{
    static const char *names[] = { "ucrt-memcpy", "volatile-byte", "sse16" };
    volatile unsigned char *observed = destination;
    /* Establish a different prior value, and verify it before the copy. */
    for (size_t i = 0; i < COPY_SIZE; ++i) observed[i] = source[i] ^ 0xff;
    for (size_t i = 0; i < COPY_SIZE; ++i)
        if (observed[i] != (unsigned char)(source[i] ^ 0xff))
            return failure("reset", target, names[method], phase, offset, i,
                           source[i] ^ 0xff, observed[i]);
    if (method == 0) native_copy(destination, source, COPY_SIZE);
    else if (method == 1) byte_copy(destination, source);
    else sse_copy(destination, source);
    for (size_t i = 0; i < COPY_SIZE; ++i)
        if (observed[i] != source[i])
            return failure("compare", target, names[method], phase, offset, i,
                           source[i], observed[i]);
    ++cases;
    return 0;
}

/* A few ASCII mapping snapshots establish which path the dual-view test took. */
static int section_mapping_note(const char *stage, unsigned int phase, size_t offset,
                                const void *address, const void *other)
{
    MEMORY_BASIC_INFORMATION info;
    char message[512], *p = message;
    if (!VirtualQuery(address, &info, sizeof(info)))
        return failure("query", "section", stage, phase, offset, 0, 0, GetLastError());
    p = append(p, "RWX-COPY SECTION "); p = append(p, stage);
    p = append(p, " phase="); p = hex(p, phase);
    p = append(p, " offset="); p = hex(p, offset);
    p = append(p, " address="); p = hex(p, (uintptr_t)address);
    p = append(p, " other="); p = hex(p, (uintptr_t)other);
    p = append(p, " allocation="); p = hex(p, (uintptr_t)info.AllocationBase);
    p = append(p, " size="); p = hex(p, info.RegionSize);
    p = append(p, " state="); p = hex(p, info.State);
    p = append(p, " protect="); p = hex(p, info.Protect);
    p = append(p, " type="); p = hex(p, info.Type);
    *p = 0;
    OutputDebugStringA(message);
    return 0;
}

/* SEC_RESERVE dual views: commit executable pages through RX, then write the
 * same object through a separate RW view. No copied bytes are executed. */
static int section_copy_check(const unsigned char *source, memcpy_fn native_copy)
{
    static const size_t offsets[] = { 0, 1, 15, 0x3ff0, 0x3ff8, 0x3fff };
    static const DWORD section_offsets[] = { 0, REGION_SIZE };
    static const char *names[] = { "ucrt-memcpy", "volatile-byte", "sse16" };
    HANDLE section = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL,
                                       PAGE_EXECUTE_READWRITE | SEC_RESERVE,
                                       0, REGION_SIZE * 2, NULL);
    if (!section) return failure("create-section", "section-rx", "none", 0, 0, 0, 0, GetLastError());
    for (unsigned int phase = 0; phase < 2; ++phase)
    {
        DWORD section_offset = section_offsets[phase];
        unsigned char *rx = MapViewOfFile(section, FILE_MAP_EXECUTE | FILE_MAP_READ | FILE_MAP_WRITE,
                                           0, section_offset, REGION_SIZE);
        unsigned char *rw;
        if (!rx)
        {
            DWORD error = GetLastError();
            CloseHandle(section);
            return failure("map-rx", "section-rx", "none", phase, section_offset, 0, 0, error);
        }
        if (section_mapping_note("before-commit", phase, section_offset, rx, NULL))
        {
            UnmapViewOfFile(rx); CloseHandle(section);
            return 1;
        }
        if (VirtualAlloc(rx, REGION_SIZE, MEM_COMMIT, PAGE_EXECUTE_READ) != rx)
        {
            DWORD error = GetLastError();
            UnmapViewOfFile(rx); CloseHandle(section);
            return failure("commit-rx", "section-rx", "none", phase, section_offset, 0, 0, error);
        }
        if (section_mapping_note("after-commit", phase, section_offset, rx, NULL))
        {
            UnmapViewOfFile(rx); CloseHandle(section);
            return 1;
        }
        rw = MapViewOfFile(section, FILE_MAP_READ | FILE_MAP_WRITE, 0, section_offset, REGION_SIZE);
        if (!rw)
        {
            DWORD error = GetLastError();
            UnmapViewOfFile(rx); CloseHandle(section);
            return failure("map-rw", "section-rx", "none", phase, section_offset, 0, 0, error);
        }
        if (section_mapping_note("rw-map", phase, section_offset, rw, rx))
        {
            UnmapViewOfFile(rw); UnmapViewOfFile(rx); CloseHandle(section);
            return 1;
        }
        for (size_t j = 0; j < sizeof(offsets) / sizeof(offsets[0]); ++j)
            for (unsigned int method = 0; method < 3; ++method)
            {
                volatile const unsigned char *observed = rx + offsets[j];
                volatile unsigned char *writer = rw + offsets[j];
                /* Each case proves a NEW write reached RX. A stale previous
                 * source pattern must not let a later broken copy pass. */
                for (size_t i = 0; i < COPY_SIZE; ++i) writer[i] = source[i] ^ 0xff;
                for (size_t i = 0; i < COPY_SIZE; ++i)
                {
                    unsigned char actual = observed[i];
                    unsigned char expected = source[i] ^ 0xff;
                    if (actual != expected)
                    {
                        UnmapViewOfFile(rw); UnmapViewOfFile(rx); CloseHandle(section);
                        return failure("dual-reset-compare", "section-rx", names[method], phase,
                                       section_offset + offsets[j], i, expected, actual);
                    }
                }
                if (copy_check(rw + offsets[j], source, native_copy, "section-rw", phase,
                               section_offset + offsets[j], method))
                {
                    UnmapViewOfFile(rw); UnmapViewOfFile(rx); CloseHandle(section);
                    return 1;
                }
                for (size_t i = 0; i < COPY_SIZE; ++i)
                {
                    unsigned char actual = observed[i];
                    if (actual != source[i])
                    {
                        UnmapViewOfFile(rw); UnmapViewOfFile(rx); CloseHandle(section);
                        return failure("dual-compare", "section-rx", names[method], phase,
                                       section_offset + offsets[j], i, source[i], actual);
                    }
                }
            }
        {
            BOOL rw_unmapped = UnmapViewOfFile(rw);
            DWORD error = rw_unmapped ? 0 : GetLastError();
            BOOL rx_unmapped = UnmapViewOfFile(rx);
            if (!rw_unmapped || !rx_unmapped)
            {
                if (!error) error = GetLastError();
                CloseHandle(section);
                return failure("unmap", "section-rx", "none", phase, section_offset, 0, 0, error);
            }
        }
    }
    if (!CloseHandle(section))
        return failure("close-section", "section-rx", "none", 0, 0, 0, 0, GetLastError());
    return 0;
}

int main(void)
{
    static const size_t offsets[] = { 0, 1, 15, 0x3ff0, 0x3ff8, 0x3fff };
    static const unsigned char instructions[] = {
        0xf0, 0x48, 0x0f, 0xb1, 0x11, 0x48, 0x89, 0xc1,
        0x48, 0x8b, 0x97, 0x18, 0x02, 0x00, 0x00, 0xc3
    };
    unsigned char source[COPY_SIZE];
    unsigned char *region, *text = (unsigned char *)(uintptr_t)text_copy_slot;
    DWORD old_protect;
    HMODULE ucrt = LoadLibraryA("ucrtbase.dll");
    memcpy_fn native_copy;
    HANDLE timer;

    if (!ucrt || !(native_copy = (memcpy_fn)(uintptr_t)GetProcAddress(ucrt, "memcpy")))
        return failure("resolve", "none", "ucrt-memcpy", 0, 0, 0, 0, GetLastError());
    timer = CreateThread(NULL, 0, watchdog, NULL, 0, NULL);
    if (!timer) return failure("watchdog", "none", "none", 0, 0, 0, 0, GetLastError());
    CloseHandle(timer);
    for (size_t i = 0; i < COPY_SIZE; ++i) source[i] = instructions[i % sizeof(instructions)];
    source[63] = source[127] = source[255] = 0xf0;
    /* Run sections before any anonymous RWX allocation. Reusing a released
     * anonymous VA can encounter a stale alias and bypass the section path. */
    if (section_copy_check(source, native_copy)) return 1;
    region = VirtualAlloc(NULL, REGION_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    if (!region) return failure("allocate", "anon", "none", 0, 0, 0, 0, GetLastError());

    for (unsigned int phase = 0; phase < 2; ++phase)
    {
        if (phase && (!VirtualProtect(region, REGION_SIZE, PAGE_EXECUTE_READ, &old_protect) ||
                      !VirtualProtect(region, REGION_SIZE, PAGE_EXECUTE_READWRITE, &old_protect)))
            return failure("protect", "anon", "none", phase, 0, 0, 0, GetLastError());
        for (size_t j = 0; j < sizeof(offsets) / sizeof(offsets[0]); ++j)
            for (unsigned int method = 0; method < 3; ++method)
                if (copy_check(region + offsets[j], source, native_copy, "anon", phase, offsets[j], method)) return 1;
    }
    if (!VirtualFree(region, 0, MEM_RELEASE))
        return failure("free", "anon", "none", 0, 0, 0, 0, GetLastError());

    if (!VirtualProtect(text, COPY_SIZE, PAGE_EXECUTE_READWRITE, &old_protect))
        return failure("protect", "pe-text", "none", 0, 0, 0, 0, GetLastError());
    for (unsigned int phase = 0; phase < 2; ++phase)
    {
        if (phase && (!VirtualProtect(text, COPY_SIZE, PAGE_EXECUTE_READ, &old_protect) ||
                      !VirtualProtect(text, COPY_SIZE, PAGE_EXECUTE_READWRITE, &old_protect)))
            return failure("protect", "pe-text", "none", phase, 0, 0, 0, GetLastError());
        for (unsigned int method = 0; method < 3; ++method)
            if (copy_check(text, source, native_copy, "pe-text", phase, 0, method)) return 1;
    }
    if (!VirtualProtect(text, COPY_SIZE, PAGE_EXECUTE_READ, &old_protect))
        return failure("restore", "pe-text", "none", 0, 0, 0, 0, GetLastError());
    {
        char message[96], *p = append(message, "RWX-COPY PASS cases=");
        p = hex(p, cases); *p = 0;
        InterlockedExchange(&finished, 1);
        OutputDebugStringA(message);
    }
    return 0;
}
