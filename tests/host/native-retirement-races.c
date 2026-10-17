/* Force startup failure and SIGQUIT at the real native_lock boundary. */
#include <assert.h>
#include <mach/mach.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int test_mutex_lock(pthread_mutex_t *);
static int test_create(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
static int test_detach(pthread_t);
static mach_msg_return_t test_message(mach_msg_header_t *, mach_msg_option_t,
    mach_msg_size_t, mach_msg_size_t, mach_port_name_t, mach_msg_timeout_t, mach_port_name_t);
#define pthread_mutex_lock test_mutex_lock
#define pthread_create test_create
#define pthread_detach test_detach
#define mach_msg test_message
#include "../../build/ntdll-unix/ios_native_lifetime.c"
#undef pthread_mutex_lock
#undef pthread_create
#undef pthread_detach
#undef mach_msg

static int startup_failure;
static atomic_int ready, enter_lock, inside_lock, release_lock, signals, completed;
static pthread_t target;

static int test_mutex_lock(pthread_mutex_t *lock)
{
    int result = pthread_mutex_lock(lock);
    if (!result && lock == &native_lock && atomic_load(&enter_lock) &&
        pthread_equal(pthread_self(), target)) {
        sigset_t current;
        assert(!pthread_sigmask(SIG_SETMASK, NULL, &current));
        assert(sigismember(&current, SIGQUIT));
        atomic_store(&inside_lock, 1);
        while (!atomic_load(&release_lock)) {}
    }
    return result;
}

static int test_create(pthread_t *thread, const pthread_attr_t *attr,
                       void *(*entry)(void *), void *arg)
{
    int result = pthread_create(thread, attr, entry, arg);
    if (!result && startup_failure && entry == native_observer)
        assert(!pthread_join(*thread, NULL)); /* observer fails before create returns */
    return result;
}

static int test_detach(pthread_t thread)
{
    if (startup_failure) return 0; /* test_create already consumed this join */
    return pthread_detach(thread);
}

static mach_msg_return_t test_message(mach_msg_header_t *msg, mach_msg_option_t options,
    mach_msg_size_t send_size, mach_msg_size_t receive_size, mach_port_name_t receive,
    mach_msg_timeout_t timeout, mach_port_name_t notify)
{
    if (startup_failure && (options & MACH_RCV_MSG)) return MACH_RCV_INVALID_NAME;
    return mach_msg(msg, options, send_size, receive_size, receive, timeout, notify);
}

void ios_thread_registry_native_dead(mach_port_t endpoint, void *teb, uint64_t generation)
{
    (void)teb;
    assert(MACH_PORT_VALID(endpoint) && generation);
}

static void complete(void *peb, uint64_t generation)
{
    assert(peb == (void *)1 && generation);
    atomic_fetch_add(&completed, 1);
}

static void quit(int number)
{
    if (number != SIGQUIT) _Exit(90);
    atomic_fetch_add(&signals, 1);
    pthread_exit(NULL);
}

static void *worker(void *arg)
{
    assert(ios_native_publish(arg, NULL));
    assert(ios_native_bind_current((void *)1));
    assert(ios_native_ready_current((void *)2));
    atomic_store(&ready, 1);
    while (!atomic_load(&enter_lock)) {}
    struct ios_native_lifetime_stats stats;
    ios_native_lifetime_get_stats(&stats);
    return NULL;
}

int main(int argc, char **argv)
{
    alarm(20);
    startup_failure = argc > 1 && !strcmp(argv[1], "startup");
    if (startup_failure) {
        assert(!ios_native_process_reserve(complete));
        assert(!native_available && !native_process_count && !native_thread_count);
        puts("PASS observer fails before pthread_create returns: admission remains closed");
        return 0;
    }
    struct sigaction action = {0};
    action.sa_handler = quit;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGQUIT, &action, NULL));
    struct ios_native_reservation *entry = ios_native_process_reserve(complete);
    assert(entry && !pthread_create(&target, NULL, worker, entry));
    while (!atomic_load(&ready)) usleep(1000);
    atomic_store(&enter_lock, 1);
    while (!atomic_load(&inside_lock)) usleep(1000);
    assert(!__pthread_kill(pthread_mach_thread_np(target), SIGQUIT));
    assert(!atomic_load(&signals) && !atomic_load(&completed));
    atomic_store(&release_lock, 1);
    assert(!pthread_join(target, NULL));
    for (int i = 0; i < 2000 && !atomic_load(&completed); i++) usleep(5000);
    assert(atomic_load(&signals) == 1 && atomic_load(&completed) == 1);
    puts("PASS SIGQUIT queued inside real core mutex: delivered after unlock; TLS cleanup does not deadlock");
    return 0;
}
