/* Disposable process only: own keys 275 and 291 before running the old raw helper. */
#include <pthread.h>
#include <stdint.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>

static pthread_key_t ios_teb_tls_key;
static int ios_teb_tls_key_created;
static unsigned cleanup_sets;
static int failures;
static int foreign_value, teb_value;

static __attribute__((unused)) int cleanup_setspecific(pthread_key_t key, const void *value)
{
    cleanup_sets++;
    return pthread_setspecific(key, value);
}

#define pthread_setspecific cleanup_setspecific
/* PRODUCTION_CLEANUP */
#undef pthread_setspecific

static jmp_buf initialization_exit;
static int initialization_status;
static _Noreturn void test_init_exit(int status)
{
    initialization_status = status;
    longjmp(initialization_exit, 1);
}

#define exit test_init_exit
static void initialize_key(void)
{
    /* PRODUCTION_INITIALIZATION */
}
#undef exit

static void check(int condition, const char *name)
{
    printf("%s: %s\n", condition ? "PASS" : "FAIL", name);
    if (!condition) failures++;
}

static void *worker(void *unused)
{
    (void)unused;
    if (pthread_setspecific(275, &foreign_value)) abort();
    uintptr_t base;
    __asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(base));
    base &= ~7ULL;
    check(*(void **)(base + 275 * 8) == &foreign_value,
          "foreign canary really occupies raw slot 275");

    ios_teb_tls_key = 0;
    ios_teb_tls_key_created = 0;
    void *foreign_zero = pthread_getspecific(0);
    cleanup_sets = 0;
    wine_process_clear_foreign_teb_slot();
    check(pthread_getspecific(0) == foreign_zero,
          "before key initialization foreign key 0 is retained");
    check(cleanup_sets == 0, "before initialization no pthread key is cleared");
    check(pthread_getspecific(275) == &foreign_value,
          "before initialization foreign 275 is retained");

    ios_teb_tls_key = 291;
    ios_teb_tls_key_created = 1;
    for (int session = 1; session <= 2; session++)
    {
        if (pthread_setspecific(275, &foreign_value) ||
            pthread_setspecific(ios_teb_tls_key, &teb_value)) abort();
        cleanup_sets = 0;
        wine_process_clear_foreign_teb_slot();
        printf("session %d\n", session);
        check(pthread_getspecific(275) == &foreign_value, "foreign 275 is retained");
        check(pthread_getspecific(ios_teb_tls_key) == NULL, "owned 291 is cleared");
        check(cleanup_sets == 1, "cleanup clears exactly one owned key");
        wine_process_clear_foreign_teb_slot();
        check(pthread_getspecific(275) == &foreign_value &&
              pthread_getspecific(ios_teb_tls_key) == NULL, "repeat cleanup is safe");
    }
    pthread_setspecific(275, NULL);
    pthread_setspecific(291, NULL);

    /* Real Darwin resource exhaustion exercises the actual initialization
     * block. Intercept only its noreturn exit boundary, as Wine's shim does. */
    pthread_key_t extra[1024];
    unsigned count = 0;
    while (count < 1024 && !pthread_key_create(&extra[count], NULL)) count++;
    if (count == 1024) abort();
    ios_teb_tls_key = 0;
    ios_teb_tls_key_created = 0;
    initialization_status = 0;
    foreign_zero = pthread_getspecific(0);
    if (!setjmp(initialization_exit)) initialize_key();
    check(initialization_status == 1, "key allocation failure exits initialization");
    check(!ios_teb_tls_key_created, "failed allocation never publishes ownership");
    cleanup_sets = 0;
    wine_process_clear_foreign_teb_slot();
    check(cleanup_sets == 0 && pthread_getspecific(0) == foreign_zero,
          "failed allocation cleanup preserves foreign key 0");
    for (unsigned i = 0; i < count; i++) pthread_key_delete(extra[i]);
    return NULL;
}

int main(void)
{
    pthread_key_t keys[512];
    unsigned count = 0;
    int own_275 = 0, own_291 = 0;
    while (count < 512 && !own_291)
    {
        if (pthread_key_create(&keys[count], NULL)) abort();
        if (keys[count] == 275) own_275 = 1;
        if (keys[count] == 291) own_291 = 1;
        if (keys[count++] > 291) break;
    }
    if (!own_275 || !own_291)
    {
        fputs("Infrastructure error: test does not own both required keys\n", stderr);
        return 2;
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, worker, NULL) || pthread_join(thread, NULL)) abort();
    for (unsigned i = 0; i < count; i++) pthread_key_delete(keys[i]);
    printf("failures=%d\n", failures);
    return failures ? 1 : 0;
}
