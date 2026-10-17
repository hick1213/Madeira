// WineServerBridge.h - Run Wine's wineserver as a thread on iOS
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Start the wineserver on a background thread.
// prefix_path: absolute path to the Wine prefix directory (e.g. app's Documents/wine)
// Returns 0 on success, -1 on failure/busy. Restart may reap a prior thread;
// callers that need to wait must use a background thread. UIKit never joins.
int wineserver_start(const char *prefix_path);

// Check if wineserver is starting/running; false once its thread has exited.
int wineserver_is_running(void);
/* 1 once the server has finished starting up (registry loaded), just before
 * its main loop: the point from which Wine may start. */
int wineserver_is_ready(void);

// Request stop; a background caller joins once. UIKit/the server itself only
// signal and return; a later background stop/start reaps the joinable thread.
void wineserver_stop(void);

// iOS socketpair bypass: inject a pre-connected client fd into the wineserver.
// Called from the app bridge after socketpair() — the wineserver event loop
// picks this up and calls create_process/create_thread on it.
void wineserver_inject_client_fd(int fd);

// Suppress os_log output from wineserver (file logging continues).
// Prevents os_log buffer contention from blocking the main thread.
extern volatile int ws_log_quiet;

#ifdef __cplusplus
}
#endif
