/* Compile the actual child and bridge pthread wrappers; stub only Wine work. */
#include <assert.h>
#include <mach/mach.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "../../build/ntdll-unix/ios_native_lifetime.h"

static atomic_int boot_unlocks, slot_releases, server_stops, frees, completed, late_started, late_release;
static int abort_body, is_child;
static pthread_key_t late_key;
pthread_key_t ios_teb_tls_key;
static sigset_t server_block_set;
static _Thread_local int wine_session_finish_started;
struct pe_image_info { int unused; };
#include "native-child-args.inc"

void ios_child_boot_unlock(void) { atomic_fetch_add(&boot_unlocks, 1); }
static void ios_child_slot_release(int slot) { assert(slot == 3); atomic_fetch_add(&slot_releases, 1); }
static void wineserver_stop(void) { atomic_fetch_add(&server_stops, 1); }
static void wine_process_clear_foreign_teb_slot(void) { assert(!pthread_setspecific(ios_teb_tls_key, NULL)); }
static void fixture_free(void *ptr) { assert(ptr); atomic_fetch_add(&frees, 1); free(ptr); }

static void *body(void)
{
    assert(ios_native_bind_current((void *)1));
    assert(ios_native_ready_current((void *)2));
    assert(!pthread_setspecific(ios_teb_tls_key, (void *)3));
    assert(!pthread_setspecific(late_key, (void *)4));
    if (abort_body) pthread_exit(NULL);
    return NULL;
}
static void *ios_child_thread_body(void *arg) { (void)arg; return body(); }
static void *wine_process_thread_body(void *arg) { (void)arg; return body(); }
#define free fixture_free
#include "native-bootstrap-wrappers.inc"
#undef free

void ios_thread_registry_native_dead(mach_port_t endpoint, void *teb, uint64_t generation)
{
    (void)teb;
    assert(MACH_PORT_VALID(endpoint) && generation);
}

static void late_destructor(void *ptr)
{
    assert(ptr == (void *)4 && !pthread_getspecific(ios_teb_tls_key));
    assert(is_child ? (atomic_load(&slot_releases) == 1 && atomic_load(&boot_unlocks) == 1 &&
                       atomic_load(&frees) == 2) : atomic_load(&server_stops) == 1);
    assert(!atomic_load(&completed));
    atomic_store(&late_started, 1);
    while (!atomic_load(&late_release)) {}
}

static void complete(void *peb, uint64_t generation)
{
    assert(peb == (void *)1 && generation);
    atomic_fetch_add(&completed, 1);
}

int main(void)
{
    alarm(25);
    assert(!pthread_key_create(&ios_teb_tls_key, NULL));
    sigemptyset(&server_block_set);
    sigaddset(&server_block_set, SIGQUIT);
    for (int mode = 0; mode < 4; mode++) {
        is_child = !(mode & 2);
        abort_body = mode & 1;
        atomic_store(&boot_unlocks, 0); atomic_store(&slot_releases, 0);
        atomic_store(&server_stops, 0); atomic_store(&frees, 0);
        atomic_store(&completed, 0); atomic_store(&late_started, 0); atomic_store(&late_release, 0);
        struct ios_native_reservation *entry = ios_native_process_reserve(complete);
        assert(entry);
        assert(!pthread_key_create(&late_key, late_destructor));
        pthread_t thread;
        if (is_child) {
            struct ios_child_args *args = calloc(1, sizeof(*args));
            assert(args);
            args->native_reservation = entry;
            args->argv = calloc(1, sizeof(char *));
            assert(args->argv);
            args->slot = 3;
            assert(!pthread_create(&thread, NULL, ios_child_thread_entry, args));
        } else assert(!pthread_create(&thread, NULL, wine_process_thread, entry));
        while (!atomic_load(&late_started)) usleep(1000);
        assert(!atomic_load(&completed));
        atomic_store(&late_release, 1);
        assert(!pthread_join(thread, NULL));
        struct ios_native_lifetime_stats stats;
        for (int i = 0; i < 2000; i++) {
            ios_native_lifetime_get_stats(&stats);
            if (!stats.generations) break;
            usleep(5000);
        }
        assert(!stats.generations && atomic_load(&completed) == 1);
        assert(!pthread_key_delete(late_key));
    }
    assert(!pthread_key_delete(ios_teb_tls_key));
    puts("PASS actual child/bridge wrappers: normal return and direct pthread_exit clean once before late TLS, reclaim after death");
    return 0;
}
