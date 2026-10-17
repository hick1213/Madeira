/* Inject only the external Mach boundary; keep real pthreads/endpoints. */
#include <assert.h>
#include <mach/mach.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <unistd.h>

static atomic_int fail_notification, fail_type, query_errors, release_worker, phase, reclaimed;
static kern_return_t test_request(mach_port_t task, mach_port_name_t name, mach_msg_id_t id,
                                 mach_port_mscount_t sync, mach_port_t port,
                                 mach_msg_type_name_t type, mach_port_t *previous)
{
    if (atomic_load(&fail_notification)) return KERN_RESOURCE_SHORTAGE;
    return mach_port_request_notification(task, name, id, sync, port, type, previous);
}
static kern_return_t test_type(mach_port_t task, mach_port_name_t name, mach_port_type_t *type)
{
    if (atomic_load(&fail_type)) {
        atomic_fetch_add(&query_errors, 1);
        return KERN_FAILURE;
    }
    return mach_port_type(task, name, type);
}
#define IOS_NATIVE_MAX_THREADS 4
#define IOS_NATIVE_MAX_PROCESSES 4
#define mach_port_request_notification test_request
#define mach_port_type test_type
#include "../../build/ntdll-unix/ios_native_lifetime.c"
#undef mach_port_request_notification
#undef mach_port_type

void ios_thread_registry_native_dead(mach_port_t endpoint, void *teb, uint64_t generation)
{
    (void)teb;
    assert(MACH_PORT_VALID(endpoint) && generation);
}

static void complete(void *peb, uint64_t generation)
{
    (void)peb;
    assert(generation);
    atomic_fetch_add(&reclaimed, 1);
}
static void await_idle(void)
{
    struct ios_native_lifetime_stats stats;
    for (int i = 0; i < 2000; i++) {
        ios_native_lifetime_get_stats(&stats);
        if (!stats.generations) {
            assert(!stats.threads && !stats.reservations && !stats.port_rights);
            return;
        }
        usleep(5000);
    }
    assert(!"lifetime core did not drain confirmed dead endpoints");
}
static void *failed_publication(void *arg)
{
    assert(!ios_native_publish(arg, NULL));
    atomic_store(&phase, 1);
    while (!atomic_load(&release_worker)) {}
    return NULL;
}
int main(void)
{
    alarm(20);
    struct ios_native_reservation *entry = ios_native_process_reserve(complete);
    assert(entry);
    atomic_store(&fail_notification, 1);
    atomic_store(&fail_type, 1);
    pthread_t thread;
    assert(!pthread_create(&thread, NULL, failed_publication, entry));
    while (!atomic_load(&phase)) usleep(1000);
    struct ios_native_lifetime_stats stats;
    ios_native_lifetime_get_stats(&stats);
    assert(stats.threads == 1 && stats.port_rights == 1 && !atomic_load(&reclaimed));
    atomic_store(&release_worker, 1);
    assert(!pthread_join(thread, NULL));
    while (!atomic_load(&query_errors)) usleep(1000);
    ios_native_lifetime_get_stats(&stats);
    assert(stats.threads == 1 && stats.port_rights == 1 && !atomic_load(&reclaimed));
    atomic_store(&fail_type, 0);
    atomic_store(&fail_notification, 0);
    await_idle();
    assert(atomic_load(&reclaimed) == 1);
    puts("PASS notification setup/query failures: no PE admission or false death; confirmed dead right later drains");

    struct ios_native_reservation *reserved[4];
    for (int i = 0; i < 4; i++) assert((reserved[i] = ios_native_process_reserve(complete)));
    assert(!ios_native_process_reserve(complete));
    ios_native_lifetime_get_stats(&stats);
    assert(stats.reservations == 4 && stats.generations == 4 && !stats.threads);
    for (int i = 0; i < 4; i++) ios_native_creation_failed(reserved[i]);
    await_idle();
    assert(atomic_load(&reclaimed) == 1); /* no PEB/resources existed for failed creates */
    puts("PASS storage capacity: creation rejected before launch; failed reservations drain without resource callback");
    assert(!ios_native_consumer_acquire((void *)0x1234));
    assert(!ios_native_retire_process((void *)0x1234));
    puts("PASS unknown owner cannot acquire a callback lease or reclaim another generation");
    return 0;
}
