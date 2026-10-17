/* Darwin behavior tests for the production lifetime core. */
#include <assert.h>
#include <errno.h>
#include <mach/mach.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include "../../build/ntdll-unix/ios_native_lifetime.h"

void fixture_reset(void);
int fixture_valid(void);
void fixture_reclaim(void *peb);
void ios_thread_registry_native_dead(mach_port_t endpoint, void *teb, uint64_t generation)
{
    (void)teb;
    assert(MACH_PORT_VALID(endpoint) && generation);
}

static pthread_mutex_t barrier_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t barrier_cond = PTHREAD_COND_INITIALIZER;
static int phase, release_phase;
static atomic_int reclaims, tls_calls, signal_calls;
static pthread_key_t late_key;
static int with_fixture;
static struct ios_native_reservation *delayed_reservation;
static atomic_int unblock_signal;
static int blocked_delivery;
static atomic_int child_reclaims;

static void publish_phase(int value)
{
    pthread_mutex_lock(&barrier_lock);
    phase = value;
    pthread_cond_broadcast(&barrier_cond);
    pthread_mutex_unlock(&barrier_lock);
}

static void wait_phase(int value)
{
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 10;
    pthread_mutex_lock(&barrier_lock);
    while (phase < value) assert(!pthread_cond_timedwait(&barrier_cond, &barrier_lock, &deadline));
    pthread_mutex_unlock(&barrier_lock);
}

static void await_release(int value)
{
    pthread_mutex_lock(&barrier_lock);
    while (release_phase < value) pthread_cond_wait(&barrier_cond, &barrier_lock);
    pthread_mutex_unlock(&barrier_lock);
}

static void release_to(int value)
{
    pthread_mutex_lock(&barrier_lock);
    release_phase = value;
    pthread_cond_broadcast(&barrier_cond);
    pthread_mutex_unlock(&barrier_lock);
}

static void reclaimed(void *peb, uint64_t generation)
{
    assert(peb == (void *)1 && generation);
    if (with_fixture) fixture_reclaim(peb);
    atomic_fetch_add(&reclaims, 1);
}

static void wait_idle(void)
{
    struct ios_native_lifetime_stats stats;
    for (int i = 0; i < 2000; i++) {
        ios_native_lifetime_get_stats(&stats);
        if (!stats.generations) {
            assert(!stats.threads && !stats.reservations && !stats.consumers && !stats.port_rights);
            return;
        }
        usleep(5000); /* harness deadline only; production never expires an owner */
    }
    fprintf(stderr, "not idle: generations=%u threads=%u reservations=%u consumers=%u rights=%u\n",
            stats.generations, stats.threads, stats.reservations, stats.consumers, stats.port_rights);
    abort();
}

static void reset_case(void)
{
    wait_idle();
    phase = release_phase = 0;
    atomic_store(&reclaims, 0);
    atomic_store(&tls_calls, 0);
    atomic_store(&signal_calls, 0);
    with_fixture = 0;
}

static void late_destructor(void *value)
{
    assert(value);
    assert(fixture_valid());
    publish_phase(1);
    await_release(1);
    /* Actual production clear must still not have executed here. */
    assert(fixture_valid());
    atomic_fetch_add(&tls_calls, 1);
}

static void *late_bootstrap(void *arg)
{
    assert(ios_native_publish(arg, (void *)2));
    assert(ios_native_bind_current((void *)1));
    assert(!pthread_setspecific(late_key, (void *)1));
    ios_native_retire_current();
    ios_native_retire_current();
    return NULL;
}

static void test_late_destructor(void)
{
    reset_case();
    with_fixture = 1;
    fixture_reset();
    struct ios_native_reservation *entry = ios_native_process_reserve(reclaimed);
    assert(entry);
    /* The lifetime key already exists, so its destructor can precede ours. */
    assert(!pthread_key_create(&late_key, late_destructor));
    pthread_t thread;
    assert(!pthread_create(&thread, NULL, late_bootstrap, entry));
    assert(!pthread_detach(thread));
    wait_phase(1);
    assert(!atomic_load(&reclaims) && fixture_valid());
    struct ios_native_lifetime_stats stats;
    ios_native_lifetime_get_stats(&stats);
    assert(stats.threads == 1 && stats.retiring == 1 && stats.port_rights == 1);
    release_to(1);
    wait_idle();
    assert(atomic_load(&tls_calls) == 1 && atomic_load(&reclaims) == 1 && !fixture_valid());
    assert(!pthread_key_delete(late_key));
    puts("PASS detached bootstrap: late TLS uses live classification; real endpoint death reclaims once");
}

static void *delayed_worker(void *arg)
{
    await_release(1); /* created but not yet published */
    assert(ios_native_publish(arg, (void *)3));
    assert(!ios_native_ready_current((void *)3)); /* owner closed before publication */
    publish_phase(2);
    await_release(2);
    return NULL;
}

static void *reservation_bootstrap(void *arg)
{
    assert(ios_native_publish(arg, (void *)2));
    assert(ios_native_bind_current((void *)1));
    struct ios_native_reservation *failed = ios_native_thread_reserve((void *)1);
    assert(failed);
    ios_native_creation_failed(failed); /* no pthread was created */
    delayed_reservation = ios_native_thread_reserve((void *)1);
    assert(delayed_reservation);
    pthread_t worker;
    assert(!pthread_create(&worker, NULL, delayed_worker, delayed_reservation));
    ios_native_retire_current();
    assert(!ios_native_thread_reserve((void *)1));
    assert(!ios_native_process_reserve(reclaimed)); /* session admission closed */
    publish_phase(1);
    /* Preserve existing join ownership: the observer must not join this. */
    assert(!pthread_join(worker, NULL));
    return NULL;
}

static void test_creation_race(void)
{
    reset_case();
    pthread_t bootstrap;
    struct ios_native_reservation *entry = ios_native_process_reserve(reclaimed);
    assert(entry && !pthread_create(&bootstrap, NULL, reservation_bootstrap, entry));
    wait_phase(1);
    struct ios_native_lifetime_stats stats;
    ios_native_lifetime_get_stats(&stats);
    assert(stats.reservations == 1 && stats.threads == 1 && !atomic_load(&reclaims));
    release_to(1);
    wait_phase(2);
    ios_native_lifetime_get_stats(&stats);
    assert(!stats.reservations && stats.threads == 2 && !atomic_load(&reclaims));
    release_to(2);
    assert(!pthread_join(bootstrap, NULL));
    wait_idle();
    assert(atomic_load(&reclaims) == 1);
    puts("PASS creation race: pre-publication reservation blocks reclaim; closed admission rejects creation");
}

static void *leased_bootstrap(void *arg)
{
    assert(ios_native_publish(arg, (void *)2));
    assert(ios_native_bind_current((void *)1));
    publish_phase(1);
    await_release(1);
    ios_native_retire_current();
    return NULL;
}

static void test_consumer_and_reused_address(void)
{
    reset_case();
    pthread_t bootstrap;
    struct ios_native_reservation *entry = ios_native_process_reserve(reclaimed);
    assert(entry && !pthread_create(&bootstrap, NULL, leased_bootstrap, entry));
    wait_phase(1);
    uint64_t old = ios_native_consumer_acquire((void *)1);
    assert(old);
    release_to(1);
    assert(!pthread_join(bootstrap, NULL));
    assert(!ios_native_consumer_acquire((void *)1));
    struct ios_native_lifetime_stats stats;
    ios_native_lifetime_get_stats(&stats);
    assert(stats.consumers == 1 && !atomic_load(&reclaims));
    ios_native_consumer_release(old);
    wait_idle();
    assert(atomic_load(&reclaims) == 1);

    phase = release_phase = 0;
    entry = ios_native_process_reserve(reclaimed);
    assert(entry && !pthread_create(&bootstrap, NULL, leased_bootstrap, entry));
    wait_phase(1);
    uint64_t fresh = ios_native_consumer_acquire((void *)1);
    assert(fresh && fresh != old);
    ios_native_consumer_release(old); /* delayed stale-generation event */
    ios_native_lifetime_get_stats(&stats);
    assert(stats.consumers == 1);
    ios_native_consumer_release(fresh);
    release_to(1);
    assert(!pthread_join(bootstrap, NULL));
    wait_idle();
    assert(atomic_load(&reclaims) == 2);
    puts("PASS foreign callback lease and reused PEB/TEB: old generation cannot release new owner");
}

static void *simple_bootstrap(void *arg)
{
    assert(ios_native_publish(arg, (void *)2));
    assert(ios_native_bind_current((void *)1));
    ios_native_retire_current();
    return NULL;
}

static void test_repeat(void)
{
    reset_case();
    for (int i = 0; i < 100; i++) {
        pthread_t thread;
        struct ios_native_reservation *entry = ios_native_process_reserve(reclaimed);
        assert(entry && !pthread_create(&thread, NULL, simple_bootstrap, entry));
        assert(!pthread_detach(thread));
        wait_idle();
    }
    assert(atomic_load(&reclaims) == 100);
    puts("PASS 100 repeated detached generations: zero outstanding records/reservations/owned port rights");
}

static void signal_destructor(void *value)
{
    assert(value);
    atomic_fetch_add(&tls_calls, 1);
}

static void quit_handler(int signal_number)
{
    if (signal_number != SIGQUIT) _Exit(90);
    atomic_fetch_add(&signal_calls, 1);
    /* Model the existing Wine abort_thread -> pthread_exit path, with a
     * thread deliberately outside any application/library critical section. */
    pthread_exit(NULL);
}

static void *signal_worker(void *arg)
{
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGQUIT);
    if (blocked_delivery) assert(!pthread_sigmask(SIG_BLOCK, &mask, NULL));
    assert(ios_native_publish(arg, (void *)3));
    assert(ios_native_ready_current((void *)3));
    assert(!pthread_setspecific(late_key, (void *)1));
    publish_phase(1);
    while (!atomic_load(&unblock_signal)) {} /* no locks at possible signal delivery */
    assert(!pthread_sigmask(SIG_UNBLOCK, &mask, NULL));
    for (;;) {}
}

static void *signal_bootstrap(void *arg)
{
    assert(ios_native_publish(arg, (void *)2));
    assert(ios_native_bind_current((void *)1));
    struct ios_native_reservation *worker_entry = ios_native_thread_reserve((void *)1);
    pthread_t worker;
    assert(worker_entry && !pthread_create(&worker, NULL, signal_worker, worker_entry));
    wait_phase(1);
    ios_native_retire_current();
    publish_phase(2);
    assert(!pthread_join(worker, NULL));
    return NULL;
}

static void test_signal_delivery(void)
{
    struct sigaction action = {0};
    action.sa_handler = quit_handler;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGQUIT, &action, NULL));
    assert(!pthread_key_create(&late_key, signal_destructor));
    for (blocked_delivery = 0; blocked_delivery <= 1; blocked_delivery++) {
        reset_case();
        atomic_store(&unblock_signal, 0);
        pthread_t bootstrap;
        struct ios_native_reservation *entry = ios_native_process_reserve(reclaimed);
        assert(entry && !pthread_create(&bootstrap, NULL, signal_bootstrap, entry));
        wait_phase(2);
        if (blocked_delivery) {
            struct ios_native_lifetime_stats stats;
            ios_native_lifetime_get_stats(&stats);
            assert(stats.threads == 2 && !atomic_load(&reclaims) && !atomic_load(&signal_calls));
        }
        atomic_store(&unblock_signal, 1);
        assert(!pthread_join(bootstrap, NULL));
        wait_idle();
        assert(atomic_load(&tls_calls) == 1 && atomic_load(&signal_calls) == 1 && atomic_load(&reclaims) == 1);
    }
    assert(!pthread_key_delete(late_key));
    puts("PASS exact-endpoint SIGQUIT: unmasked delivery and pending protected-section delivery with TLS cleanup");
}

static void child_complete(void *peb, uint64_t generation)
{
    assert(peb == (void *)2 && generation);
    atomic_fetch_add(&child_reclaims, 1);
}

static void session_complete(void *peb, uint64_t generation)
{
    assert(atomic_load(&child_reclaims) == 1);
    reclaimed(peb, generation);
}

static void *session_child(void *arg)
{
    assert(ios_native_publish(arg, (void *)4));
    assert(ios_native_bind_current((void *)2));
    publish_phase(1);
    await_release(1);
    /* Initial launcher logical exit does not forbid its surviving child from
     * launching the real game or creating workers. */
    struct ios_native_reservation *nested = ios_native_process_reserve(NULL);
    assert(nested);
    ios_native_creation_failed(nested);
    struct ios_native_reservation *worker = ios_native_thread_reserve((void *)2);
    assert(worker);
    ios_native_creation_failed(worker);
    publish_phase(3);
    await_release(2);
    return NULL;
}

static void *session_root(void *arg)
{
    assert(ios_native_publish(arg, (void *)3));
    assert(ios_native_bind_current((void *)1));
    struct ios_native_reservation *child = ios_native_process_reserve(child_complete);
    pthread_t thread;
    assert(child && !pthread_create(&thread, NULL, session_child, child));
    assert(!pthread_detach(thread));
    wait_phase(1);
    assert(ios_native_retire_process((void *)1));
    publish_phase(2);
    wait_phase(3);
    ios_native_retire_current(); /* session close now also closes descendants */
    publish_phase(4);
    return NULL;
}

static void test_session_order(void)
{
    reset_case();
    atomic_store(&child_reclaims, 0);
    pthread_t root;
    struct ios_native_reservation *entry = ios_native_process_reserve(session_complete);
    assert(entry && !pthread_create(&root, NULL, session_root, entry));
    wait_phase(2);
    release_to(1);
    wait_phase(4);
    assert(!pthread_join(root, NULL));
    assert(!atomic_load(&child_reclaims) && !atomic_load(&reclaims));
    release_to(2);
    wait_idle();
    assert(atomic_load(&child_reclaims) == 1 && atomic_load(&reclaims) == 1);
    puts("PASS launcher logical exit preserves children; session completion follows every descendant cleanup");
}

int main(void)
{
    alarm(45);
    test_late_destructor();
    test_creation_race();
    test_consumer_and_reused_address();
    test_signal_delivery();
    test_session_order();
    test_repeat();
    return 0;
}
