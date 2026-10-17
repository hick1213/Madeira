/* Actual Darwin VM experiment, not a model of Wine/FEX memory accounting.
 * Build: xcrun clang -std=c11 -O2 -Wall -Wextra -Werror this-file.c -o canary
 * Run all isolated modes: ./canary; one mode: ./canary --mode MODE
 * Measurements never load the target mappings. Complete readback follows the
 * last pre-validation sample. There is no JIT mapping or memory-pressure loop.
 */
#include <errno.h>
#include <inttypes.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MIB ((size_t)1024 * 1024)
#define COMMIT_BYTES (256 * MIB)
#define POISON_BYTES (128 * MIB)
#define REPLACE_OFFSET MIB
#define REPLACE_BYTES (POISON_BYTES - 2 * MIB)
#define LARGE_RESERVE_BYTES ((size_t)32 * 1024 * MIB)
#define MODE_LIMIT_SECONDS 15

enum read_policy { READ_NONE, READ_EVERY, READ_SAMPLED };
struct mode {
    const char *name;
    enum read_policy read;
    bool remap, large, reusable;
};

static const struct mode modes[] = {
    { "commit-none", READ_NONE, false, false, false },
    { "commit-every64", READ_EVERY, false, false, false },
    { "commit-sampled64", READ_SAMPLED, false, false, false },
    { "fixed-256m", READ_NONE, true, false, false },
    { "fixed-32g", READ_NONE, true, true, false },
    { "reusable-fixed-256m", READ_NONE, true, false, true },
    { "reusable-fixed-32g", READ_NONE, true, true, true }
};

static double seconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (double)now.tv_sec + (double)now.tv_nsec / 1e9;
}

static int failure(const struct mode *mode, const char *operation, int code)
{
    printf("{\"event\":\"error\",\"mode\":\"%s\",\"operation\":\"%s\",\"code\":%d,\"errno\":%d}\n",
           mode->name, operation, code, errno);
    return 1;
}

/* vm_region_recurse counters describe an object, so duplicate object IDs must
 * not be added together. pages_swapped_out is the SDK's field name; it is not
 * presented as a per-region compressed-byte count. */
static int region_sample(const struct mode *mode, const char *phase,
                         const char *label, const unsigned char *target)
{
    mach_vm_address_t address = (mach_vm_address_t)(uintptr_t)target;
    mach_vm_size_t size = 0;
    natural_t depth = 0;
    vm_region_submap_info_data_64_t info;
    mach_msg_type_number_t count;
    kern_return_t rc;
    do {
        memset(&info, 0, sizeof(info));
        count = VM_REGION_SUBMAP_INFO_COUNT_64;
        rc = mach_vm_region_recurse(mach_task_self(), &address, &size, &depth,
                                   (vm_region_recurse_info_t)&info, &count);
        if (rc != KERN_SUCCESS) return failure(mode, "mach_vm_region_recurse", rc);
        if (info.is_submap) ++depth;
    } while (info.is_submap);
    bool contains = address <= (mach_vm_address_t)(uintptr_t)target &&
                    (mach_vm_address_t)(uintptr_t)target - address < size;
    printf("{\"event\":\"region\",\"mode\":\"%s\",\"phase\":\"%s\",\"label\":\"%s\","
           "\"query_address\":\"0x%" PRIxPTR "\",\"region_address\":\"0x%llx\","
           "\"region_size_bytes\":%llu,\"contains_query\":%s,\"info_count\":%u,"
           "\"object_id\":%u,\"object_id_full\":%llu,\"object_offset_bytes\":%llu,"
           "\"pages_resident\":%u,\"pages_swapped_out\":%u,\"pages_dirtied\":%u,"
           "\"pages_reusable\":%u,\"ref_count\":%u,\"shadow_depth\":%u,"
           "\"protection\":%d,\"share_mode\":%u}\n",
           mode->name, phase, label, (uintptr_t)target,
           (unsigned long long)address, (unsigned long long)size,
           contains ? "true" : "false", count, info.object_id,
           (unsigned long long)info.object_id_full,
           (unsigned long long)info.offset, info.pages_resident,
           info.pages_swapped_out, info.pages_dirtied, info.pages_reusable,
           info.ref_count, info.shadow_depth, info.protection, info.share_mode);
    return contains ? 0 : failure(mode, "region_contains_query", 0);
}

static int sample(const struct mode *mode, const char *phase,
                  const unsigned char *base, size_t reserve_bytes)
{
    task_vm_info_data_t info;
    mach_msg_type_number_t count = TASK_VM_INFO_COUNT;
    memset(&info, 0, sizeof(info));
    kern_return_t rc = task_info(mach_task_self(), TASK_VM_INFO,
                                (task_info_t)&info, &count);
    if (rc != KERN_SUCCESS || count < TASK_VM_INFO_REV1_COUNT)
        return failure(mode, "TASK_VM_INFO", rc);
    printf("{\"event\":\"task_vm\",\"mode\":\"%s\",\"phase\":\"%s\","
           "\"info_count\":%u,\"page_size_bytes\":%d,\"region_count\":%d,"
           "\"virtual_size_bytes\":%llu,\"resident_size_bytes\":%llu,"
           "\"internal_bytes\":%llu,\"compressed_bytes\":%llu,"
           "\"reusable_bytes\":%llu,\"phys_footprint_bytes\":%llu,"
           "\"resident_mib\":%.6f,\"internal_mib\":%.6f,"
           "\"compressed_mib\":%.6f,\"reusable_mib\":%.6f,\"phys_footprint_mib\":%.6f}\n",
           mode->name, phase, count, info.page_size, info.region_count,
           (unsigned long long)info.virtual_size,
           (unsigned long long)info.resident_size,
           (unsigned long long)info.internal,
           (unsigned long long)info.compressed,
           (unsigned long long)info.reusable,
           (unsigned long long)info.phys_footprint,
           (double)info.resident_size / MIB, (double)info.internal / MIB,
           (double)info.compressed / MIB, (double)info.reusable / MIB,
           (double)info.phys_footprint / MIB);
    if (!base) return 0;
    if (region_sample(mode, phase, "left-neighbor", base) ||
        region_sample(mode, phase, "replace-first", base + REPLACE_OFFSET) ||
        region_sample(mode, phase, "right-neighbor", base + POISON_BYTES - MIB) ||
        region_sample(mode, phase, "committed-unpoisoned", base + POISON_BYTES))
        return 1;
    if (reserve_bytes > COMMIT_BYTES)
        return region_sample(mode, phase, "sparse-tail", base + reserve_bytes - 16384);
    return 0;
}

/* Volatile byte loads force full-range validation after measurement. */
static size_t first_different(const unsigned char *base, size_t size,
                              unsigned char expected)
{
    const volatile unsigned char *bytes = base;
    for (size_t i = 0; i < size; ++i)
        if (bytes[i] != expected) return i;
    return size;
}

static int child_mode(const struct mode *mode)
{
    alarm(MODE_LIMIT_SECONDS);
    long host_page = sysconf(_SC_PAGESIZE);
    if (host_page != 16384) return failure(mode, "requires_16KiB_host_pages", (int)host_page);
    size_t reserve_bytes = mode->large ? LARGE_RESERVE_BYTES : COMMIT_BYTES;
    double start = seconds();
    printf("{\"event\":\"mode_start\",\"mode\":\"%s\",\"pid\":%d,"
           "\"reserve_bytes\":%zu,\"commit_bytes\":%zu,\"explicit_target_write_bytes\":%zu,"
           "\"maximum_target_resident_bound_bytes\":%zu,\"timeout_seconds\":%d}\n",
           mode->name, getpid(), reserve_bytes, COMMIT_BYTES,
           mode->remap ? POISON_BYTES : 0,
           mode->remap ? POISON_BYTES + REPLACE_BYTES : COMMIT_BYTES,
           MODE_LIMIT_SECONDS);
    if (sample(mode, "baseline", NULL, 0)) return 1;
    unsigned char *base = mmap(NULL, reserve_bytes, PROT_NONE,
                              MAP_PRIVATE | MAP_ANON, -1, 0);
    if (base == MAP_FAILED) return failure(mode, "reserve_mmap", -1);
    int result = 1;
    if (sample(mode, "reserved", base, reserve_bytes)) goto cleanup;

    size_t reads = 0;
    double commit_start = seconds();
    for (size_t offset = 0; offset < COMMIT_BYTES; offset += (size_t)host_page)
    {
        if (mprotect(base + offset, (size_t)host_page, PROT_READ | PROT_WRITE))
        {
            failure(mode, "commit_mprotect", -1);
            goto cleanup;
        }
        size_t sequence = offset / (size_t)host_page + 1;
        bool read = mode->read == READ_EVERY ||
                    (mode->read == READ_SAMPLED && (sequence <= 8 || sequence % 4096 == 0));
        if (read)
        {
            unsigned char bytes[64];
            mach_vm_size_t got = 0;
            kern_return_t rc = mach_vm_read_overwrite(mach_task_self(),
                (mach_vm_address_t)(uintptr_t)(base + offset), sizeof(bytes),
                (mach_vm_address_t)(uintptr_t)bytes, &got);
            if (rc != KERN_SUCCESS || got != sizeof(bytes) ||
                first_different(bytes, sizeof(bytes), 0) != sizeof(bytes))
            {
                failure(mode, "commit_mach_read64_zero", rc);
                goto cleanup;
            }
            ++reads;
        }
    }
    printf("{\"event\":\"commit_complete\",\"mode\":\"%s\",\"mprotect_calls\":%zu,"
           "\"mach_read64_calls\":%zu,\"mach_read_requested_bytes\":%zu,\"elapsed_seconds\":%.6f}\n",
           mode->name, COMMIT_BYTES / (size_t)host_page, reads, reads * 64,
           seconds() - commit_start);
    if (sample(mode, "committed", base, reserve_bytes)) goto cleanup;

    if (mode->remap)
    {
        memset(base, 0xa5, POISON_BYTES);
        if (sample(mode, "poisoned", base, reserve_bytes)) goto cleanup;
        if (mode->reusable)
        {
            /* This owned interval is now logically decommitted. Do not access
             * it until fresh zero-backed MAP_FIXED mappings replace it. */
            int rc = madvise(base + REPLACE_OFFSET, REPLACE_BYTES, MADV_FREE_REUSABLE);
            printf("{\"event\":\"madvise\",\"mode\":\"%s\",\"advice\":\"MADV_FREE_REUSABLE\","
                   "\"bytes\":%zu,\"rc\":%d,\"errno\":%d}\n",
                   mode->name, REPLACE_BYTES, rc, rc ? errno : 0);
            if (rc) goto cleanup;
            if (sample(mode, "reusable-before-fixed", base, reserve_bytes)) goto cleanup;
        }
        size_t remaps = 0;
        double remap_start = seconds();
        for (size_t offset = REPLACE_OFFSET; offset < REPLACE_OFFSET + REPLACE_BYTES; offset += MIB)
        {
            unsigned char *expected = base + offset;
            void *mapped = mmap(expected, MIB, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0);
            /* Every MAP_FIXED result is checked, never inferred from counters. */
            if (mapped != expected)
            {
                failure(mode, "replace_MAP_FIXED", mapped == MAP_FAILED ? -1 : 0);
                goto cleanup;
            }
            ++remaps;
        }
        printf("{\"event\":\"remap_complete\",\"mode\":\"%s\",\"checked_MAP_FIXED_calls\":%zu,"
               "\"replace_bytes\":%zu,\"elapsed_seconds\":%.6f}\n",
               mode->name, remaps, REPLACE_BYTES, seconds() - remap_start);
        if (sample(mode, "after-fixed-immediate", base, reserve_bytes)) goto cleanup;
        struct timespec settle = { .tv_sec = 0, .tv_nsec = 200000000 };
        if (nanosleep(&settle, NULL)) { failure(mode, "settle_nanosleep", -1); goto cleanup; }
        if (sample(mode, "after-fixed-settled", base, reserve_bytes)) goto cleanup;
    }

    /* No target-memory validation has occurred before this phase, except the
     * explicitly selected Mach read diagnostics during commit. */
    size_t zero_bytes = mode->remap ? REPLACE_BYTES : COMMIT_BYTES;
    const unsigned char *zero_base = mode->remap ? base + REPLACE_OFFSET : base;
    size_t different = first_different(zero_base, zero_bytes, 0);
    bool zero_ok = different == zero_bytes;
    bool left_ok = !mode->remap || first_different(base, MIB, 0xa5) == MIB;
    bool right_ok = !mode->remap || first_different(base + POISON_BYTES - MIB, MIB, 0xa5) == MIB;
    printf("{\"event\":\"validation_after_measurement\",\"mode\":\"%s\","
           "\"zero_bytes_checked\":%zu,\"zero_ok\":%s,\"first_nonzero_offset\":%zu,"
           "\"left_neighbor_preserved\":%s,\"right_neighbor_preserved\":%s,"
           "\"neighbor_bytes_checked\":%zu}\n", mode->name, zero_bytes,
           zero_ok ? "true" : "false", different, left_ok ? "true" : "false",
           right_ok ? "true" : "false", mode->remap ? 2 * MIB : 0);
    if (!zero_ok || !left_ok || !right_ok) goto cleanup;
    if (sample(mode, "after-validation", base, reserve_bytes)) goto cleanup;
    result = 0;

cleanup:
    if (munmap(base, reserve_bytes)) result = failure(mode, "final_munmap", -1);
    if (sample(mode, "after-full-unmap", NULL, 0)) result = 1;
    printf("{\"event\":\"mode_end\",\"mode\":\"%s\",\"result\":%d,\"elapsed_seconds\":%.6f}\n",
           mode->name, result, seconds() - start);
    return result;
}

static int run_mode(const struct mode *mode)
{
    double start = seconds();
    pid_t child = fork();
    if (child < 0) return failure(mode, "fork", -1);
    if (!child) _exit(child_mode(mode));
    int status = 0;
    bool timeout = false;
    for (;;)
    {
        pid_t waited = waitpid(child, &status, WNOHANG);
        if (waited == child) break;
        if (waited < 0 && errno != EINTR) return failure(mode, "waitpid", -1);
        if (seconds() - start > MODE_LIMIT_SECONDS)
        {
            timeout = true;
            if (kill(child, SIGKILL) && errno != ESRCH) failure(mode, "timeout_kill", -1);
            while (waitpid(child, &status, 0) < 0)
                if (errno != EINTR) return failure(mode, "timeout_waitpid", -1);
            break;
        }
        struct timespec poll = { .tv_sec = 0, .tv_nsec = 10000000 };
        nanosleep(&poll, NULL);
    }
    int exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    int signal_number = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
    printf("{\"event\":\"child_status\",\"mode\":\"%s\",\"exit_code\":%d,"
           "\"signal\":%d,\"timeout\":%s,\"elapsed_seconds\":%.6f}\n",
           mode->name, exit_code, signal_number, timeout ? "true" : "false", seconds() - start);
    return exit_code != 0 || signal_number != 0 || timeout;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    const char *selected = NULL;
    if (argc == 3 && !strcmp(argv[1], "--mode")) selected = argv[2];
    else if (argc != 1) { fprintf(stderr, "usage: %s [--mode MODE]\n", argv[0]); return 2; }
    bool matched = false;
    int result = 0;
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
    {
        if (selected && strcmp(selected, modes[i].name)) continue;
        matched = true;
        if (run_mode(&modes[i])) result = 1;
    }
    if (!matched) { fprintf(stderr, "unknown mode: %s\n", selected); return 2; }
    return result;
}
