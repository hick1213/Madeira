/* Actual lifecycle code is inserted below. Only pthread creation/join errors,
 * the UI-thread predicate, seeding and the external server body are adapted. */
#define _DARWIN_C_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sched.h>

int wineserver_start(const char *);
int wineserver_is_running(void);
int wineserver_is_ready(void);
void wineserver_stop(void);
volatile int g_wineserver_should_stop;
int wineserver_ready;
static _Thread_local int host_ui_thread;
static pthread_mutex_t host_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t host_cond = PTHREAD_COND_INITIALIZER;
static unsigned host_created, host_entered, host_joined;
static unsigned host_fonts_prepared;
static int host_create_error, host_attr_error, host_join_error, host_mode;
static unsigned host_attrs;
static int host_seed_hold, host_seed_entered, host_seed_continue;
static int host_join_hold, host_join_entered, host_join_continue;
static unsigned host_start_waiting, host_stop_waiting;
static _Thread_local int host_wait_role;
static int host_entry_stop[128];
static pthread_t host_threads[128];
static unsigned host_join_states[128];
enum { HOST_NORMAL, HOST_NATURAL_EXIT, HOST_SELF_STOP, HOST_FATAL_EXIT };

static void wine_log_msg(const char *fmt, ...) { (void)fmt; }
void madeira_seed_prefix_if_needed(const char *prefix) {
    assert(prefix && *prefix);
    assert(!pthread_mutex_lock(&host_mutex));
    if (host_seed_hold) {
        host_seed_entered = 1;
        assert(!pthread_cond_broadcast(&host_cond));
        while (!host_seed_continue) assert(!pthread_cond_wait(&host_cond, &host_mutex));
    }
    assert(!pthread_mutex_unlock(&host_mutex));
}
static void madeira_prepare_cjk_fonts(const char *prefix) {
    assert(prefix && *prefix);
    assert(!pthread_mutex_lock(&host_mutex));
    /* The actual production lifecycle must finish joining the old server
     * before it can edit the next session's on-disk registry. */
    assert(host_created == host_joined);
    ++host_fonts_prepared;
    assert(!pthread_mutex_unlock(&host_mutex));
}
static int host_pthread_main_np(void) { return host_ui_thread; }
static int host_pthread_attr_set_qos_class_np(pthread_attr_t *attr, qos_class_t qos, int priority)
{ (void)attr; (void)qos; (void)priority; return 0; }
static int host_pthread_attr_init(pthread_attr_t *attr)
{
    int error = host_attr_error;
    host_attr_error = 0;
    if (error) return error;
    int ret = pthread_attr_init(attr);
    assert(!ret);
    ++host_attrs;
    return ret;
}
static int host_pthread_attr_destroy(pthread_attr_t *attr)
{
    assert(host_attrs);
    --host_attrs;
    return pthread_attr_destroy(attr);
}
static int host_pthread_cond_wait(pthread_cond_t *cond, pthread_mutex_t *mutex)
{
    /* Preserve the real OS wait; this only provides a deterministic handshake
     * before the caller releases the lifecycle lock and waits for a join owner. */
    assert(!pthread_mutex_lock(&host_mutex));
    if (host_wait_role == 1) ++host_start_waiting;
    if (host_wait_role == 2) ++host_stop_waiting;
    assert(!pthread_cond_broadcast(&host_cond));
    assert(!pthread_mutex_unlock(&host_mutex));
    return pthread_cond_wait(cond, mutex);
}
void ios_wineserver_wake(void)
{
    assert(!pthread_mutex_lock(&host_mutex));
    assert(!pthread_cond_broadcast(&host_cond));
    assert(!pthread_mutex_unlock(&host_mutex));
}
static void *wineserver_thread_body(void *arg)
{
    (void)arg;
    assert(!pthread_mutex_lock(&host_mutex));
    unsigned index = host_entered++;
    assert(index < 128);
    host_entry_stop[index] = __atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE);
    int mode = host_mode;
    __atomic_store_n(&wineserver_ready, 1, __ATOMIC_RELEASE);
    assert(!pthread_cond_broadcast(&host_cond));
    assert(!pthread_mutex_unlock(&host_mutex));
    if (mode == HOST_SELF_STOP) {
        wineserver_stop();
        assert(wineserver_start("/tmp/madeira-server-self-restart") == -1);
    }
    if (mode == HOST_FATAL_EXIT) pthread_exit(NULL);
    if (mode == HOST_NATURAL_EXIT) return NULL;
    assert(!pthread_mutex_lock(&host_mutex));
    while (!__atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE)) {
        /* External server poll has a periodic wake even without stop signalling.
         * A short timed wait models that existing behaviour, not a new policy. */
        struct timespec deadline;
        clock_gettime(CLOCK_REALTIME, &deadline);
        deadline.tv_nsec += 2000000;
        if (deadline.tv_nsec >= 1000000000) { ++deadline.tv_sec; deadline.tv_nsec -= 1000000000; }
        int err = pthread_cond_timedwait(&host_cond, &host_mutex, &deadline);
        assert(!err || err == ETIMEDOUT);
    }
    assert(!pthread_mutex_unlock(&host_mutex));
    return NULL;
}
static int host_pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                               void *(*entry)(void *), void *arg)
{
    assert(!pthread_mutex_lock(&host_mutex));
    int error = host_create_error;
    host_create_error = 0;
    unsigned index = host_created;
    if (!error) ++host_created;
    assert(!pthread_mutex_unlock(&host_mutex));
    if (error) return error;
    int ret = pthread_create(thread, attr, entry, arg);
    assert(!ret);
    assert(!pthread_mutex_lock(&host_mutex));
    host_threads[index] = *thread;
    assert(!pthread_mutex_unlock(&host_mutex));
    return 0;
}
static int host_pthread_join(pthread_t thread, void **result)
{
    assert(!host_ui_thread); /* no blocking join on UIKit's thread */
    assert(!pthread_equal(thread, pthread_self()));
    assert(!pthread_mutex_lock(&host_mutex));
    int index = -1;
    for (unsigned i = 0; i < host_created; ++i)
        if (!host_join_states[i] && pthread_equal(thread, host_threads[i])) index = (int)i;
    assert(index >= 0); /* no repeated or concurrent join on the same owner */
    int error = host_join_error;
    host_join_error = 0;
    if (error) {
        assert(!pthread_mutex_unlock(&host_mutex));
        return error; /* retain the actual unjoined OS thread */
    }
    host_join_states[index] = 1;
    if (host_join_hold) {
        host_join_entered = 1;
        assert(!pthread_cond_broadcast(&host_cond));
        while (!host_join_continue) assert(!pthread_cond_wait(&host_cond, &host_mutex));
    }
    assert(!pthread_mutex_unlock(&host_mutex));
    int ret = pthread_join(thread, result);
    assert(!ret);
    assert(!pthread_mutex_lock(&host_mutex));
    host_join_states[index] = 2;
    ++host_joined;
    assert(!pthread_mutex_unlock(&host_mutex));
    return 0;
}
#define pthread_create host_pthread_create
#define pthread_join host_pthread_join
#define pthread_main_np host_pthread_main_np
#define pthread_attr_set_qos_class_np host_pthread_attr_set_qos_class_np
#define pthread_attr_init host_pthread_attr_init
#define pthread_attr_destroy host_pthread_attr_destroy
#define pthread_cond_wait host_pthread_cond_wait
/* PRODUCTION_LIFECYCLE */
#undef pthread_create
#undef pthread_join
#undef pthread_attr_init
#undef pthread_attr_destroy
#undef pthread_cond_wait

static void host_wait_entered(unsigned count)
{
    assert(!pthread_mutex_lock(&host_mutex));
    while (host_entered < count) assert(!pthread_cond_wait(&host_cond, &host_mutex));
    assert(!pthread_mutex_unlock(&host_mutex));
}
static void host_expect_stopped(void)
{
    if (wineserver_is_running() || wineserver_is_ready()) {
        fputs("FAIL: stopped server must report running=0 and ready=0\n", stderr);
        exit(1);
    }
}
static void *host_stop_thread(void *unused) { (void)unused; host_wait_role = 2; wineserver_stop(); return NULL; }
struct host_start { const char *prefix; int result; };
static void *host_start_thread(void *arg)
{
    struct host_start *start = arg;
    host_wait_role = 1;
    start->result = wineserver_start(start->prefix);
    return NULL;
}
static void host_wait_not_running(void)
{
    while (wineserver_is_running()) sched_yield();
    assert(!wineserver_is_ready());
}
int main(void)
{
    assert(wineserver_start("/tmp/madeira-server-first") == 0);
    host_wait_entered(1);
    assert(!host_entry_stop[0] && wineserver_is_running() && wineserver_is_ready());
    assert(host_fonts_prepared == 1);
    assert(wineserver_start("/tmp/madeira-server-already-running") == 0);
    assert(host_fonts_prepared == 1); /* Never migrate an active server hive. */
    wineserver_stop();
    host_expect_stopped();
    wineserver_stop();
    assert(host_joined == 1);
    assert(wineserver_start("/tmp/madeira-server-second") == 0);
    host_wait_entered(2);
    assert(!host_entry_stop[1]);
    assert(wineserver_is_running() && wineserver_is_ready());
    pthread_t stoppers[8];
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_create(&stoppers[i], NULL, host_stop_thread, NULL));
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_join(stoppers[i], NULL));
    assert(host_joined == 2);
    host_expect_stopped();

    host_create_error = EAGAIN;
    assert(wineserver_start("/tmp/madeira-server-create-error") == -1);
    host_expect_stopped();
    assert(host_created == 2 && host_joined == 2);
    wineserver_stop();
    assert(host_joined == 2);

    host_mode = HOST_NORMAL;
    assert(wineserver_start("/tmp/madeira-server-ui") == 0);
    host_wait_entered(3);
    host_ui_thread = 1;
    wineserver_stop(); /* request only; must never join here */
    host_ui_thread = 0;
    wineserver_stop();
    assert(host_joined == 3);
    host_expect_stopped();

    host_mode = HOST_SELF_STOP;
    assert(wineserver_start("/tmp/madeira-server-self") == 0);
    host_wait_entered(4);
    wineserver_stop();
    assert(host_joined == 4);
    host_expect_stopped();

    host_mode = HOST_FATAL_EXIT;
    assert(wineserver_start("/tmp/madeira-server-fatal") == 0);
    host_wait_entered(5);
    wineserver_stop();
    assert(host_joined == 5);
    host_expect_stopped();

    host_mode = HOST_NATURAL_EXIT;
    assert(wineserver_start("/tmp/madeira-server-natural") == 0);
    host_wait_entered(6);
    wineserver_stop();
    assert(host_joined == 6);
    host_expect_stopped();

    host_attr_error = ENOMEM;
    assert(wineserver_start("/tmp/madeira-server-attr-error") == -1);
    assert(!host_attrs && host_created == 6 && host_joined == 6);
    host_expect_stopped();

    host_mode = HOST_NORMAL;
    assert(wineserver_start("/tmp/madeira-server-join-error") == 0);
    host_wait_entered(7);
    host_join_error = EINVAL;
    wineserver_stop();
    host_wait_not_running();
    assert(host_created == 7 && host_joined == 6);
    assert(wineserver_start("/tmp/madeira-server-after-join-error") == 0);
    host_wait_entered(8);
    assert(host_joined == 7 && !host_entry_stop[7]);
    wineserver_stop();
    assert(host_joined == 8);
    host_expect_stopped();

    /* Stop arriving while external prefix seeding is held must cancel creation
     * and cannot poison the subsequent fresh start. */
    host_seed_hold = 1;
    struct host_start seeded = {"/tmp/madeira-server-seed-cancel", 99};
    pthread_t seeding_thread;
    assert(!pthread_create(&seeding_thread, NULL, host_start_thread, &seeded));
    assert(!pthread_mutex_lock(&host_mutex));
    while (!host_seed_entered) assert(!pthread_cond_wait(&host_cond, &host_mutex));
    assert(!pthread_mutex_unlock(&host_mutex));
    host_ui_thread = 1;
    wineserver_stop();
    host_ui_thread = 0;
    assert(!pthread_mutex_lock(&host_mutex));
    host_seed_continue = 1;
    assert(!pthread_cond_broadcast(&host_cond));
    assert(!pthread_mutex_unlock(&host_mutex));
    assert(!pthread_join(seeding_thread, NULL));
    host_seed_hold = 0;
    assert(seeded.result == -1 && host_created == 8 && host_joined == 8);
    host_expect_stopped();

    /* A new background start waits for the exact old join; an old concurrent
     * stop waiter must not stop the new generation when it finally resumes. */
    assert(wineserver_start("/tmp/madeira-server-old-generation") == 0);
    host_wait_entered(9);
    host_join_hold = 1;
    pthread_t joining_thread, old_stopper, restarting_thread;
    assert(!pthread_create(&joining_thread, NULL, host_stop_thread, NULL));
    assert(!pthread_mutex_lock(&host_mutex));
    while (!host_join_entered) assert(!pthread_cond_wait(&host_cond, &host_mutex));
    host_start_waiting = host_stop_waiting = 0;
    assert(!pthread_mutex_unlock(&host_mutex));
    host_ui_thread = 1;
    assert(wineserver_start("/tmp/madeira-server-ui-join-busy") == -1);
    host_ui_thread = 0;
    struct host_start restarted = {"/tmp/madeira-server-new-generation", 99};
    assert(!pthread_create(&old_stopper, NULL, host_stop_thread, NULL));
    assert(!pthread_create(&restarting_thread, NULL, host_start_thread, &restarted));
    assert(!pthread_mutex_lock(&host_mutex));
    while (!host_start_waiting || !host_stop_waiting)
        assert(!pthread_cond_wait(&host_cond, &host_mutex));
    assert(host_created == 9 && restarted.result == 99);
    host_join_continue = 1;
    host_join_hold = 0;
    assert(!pthread_cond_broadcast(&host_cond));
    assert(!pthread_mutex_unlock(&host_mutex));
    assert(!pthread_join(joining_thread, NULL));
    assert(!pthread_join(old_stopper, NULL));
    assert(!pthread_join(restarting_thread, NULL));
    host_wait_entered(10);
    assert(restarted.result == 0 && host_created == 10 && host_joined == 9);
    assert(!host_entry_stop[9] && wineserver_is_running() && wineserver_is_ready());
    wineserver_stop();
    assert(host_joined == 10 && !host_attrs);
    host_expect_stopped();
    puts("PASS: restart/reset, one join owner, repeated/concurrent/self/UI stop, create rollback and pthread_exit cleanup");
    return 0;
}
