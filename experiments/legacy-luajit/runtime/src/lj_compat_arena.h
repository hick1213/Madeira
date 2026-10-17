/* Experimental high-address reference arena. See prototype design/provenance. */
#ifndef _LJ_COMPAT_ARENA_H
#define _LJ_COMPAT_ARENA_H
#include <stddef.h>
#include <stdint.h>

extern uintptr_t lj_compat_arena_base;
uint32_t lj_compat_ref_encode(const void *p);
void *lj_compat_ref_decode(uint32_t ref);
int lj_compat_contains(const void *p, size_t size);
/* Sizes round up to 64KiB; unmap addresses must be 64KiB aligned.
** map returns (void *)-1 on failure; unmap returns 0 or -1. */
void *lj_compat_arena_map(size_t size);
int lj_compat_arena_unmap(void *p, size_t size);
#endif
