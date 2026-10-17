// WineServerBridge.m - Run Wine's wineserver as a thread on iOS
// This bridges the wineserver (compiled as a static library) into the iOS app.

#import <Foundation/Foundation.h>
#import <os/log.h>
#import <sys/stat.h>
#import <pthread.h>
#import <stdarg.h>

#include "WineServerBridge.h"
#include "RuntimeFonts.h"
#include <errno.h>
#include <sys/time.h>

static FILE *g_ws_bridge_log = NULL;
static pthread_mutex_t g_ws_bridge_log_mutex = PTHREAD_MUTEX_INITIALIZER;

static void wine_log_msg_impl(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void wine_log_msg_impl(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    char buf[1024];
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    os_log(OS_LOG_DEFAULT, "%s", buf);
    extern void wine_ui_log(const char *message);
    wine_ui_log(buf);
    pthread_mutex_lock(&g_ws_bridge_log_mutex);
    if (g_ws_bridge_log) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        struct tm tm;
        localtime_r(&tv.tv_sec, &tm);
        fprintf(g_ws_bridge_log, "[%02d:%02d:%02d.%03d] %s\n",
                tm.tm_hour, tm.tm_min, tm.tm_sec, (int)(tv.tv_usec/1000), buf);
        fflush(g_ws_bridge_log);
    }
    pthread_mutex_unlock(&g_ws_bridge_log_mutex);
}

#define wine_log_msg(fmt, ...) wine_log_msg_impl("[Wine] " fmt, ##__VA_ARGS__)

// The wineserver's main() renamed to wineserver_main()
extern int wineserver_main(int argc, char *argv[]);

// File logging for wineserver C code (defined in wine_log_ios.h, linked from libwineserver.a)
extern void wineserver_log_set_file(const char *path);

// Set NLS directory for wineserver (defined in unicode_ios.c)
extern void wineserver_set_nls_dir(const char *path);

// Override wineserver's fatal_error to use logging and pthread_exit instead of exit(1)
void fatal_error( const char *err, ... ) {
    va_list args;
    char buf[1024];
    va_start(args, err);
    vsnprintf(buf, sizeof(buf), err, args);
    va_end(args);
    wine_log_msg_impl("[Wine] FATAL: %s", buf);
    // Don't call exit(1) — that kills the whole app.
    // Instead, terminate just this thread.
    pthread_exit(NULL);
}

// Wineserver globals we need to set before calling main
extern int foreground;
extern int debug_level;

// Stop flag checked by wineserver event loop (fd_ios.c)
volatile int g_wineserver_should_stop = 0;

static void *wineserver_thread_body(void *arg) {
    const char *prefix_path = arg;
    @autoreleasepool {
        // Set up file-based logging
        NSString *docs = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES).firstObject;
        NSString *logPath = [docs stringByAppendingPathComponent:@"madeira-log.txt"];
        pthread_mutex_lock(&g_ws_bridge_log_mutex);
        if (g_ws_bridge_log) fclose(g_ws_bridge_log);
        g_ws_bridge_log = fopen(logPath.UTF8String, "a");
        pthread_mutex_unlock(&g_ws_bridge_log_mutex);
        // Also set up logging for wineserver C code (main_ios.c, fd_ios.c, request_ios.c)
        wineserver_log_set_file(logPath.UTF8String);

        // Redirect wineserver stderr to log file
        {
            int logfd = open(logPath.UTF8String, O_WRONLY | O_CREAT | O_APPEND, 0644);
            if (logfd >= 0) {
                dup2(logfd, STDERR_FILENO);
                close(logfd);
            }
        }

        // Test that ws_log works from here
        extern void ws_log(const char *fmt, ...);
        ws_log("[wineserver-bridge] ws_log test - log file set up OK");

        wine_log_msg("Wineserver thread started");

        // Set up environment for wineserver
        setenv("WINEPREFIX", prefix_path, 1);
        setenv("HOME", prefix_path, 1);  // Fallback if WINEPREFIX not used

        wine_log_msg("WINEPREFIX=%s", prefix_path);

        // Create the Wine prefix directory if it doesn't exist
        mkdir(prefix_path, 0755);

        // Tell wineserver where NLS files are (inside the app bundle)
        NSString *nlsDir = [NSBundle.mainBundle.bundlePath stringByAppendingPathComponent:@"nls"];
        wineserver_set_nls_dir(nlsDir.UTF8String);
        wine_log_msg("NLS dir=%s", nlsDir.UTF8String);

        // wineserver_main sets foreground=1 based on args, but we set it directly
        foreground = 1;

        // Call wineserver's main function with minimal args
        // No --debug: debug output floods stderr pipe, blocking the main_loop
        char *argv[] = { "wineserver", "--foreground", NULL };
        int argc = 2;

        wine_log_msg("Calling wineserver_main...");
        int ret = wineserver_main(argc, argv);
        wine_log_msg("wineserver_main returned: %d", ret);

    }
    return NULL;
}

static void madeira_prepare_cjk_fonts(const char *prefix_path) {
    @autoreleasepool {
        // This is the sole offline registry window. The prefix seed is also
        // called later by Wine, so font migration must remain here, before
        // this owned wineserver thread loads system.reg.
        NSError *fontError = nil;
        NSString *fontResources = [[[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"cjk-fonts"] stringByResolvingSymlinksInPath];
        NSString *fontPrefix = [[NSString stringWithUTF8String:prefix_path] stringByStandardizingPath];
        NSString *home = [NSHomeDirectory() stringByStandardizingPath];
        if ([fontPrefix hasPrefix:[home stringByAppendingString:@"/"]]) {
            // Canonicalize only trusted container ancestry (/var on iOS),
            // retaining prefix components for the helper's no-symlink checks.
            fontPrefix = [[home stringByResolvingSymlinksInPath] stringByAppendingPathComponent:
                [fontPrefix substringFromIndex:home.length + 1]];
        }
        if (!madeira_seed_cjk_fonts(fontPrefix.fileSystemRepresentation, fontResources.fileSystemRepresentation,
                "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b", &fontError)) {
            wine_log_msg("[fonts-cjk] preparation failed: %s", fontError.localizedDescription.UTF8String ?: "unknown error");
        } else {
            wine_log_msg("[fonts-cjk] Noto Sans CJK SC font and fallback ready");
        }
    }
}

/* IOS_WINESERVER_LIFECYCLE_BEGIN */
static pthread_mutex_t g_wineserver_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_wineserver_cond = PTHREAD_COND_INITIALIZER;
static pthread_t g_wineserver_thread;
static int g_wineserver_thread_valid;
static int g_wineserver_starting;
static int g_wineserver_joining;
static int g_wineserver_running;
static unsigned long long g_wineserver_generation;

extern int wineserver_ready;
extern void ios_wineserver_wake(void);

static void wineserver_thread_cleanup(void *arg) {
    free(arg); /* one immutable prefix allocation owned by this session */
    pthread_mutex_lock(&g_wineserver_mutex);
    __atomic_store_n(&wineserver_ready, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&g_wineserver_running, 0, __ATOMIC_RELEASE);
    pthread_cond_broadcast(&g_wineserver_cond);
    pthread_mutex_unlock(&g_wineserver_mutex);
}

static void *wineserver_thread_func(void *arg) {
    /* fatal_error uses pthread_exit, so normal-return-only cleanup is not
     * enough. Joinability remains until exactly one owner reaps the thread. */
    pthread_cleanup_push(wineserver_thread_cleanup, arg);
    wineserver_thread_body(arg);
    pthread_cleanup_pop(1);
    return NULL;
}

/* The caller owns g_wineserver_joining. Never hold the lifecycle mutex during
 * pthread_join: the finishing thread needs it to publish its cleanup. */
static int wineserver_finish_join(pthread_t thread, unsigned long long generation) {
    wine_log_msg("Joining wineserver thread (generation %llu, running=%d ready=%d)",
                 generation,
                 __atomic_load_n(&g_wineserver_running, __ATOMIC_ACQUIRE),
                 __atomic_load_n(&wineserver_ready, __ATOMIC_ACQUIRE));
    int error = pthread_join(thread, NULL);
    pthread_mutex_lock(&g_wineserver_mutex);
    if (g_wineserver_generation == generation) {
        if (!error) {
            g_wineserver_thread_valid = 0;
            g_wineserver_thread = (pthread_t)0;
            __atomic_store_n(&wineserver_ready, 0, __ATOMIC_RELEASE);
            __atomic_store_n(&g_wineserver_running, 0, __ATOMIC_RELEASE);
        }
        /* A failed join retains ownership of the joinable thread; a later
         * background call may retry, but no new server can overwrite it. */
        g_wineserver_joining = 0;
    }
    pthread_cond_broadcast(&g_wineserver_cond);
    pthread_mutex_unlock(&g_wineserver_mutex);
    if (error) wine_log_msg("Wineserver join failed: %d", error);
    else wine_log_msg("Wineserver thread joined (generation %llu)", generation);
    return error ? -1 : 0;
}

int wineserver_start(const char *prefix_path) {
    char *owned_prefix;
    unsigned long long generation;
    int main_thread = pthread_main_np();
    if (!prefix_path || !*prefix_path) return -1;
    pthread_mutex_lock(&g_wineserver_mutex);
    for (;;) {
        if (g_wineserver_thread_valid && pthread_equal(g_wineserver_thread, pthread_self())) {
            int active = !g_wineserver_joining &&
                         !__atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE);
            pthread_mutex_unlock(&g_wineserver_mutex);
            return active ? 0 : -1; /* never wait on an owner joining ourselves */
        }
        if (g_wineserver_starting || g_wineserver_joining) {
            if (main_thread) {
                pthread_mutex_unlock(&g_wineserver_mutex);
                return -1; /* UIKit must never wait for a previous session */
            }
            pthread_cond_wait(&g_wineserver_cond, &g_wineserver_mutex);
            continue;
        }
        if (!g_wineserver_thread_valid) break;
        if (__atomic_load_n(&g_wineserver_running, __ATOMIC_ACQUIRE) &&
            !__atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE)) {
            pthread_mutex_unlock(&g_wineserver_mutex);
            return 0;
        }
        if (main_thread || pthread_equal(g_wineserver_thread, pthread_self())) {
            pthread_mutex_unlock(&g_wineserver_mutex);
            return -1;
        }
        pthread_t previous = g_wineserver_thread;
        unsigned long long previous_generation = g_wineserver_generation;
        g_wineserver_joining = 1;
        pthread_mutex_unlock(&g_wineserver_mutex);
        if (wineserver_finish_join(previous, previous_generation)) return -1;
        pthread_mutex_lock(&g_wineserver_mutex);
    }
    owned_prefix = strdup(prefix_path);
    if (!owned_prefix) {
        pthread_mutex_unlock(&g_wineserver_mutex);
        return -1;
    }
    g_wineserver_starting = 1;
    generation = ++g_wineserver_generation;
    __atomic_store_n(&g_wineserver_should_stop, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&wineserver_ready, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&g_wineserver_running, 1, __ATOMIC_RELEASE);
    pthread_mutex_unlock(&g_wineserver_mutex);
    wine_log_msg("Starting wineserver with prefix: %s", prefix_path);

    /* ml588: seed the prefix BEFORE the server thread starts. init_registry()
     * runs within milliseconds of the thread launching, and if system.reg is
     * not on disk by then the server builds an empty registry and its first
     * save overwrites the template's (ml587: 17,479 keys -> 24). Seeding is
     * idempotent, so this costs one stat() on every launch after the first. */
    {
        extern void madeira_seed_prefix_if_needed(const char *prefix_path);
        madeira_seed_prefix_if_needed(owned_prefix);
        madeira_prepare_cjk_fonts(owned_prefix);
    }

    /* 2026-07-04 perf: the wineserver thread used to be created at LOWERED
     * priority (sched_priority 20, "so wineserver doesn't starve the main
     * thread"). That made every client server-request round trip wait on a
     * low-priority, E-core-eligible thread — [PROF] showed the game thread
     * parked in read_reply_data (waiting for wineserver replies) for most
     * of each 55ms frame while game threads run USER_INTERACTIVE: textbook
     * priority inversion. The server is on the critical path of every
     * object wait / message pump call — it must run AT LEAST as hot as its
     * clients. */
    pthread_attr_t attr;
    int ret = pthread_attr_init(&attr);
    int attr_initialized = !ret;
    if (!ret) pthread_attr_set_qos_class_np(&attr, QOS_CLASS_USER_INTERACTIVE, 0);
    pthread_mutex_lock(&g_wineserver_mutex);
    /* stop() may have arrived during prefix seeding. Do not create a new
     * thread after that request; its background caller is waiting on starting. */
    if (!ret && !__atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE)) {
        pthread_t thread = (pthread_t)0;
        ret = pthread_create(&thread, &attr, wineserver_thread_func, owned_prefix);
        if (!ret) {
            g_wineserver_thread = thread;
            g_wineserver_thread_valid = 1;
        }
    } else if (!ret) {
        ret = ECANCELED;
    }
    g_wineserver_starting = 0;
    if (ret) {
        __atomic_store_n(&wineserver_ready, 0, __ATOMIC_RELEASE);
        __atomic_store_n(&g_wineserver_running, 0, __ATOMIC_RELEASE);
    }
    pthread_cond_broadcast(&g_wineserver_cond);
    pthread_mutex_unlock(&g_wineserver_mutex);
    /* attr is initialized unless pthread_attr_init itself failed. */
    if (attr_initialized) pthread_attr_destroy(&attr);
    if (ret) {
        free(owned_prefix);
        wine_log_msg("Failed to create wineserver thread: %d", ret);
        return -1;
    }
    wine_log_msg("Wineserver thread created (generation %llu)", generation);
    return 0;
}

int wineserver_is_running(void) {
    return __atomic_load_n(&g_wineserver_running, __ATOMIC_ACQUIRE);
}

int wineserver_is_ready(void) {
    return wineserver_is_running() &&
           !__atomic_load_n(&g_wineserver_should_stop, __ATOMIC_ACQUIRE) &&
           __atomic_load_n(&wineserver_ready, __ATOMIC_ACQUIRE);
}

void wineserver_stop(void) {
    unsigned long long generation;
    pthread_t thread;
    wine_log_msg("Wineserver stop requested");
    pthread_mutex_lock(&g_wineserver_mutex);
    if (!g_wineserver_starting && !g_wineserver_thread_valid) {
        pthread_mutex_unlock(&g_wineserver_mutex);
        return;
    }
    generation = g_wineserver_generation;
    __atomic_store_n(&g_wineserver_should_stop, 1, __ATOMIC_RELEASE);
    __atomic_store_n(&wineserver_ready, 0, __ATOMIC_RELEASE);
    if (pthread_main_np() ||
        (g_wineserver_thread_valid && pthread_equal(g_wineserver_thread, pthread_self()))) {
        pthread_mutex_unlock(&g_wineserver_mutex);
        ios_wineserver_wake();
        return; /* signal-only on UIKit/the server itself; no blocking join */
    }
    while (g_wineserver_starting && generation == g_wineserver_generation)
        pthread_cond_wait(&g_wineserver_cond, &g_wineserver_mutex);
    if (generation != g_wineserver_generation || !g_wineserver_thread_valid) {
        pthread_mutex_unlock(&g_wineserver_mutex);
        return;
    }
    if (g_wineserver_joining) {
        while (g_wineserver_joining && generation == g_wineserver_generation)
            pthread_cond_wait(&g_wineserver_cond, &g_wineserver_mutex);
        pthread_mutex_unlock(&g_wineserver_mutex);
        return; /* another stop/start owns this exact generation's join */
    }
    g_wineserver_joining = 1;
    thread = g_wineserver_thread;
    pthread_mutex_unlock(&g_wineserver_mutex);
    ios_wineserver_wake();
    wineserver_finish_join(thread, generation);
}
/* IOS_WINESERVER_LIFECYCLE_END */
