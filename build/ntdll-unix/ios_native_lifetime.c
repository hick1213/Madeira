/* A pinned Mach endpoint dies after native pthread/TLS teardown. Windows exit,
 * a TEB cleanup callback, signal delivery and elapsed time do not prove this.
 * No pthread join ownership is taken here. */
#include "ios_native_lifetime.h"
#include <mach/mach.h>
#include <mach/notify.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef IOS_NATIVE_MAX_THREADS
#define IOS_NATIVE_MAX_THREADS 4096
#endif
#ifndef IOS_NATIVE_MAX_PROCESSES
#define IOS_NATIVE_MAX_PROCESSES 64 /* bounded by Wine's process/socket identity tables */
#endif

struct native_process {
    struct native_process *next, *session;
    uint64_t generation;
    void *peb;
    ios_native_reclaim_fn reclaim;
    unsigned threads, reservations, consumers, children;
    int retiring, collecting, closing_session;
};

struct ios_native_reservation {
    struct ios_native_reservation *next;
    struct native_process *owner;
    mach_port_t endpoint;
    void *teb;
    int published, notification, ready, quitting, bootstrap;
};

static pthread_mutex_t native_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t native_once = PTHREAD_ONCE_INIT;
static pthread_key_t native_key;
static int native_available;
static mach_port_t native_events;
static struct native_process *native_processes;
static struct ios_native_reservation *native_threads;
static uint64_t native_next_generation, native_completed;
static unsigned native_process_count, native_thread_count, native_rights;
static _Thread_local struct ios_native_reservation *native_current;

/* A pending SIGQUIT can run pthread/TLS teardown at mask restoration. Never
 * restore it while native_lock is held: the cleanup hint takes this same lock. */
static void native_lock_enter(sigset_t *previous)
{
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGQUIT);
    pthread_sigmask(SIG_BLOCK, &mask, previous);
    pthread_mutex_lock(&native_lock);
}

static void native_lock_leave(const sigset_t *previous)
{
    pthread_mutex_unlock(&native_lock);
    pthread_sigmask(SIG_SETMASK, previous, NULL);
}

extern int __pthread_kill(mach_port_t, int);
extern void ios_thread_registry_native_dead(mach_port_t, void *, uint64_t);

/* The kernel notification is the wakeup. This message only wakes the observer
 * after changes that need no thread death (a failed create or final lease). */
static void native_wake(void)
{
    mach_msg_header_t msg = {0};
    msg.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_MAKE_SEND, 0);
    msg.msgh_size = sizeof(msg);
    msg.msgh_remote_port = native_events;
    msg.msgh_id = 1;
    (void)mach_msg(&msg, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(msg), 0,
                   MACH_PORT_NULL, 0, MACH_PORT_NULL);
}

static void native_request_locked(struct native_process *owner)
{
    struct ios_native_reservation *entry;
    owner->retiring = 1;
    for (entry = native_threads; entry; entry = entry->next) {
        if (entry->owner != owner || !entry->published || !entry->ready || entry->quitting)
            continue;
        /* The initiating thread must return/longjmp to its existing cleanup.
         * A successful signal is only a request; it never removes an entry. */
        if (entry == native_current) { entry->quitting = 1; continue; }
        int error = __pthread_kill(entry->endpoint, SIGQUIT);
        entry->quitting = 1;
        if (error)
            dprintf(2, "[native-life] gen=%llu port=%x SIGQUIT error=%d; retaining until death\n",
                    (unsigned long long)owner->generation, entry->endpoint, error);
    }
}

static void native_retire_locked(struct native_process *owner)
{
    struct native_process *child;
    native_request_locked(owner);
    if (owner->session == owner) {
        owner->closing_session = 1;
        for (child = native_processes; child; child = child->next)
            if (child != owner && child->session == owner) native_request_locked(child);
    }
}

static void native_cleanup_hint(void *value)
{
    sigset_t native_mask;
    struct ios_native_reservation *entry = value;
    /* A TLS destructor is NOT completion. Its entry and port stay retained
     * while later destructors can still call Wine/FEX code. */
    native_lock_enter(&native_mask);
    entry->quitting = 1;
    if (entry->bootstrap) native_retire_locked(entry->owner);
    native_lock_leave(&native_mask);
    native_wake();
}

static void native_remove_thread_locked(struct ios_native_reservation **link)
{
    struct ios_native_reservation *entry = *link;
    *link = entry->next;
    entry->owner->threads--;
    native_thread_count--;
    ios_thread_registry_native_dead(entry->endpoint, entry->teb, entry->owner->generation);
    mach_port_deallocate(mach_task_self(), entry->endpoint); /* our retained right */
    native_rights--;
    free(entry);
}

static void native_complete_event(mach_port_t endpoint)
{
    sigset_t native_mask;
    struct ios_native_reservation **link;
    native_lock_enter(&native_mask);
    for (link = &native_threads; *link; link = &(*link)->next) {
        if ((*link)->published && (*link)->endpoint == endpoint && (*link)->notification) {
            native_remove_thread_locked(link);
            break;
        }
    }
    /* MACH_NOTIFY_DEAD_NAME adds one dead-name reference for the message,
     * distinct from mach_thread_self's retained reference. */
    mach_port_deallocate(mach_task_self(), endpoint);
    native_lock_leave(&native_mask);
}

static void native_collect(void)
{
    sigset_t native_mask;
    struct native_process *owner, **link;
    for (;;) {
        native_lock_enter(&native_mask);
        /* Notification setup failure is not death. Only a successful query
         * for our still-retained exact right may prove a dead name. */
        struct ios_native_reservation **thread = &native_threads;
        while (*thread) {
            mach_port_type_t type = 0;
            if ((*thread)->published && !(*thread)->notification &&
                mach_port_type(mach_task_self(), (*thread)->endpoint, &type) == KERN_SUCCESS &&
                (type & MACH_PORT_TYPE_DEAD_NAME))
                native_remove_thread_locked(thread);
            else thread = &(*thread)->next;
        }
        for (owner = native_processes; owner; owner = owner->next)
            if (owner->retiring && !owner->collecting && !owner->threads &&
                !owner->reservations && !owner->consumers && !owner->children) break;
        if (!owner) { native_lock_leave(&native_mask); return; }
        owner->collecting = 1;
        native_lock_leave(&native_mask);
        /* Never enter Wine/virtual/pool locks with the lifetime lock held. The
         * record stays published, closed, and unreusable until this returns. */
        if (owner->reclaim) owner->reclaim(owner->peb, owner->generation);
        native_lock_enter(&native_mask);
        for (link = &native_processes; *link != owner; link = &(*link)->next) {}
        *link = owner->next;
        if (owner->session != owner) owner->session->children--;
        native_process_count--;
        native_completed++;
        native_lock_leave(&native_mask);
        free(owner);
    }
}

static void *native_observer(void *unused)
{
    sigset_t native_mask;
    (void)unused;
    for (;;) {
        union { mach_msg_header_t header; mach_dead_name_notification_t dead;
                unsigned char storage[256]; } msg;
        kern_return_t result = mach_msg(&msg.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT,
                                        0, sizeof(msg), native_events, 100, MACH_PORT_NULL);
        if (result == KERN_SUCCESS) {
            if (msg.header.msgh_id == MACH_NOTIFY_DEAD_NAME &&
                msg.header.msgh_size == offsetof(mach_dead_name_notification_t, trailer))
                native_complete_event(msg.dead.not_port);
            else mach_msg_destroy(&msg.header);
        } else if (result != MACH_RCV_TIMED_OUT && result != MACH_RCV_INTERRUPTED) {
            native_lock_enter(&native_mask);
            native_available = 0;
            native_lock_leave(&native_mask);
            dprintf(2, "[native-life] observer error=%d; admission closed, resources retained\n", result);
            return NULL;
        }
        native_collect();
    }
}

static void native_init(void)
{
    pthread_t observer;
    sigset_t native_mask;
    if (pthread_key_create(&native_key, native_cleanup_hint)) return;
    if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &native_events) != KERN_SUCCESS) {
        pthread_key_delete(native_key);
        return;
    }
    native_lock_enter(&native_mask);
    native_available = 1;
    native_lock_leave(&native_mask);
    if (pthread_create(&observer, NULL, native_observer, NULL)) {
        native_lock_enter(&native_mask);
        native_available = 0;
        native_lock_leave(&native_mask);
        mach_port_mod_refs(mach_task_self(), native_events, MACH_PORT_RIGHT_RECEIVE, -1);
        pthread_key_delete(native_key);
        return;
    }
    pthread_detach(observer);
}

struct ios_native_reservation *ios_native_process_reserve(ios_native_reclaim_fn reclaim)
{
    sigset_t native_mask;
    struct native_process *owner;
    struct ios_native_reservation *entry;
    pthread_once(&native_once, native_init);
    owner = calloc(1, sizeof(*owner));
    entry = calloc(1, sizeof(*entry));
    if (!owner || !entry) { free(owner); free(entry); return NULL; }
    native_lock_enter(&native_mask);
    if (!native_available || native_process_count == IOS_NATIVE_MAX_PROCESSES ||
        native_thread_count == IOS_NATIVE_MAX_THREADS || native_next_generation == UINT64_MAX ||
        (native_current && (native_current->owner->retiring ||
                            native_current->owner->session->closing_session))) {
        native_lock_leave(&native_mask);
        free(owner); free(entry); return NULL;
    }
    owner->generation = ++native_next_generation;
    owner->reclaim = reclaim;
    owner->session = native_current ? native_current->owner->session : owner;
    if (owner->session != owner) owner->session->children++;
    owner->reservations = 1;
    owner->next = native_processes;
    native_processes = owner;
    native_process_count++;
    entry->owner = owner;
    entry->bootstrap = 1;
    entry->next = native_threads;
    native_threads = entry;
    native_thread_count++;
    native_lock_leave(&native_mask);
    return entry;
}

struct ios_native_reservation *ios_native_thread_reserve(void *peb)
{
    sigset_t native_mask;
    struct ios_native_reservation *entry = calloc(1, sizeof(*entry));
    if (!entry) return NULL;
    native_lock_enter(&native_mask);
    /* Only the actual target process's native creation route may reserve. */
    if (!native_available || !native_current || native_current->owner->peb != peb ||
        native_current->owner->retiring || native_current->owner->session->closing_session ||
        native_thread_count == IOS_NATIVE_MAX_THREADS) {
        native_lock_leave(&native_mask); free(entry); return NULL;
    }
    entry->owner = native_current->owner;
    entry->owner->reservations++;
    entry->next = native_threads;
    native_threads = entry;
    native_thread_count++;
    native_lock_leave(&native_mask);
    return entry;
}

void ios_native_creation_failed(struct ios_native_reservation *entry)
{
    sigset_t native_mask;
    struct ios_native_reservation **link;
    if (!entry) return;
    native_lock_enter(&native_mask);
    if (entry->published) abort(); /* pthread_create success must never use this path */
    for (link = &native_threads; *link != entry; link = &(*link)->next) {}
    *link = entry->next;
    entry->owner->reservations--;
    if (entry->bootstrap) {
        entry->owner->retiring = 1;
        entry->owner->reclaim = NULL; /* no thread, PEB or Wine resources were created */
    }
    native_thread_count--;
    native_lock_leave(&native_mask);
    free(entry);
    native_wake();
}

int ios_native_publish(struct ios_native_reservation *entry, void *teb)
{
    sigset_t native_mask;
    mach_port_t previous = MACH_PORT_NULL;
    kern_return_t result;
    if (!entry || native_current) return 0;
    native_lock_enter(&native_mask);
    entry->endpoint = mach_thread_self(); /* this core's own send reference */
    if (!MACH_PORT_VALID(entry->endpoint)) {
        native_lock_leave(&native_mask);
        return 0; /* unresolved reservation retains the generation */
    }
    native_rights++;
    entry->teb = teb;
    entry->published = 1;
    entry->owner->reservations--;
    entry->owner->threads++;
    native_current = entry;
    result = mach_port_request_notification(mach_task_self(), entry->endpoint,
                 MACH_NOTIFY_DEAD_NAME, 0, native_events, MACH_MSG_TYPE_MAKE_SEND_ONCE, &previous);
    entry->notification = result == KERN_SUCCESS;
    if (MACH_PORT_VALID(previous)) mach_port_deallocate(mach_task_self(), previous);
    int key_error = pthread_setspecific(native_key, entry);
    if (key_error || result != KERN_SUCCESS) {
        entry->quitting = 1;
        if (entry->bootstrap) native_retire_locked(entry->owner);
        dprintf(2, "[native-life] gen=%llu publication failed notification=%d tls=%d; PE entry refused\n",
                (unsigned long long)entry->owner->generation, result, key_error);
    }
    native_lock_leave(&native_mask);
    native_wake();
    return !key_error && result == KERN_SUCCESS;
}

int ios_native_bind_current(void *peb)
{
    sigset_t native_mask;
    struct native_process *other;
    int ok = 0;
    native_lock_enter(&native_mask);
    if (native_current && peb && !native_current->owner->retiring) {
        for (other = native_processes; other; other = other->next)
            if (other != native_current->owner && other->peb == peb) break;
        if (!other && (!native_current->owner->peb || native_current->owner->peb == peb)) {
            native_current->owner->peb = peb;
            ok = 1;
        }
    }
    native_lock_leave(&native_mask);
    return ok;
}

int ios_native_ready_current(void *teb)
{
    sigset_t native_mask;
    int ok = 0;
    native_lock_enter(&native_mask);
    if (native_current && native_current->owner->peb &&
        (!native_current->teb || native_current->teb == teb)) {
        native_current->teb = teb;
        native_current->ready = 1;
        ok = !native_current->owner->retiring;
    }
    native_lock_leave(&native_mask);
    return ok;
}

uint64_t ios_native_current_generation(void)
{
    return native_current ? native_current->owner->generation : 0;
}

void ios_native_retire_current(void)
{
    sigset_t native_mask;
    native_lock_enter(&native_mask);
    if (native_current) native_retire_locked(native_current->owner);
    native_lock_leave(&native_mask);
    native_wake();
}

int ios_native_retire_process(void *peb)
{
    sigset_t native_mask;
    int ok = 0;
    native_lock_enter(&native_mask);
    if (native_current && native_current->owner->peb == peb) {
        /* Logical exit of a launcher must not kill its live game children.
         * The bridge closes the complete session after its child wait. */
        native_request_locked(native_current->owner);
        ok = 1;
    }
    native_lock_leave(&native_mask);
    if (ok) native_wake();
    else dprintf(2, "[native-life] untracked retirement peb=%p refused; resources retained\n", peb);
    return ok;
}

uint64_t ios_native_consumer_acquire(void *peb)
{
    sigset_t native_mask;
    uint64_t generation = 0;
    struct native_process *owner;
    native_lock_enter(&native_mask);
    for (owner = native_processes; owner; owner = owner->next)
        if (peb && owner->peb == peb && !owner->retiring) {
            owner->consumers++;
            generation = owner->generation;
            break;
        }
    native_lock_leave(&native_mask);
    return generation;
}

void ios_native_consumer_release(uint64_t generation)
{
    sigset_t native_mask;
    struct native_process *owner;
    native_lock_enter(&native_mask);
    for (owner = native_processes; owner; owner = owner->next)
        if (owner->generation == generation) {
            if (!owner->consumers) abort();
            owner->consumers--;
            break;
        }
    native_lock_leave(&native_mask);
    native_wake();
}

void ios_native_lifetime_get_stats(struct ios_native_lifetime_stats *stats)
{
    sigset_t native_mask;
    struct native_process *owner;
    memset(stats, 0, sizeof(*stats));
    native_lock_enter(&native_mask);
    stats->generations = native_process_count;
    stats->port_rights = native_rights;
    stats->completed = native_completed;
    for (owner = native_processes; owner; owner = owner->next) {
        stats->reservations += owner->reservations;
        stats->threads += owner->threads;
        stats->consumers += owner->consumers;
        stats->retiring += owner->retiring;
    }
    native_lock_leave(&native_mask);
}
