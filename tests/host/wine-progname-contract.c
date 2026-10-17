/* Runs actual Wine helpers in a disposable Darwin process. Never dereference
 * a guest-stack pointer after its pthread has been joined. */
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int BOOL;
#define FALSE 0
static int main_argc;
static char **main_argv;

/* PRODUCTION_FUNCTIONS */

static const char *host_name;
static char *host_name_copy;
static uintptr_t observed_name, stack_low, stack_high;
static int host_content_preserved, guest_name_selected, argv_preserved;

static void *worker(void *unused)
{
    (void)unused;
    char exe_path[512];
    snprintf(exe_path, sizeof(exe_path),
             "C:\\madeira-shadow-trace-20261013\\d3d11-depth-comparison-probe.exe");
    char *argv[4] = { "wine", exe_path, "--guest-argument", NULL };
    main_argc = 3;
    main_argv = argv;
    stack_high = (uintptr_t)pthread_get_stackaddr_np(pthread_self());
    stack_low = stack_high - pthread_get_stacksize_np(pthread_self());

    rebuild_argv();
    const char *name = getprogname();
    observed_name = (uintptr_t)name;
    /* Both comparisons run while this thread's exe_path is still alive. */
    host_content_preserved = !strcmp(name, host_name_copy);
    guest_name_selected = name == strrchr(exe_path, '\\') + 1 &&
                          !strcmp(name, "d3d11-depth-comparison-probe.exe");
    argv_preserved = main_argc == 2 && main_argv[0] == exe_path &&
                     !strcmp(main_argv[0], exe_path) &&
                     !strcmp(main_argv[1], "--guest-argument") && main_argv[2] == NULL;
    return NULL;
}

static int failures;
static void check(int value, const char *name)
{
    printf("%s: %s\n", value ? "PASS" : "FAIL", name);
    if (!value) failures++;
}

int main(void)
{
    host_name = getprogname();
    host_name_copy = strdup(host_name);
    if (!host_name_copy) return 2;
    pthread_t thread;
    if (pthread_create(&thread, NULL, worker, NULL) || pthread_join(thread, NULL)) return 2;
    /* getprogname returns a pointer only. Restore host identity immediately,
     * before any printing/library diagnostics; do not inspect old bytes. */
    uintptr_t after_join = (uintptr_t)getprogname();
    setprogname(host_name);
    main_argv = NULL;
    main_argc = 0;

    int points_into_worker = observed_name >= stack_low && observed_name < stack_high;
    printf("observed pointer was in worker native stack: %d\n", points_into_worker);
#if HOST_EXPECT_IOS
    check(observed_name == (uintptr_t)host_name && after_join == (uintptr_t)host_name,
          "iOS guest leaves host program-name pointer untouched through native join");
    check(host_content_preserved, "iOS guest leaves host program-name content untouched");
    check(!points_into_worker, "iOS host name never borrows the guest thread stack");
#else
    check(guest_name_selected && points_into_worker && after_join == observed_name,
          "non-iOS preserves upstream guest basename selection");
#endif
    check(argv_preserved, "actual rebuild_argv preserves guest image path and arguments");
    check(getprogname() == host_name && !strcmp(getprogname(), host_name_copy),
          "test restores original native host identity");
    free(host_name_copy);
    printf("failures=%d\n", failures);
    return failures ? 1 : 0;
}
