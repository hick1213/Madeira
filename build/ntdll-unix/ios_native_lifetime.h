/* Native completion, independent of Wine's logical process/thread handles. */
#ifndef IOS_NATIVE_LIFETIME_H
#define IOS_NATIVE_LIFETIME_H

#include <stdint.h>

struct ios_native_reservation;
typedef void (*ios_native_reclaim_fn)(void *peb, uint64_t generation);

/* Reserve before pthread_create. NULL rejects creation; only the creator may
 * cancel, and only when pthread_create failed (no native thread exists).
 * A Wine creator must keep SIGQUIT blocked across reserve -> create/cancel so
 * native exit cannot abandon a reservation before ownership transfers. */
struct ios_native_reservation *ios_native_process_reserve(ios_native_reclaim_fn reclaim);
struct ios_native_reservation *ios_native_thread_reserve(void *peb);
void ios_native_creation_failed(struct ios_native_reservation *reservation);
/* Called by the new native thread before it enters any child-owned code. */
int ios_native_publish(struct ios_native_reservation *reservation, void *teb);
int ios_native_bind_current(void *peb);
int ios_native_ready_current(void *teb);
uint64_t ios_native_current_generation(void);
void ios_native_retire_current(void);
int ios_native_retire_process(void *peb);
/* Foreign-thread callback leases; a closed/missing generation rejects entry.
 * Keep SIGQUIT blocked across acquisition -> callback -> release, with a
 * pthread cleanup handler if the callback can exit its native caller. */
uint64_t ios_native_consumer_acquire(void *peb);
void ios_native_consumer_release(uint64_t generation);

struct ios_native_lifetime_stats {
    unsigned generations, reservations, threads, consumers, retiring, port_rights;
    uint64_t completed;
};
void ios_native_lifetime_get_stats(struct ios_native_lifetime_stats *stats);

#endif
