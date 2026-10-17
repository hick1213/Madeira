/* Windows-only public-API probe with all remaining low 4 GiB holes reserved.
 * Existing loader/Wine mappings remain intact. This does not emulate PAGEZERO
 * or FEX; it tests that post-startup Lua allocation needs no free low address.
 */
#ifndef _WIN32
#error This fixture requires Windows VirtualQuery and VirtualAlloc.
#endif
#define main lua_probe_main
#include "probe.c"
#undef main

#define LOW_LIMIT UINT64_C(0x100000000)
#define MAX_RESERVATIONS 4096

static void *reservations[MAX_RESERVATIONS];
static size_t reservation_count;

static int reserve_low_holes(void)
{
  SYSTEM_INFO info;
  uintptr_t cursor;
  uint64_t reserved = 0;
  GetSystemInfo(&info);
  cursor = (uintptr_t)info.lpMinimumApplicationAddress;
  while (cursor < LOW_LIMIT) {
    MEMORY_BASIC_INFORMATION region;
    uintptr_t end, start;
    size_t bytes;
    void *allocated;
    if (!VirtualQuery((void *)cursor, &region, sizeof(region))) {
      fprintf(stderr, "LOW_GUARD_FAIL query=%p error=%lu\n",
              (void *)cursor, (unsigned long)GetLastError());
      return 1;
    }
    end = (uintptr_t)region.BaseAddress + region.RegionSize;
    if (end <= cursor) return 1;
    if (end > LOW_LIMIT) end = LOW_LIMIT;
    if (region.State == MEM_FREE) {
      uintptr_t granularity = info.dwAllocationGranularity;
      start = (cursor + granularity - 1) & ~(granularity - 1);
      if (start < end) {
        if (reservation_count == MAX_RESERVATIONS) return 1;
        bytes = (size_t)(end - start);
        allocated = VirtualAlloc((void *)start, bytes, MEM_RESERVE, PAGE_NOACCESS);
        if (allocated != (void *)start) {
          if (allocated) VirtualFree(allocated, 0, MEM_RELEASE);
          fprintf(stderr, "LOW_GUARD_FAIL reserve=%p bytes=%zu error=%lu\n",
                  (void *)start, bytes, (unsigned long)GetLastError());
          return 1;
        }
        reservations[reservation_count++] = allocated;
        reserved += bytes;
      }
    }
    cursor = end;
  }
  /* Do not report a vacuous guard as proof on an unexpected runtime. */
  if (!reservation_count || reserved < UINT64_C(0x40000000)) return 1;
  printf("LOW_GUARD_READY reservations=%zu bytes=%llu limit=0x100000000\n",
         reservation_count, (unsigned long long)reserved);
  return 0;
}

int main(int argc, char **argv)
{
  int result;
  setvbuf(stdout, NULL, _IONBF, 0);
  result = reserve_low_holes();
  if (!result) result = lua_probe_main(argc, argv);
  while (reservation_count) {
    if (!VirtualFree(reservations[--reservation_count], 0, MEM_RELEASE)) result = 1;
  }
  if (!result) puts("LOW_GUARD_PASS");
  return result;
}
