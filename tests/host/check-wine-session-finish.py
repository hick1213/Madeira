#!/usr/bin/env python3
"""Execute actual Wine finalization and explicit native-completion boundaries."""

from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError(
        "Run without Python optimization; lifecycle assertions are required."
    )

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "app/Madeira/WineProcessBridge.m").read_text()


def function(signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


finalize = function("static void wine_process_finish_session(")
complete = function("static void wine_process_native_complete(")
state_start = source.index("static _Thread_local int wine_session_finish_started")
finish_state = source[state_start:source.index(";", state_start) + 1]

code = (
    r"""
#define _DARWIN_C_SOURCE
#include <assert.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>
static volatile int g_wine_running = 1;
static int retired, stopped, tls_cleared, jit_finished, epic_ended, native_dead;
static int fixture_peb;
static const uint64_t fixture_generation = 42;
int wine_process_is_running(void);
static void assert_quit_blocked(void) {
    sigset_t current;
    assert(!pthread_sigmask(SIG_SETMASK, NULL, &current));
    assert(sigismember(&current, SIGQUIT) == 1);
}
static void assert_same_mask(const sigset_t *expected) {
    sigset_t current;
    assert(!pthread_sigmask(SIG_SETMASK, NULL, &current));
    for (int signal = 1; signal < NSIG; signal++)
        assert(sigismember(&current, signal) == sigismember(expected, signal));
}
static void ios_native_retire_current(void) {
    assert(wine_process_is_running());
    assert(!retired && !stopped && !tls_cleared && !epic_ended && !native_dead);
    assert_quit_blocked();
    retired++;
}
static void wineserver_stop(void) {
    assert(wine_process_is_running());
    assert(retired == 1 && !stopped && !tls_cleared && !epic_ended);
    assert_quit_blocked();
    stopped++;
}
static void wine_process_clear_foreign_teb_slot(void) {
    assert(wine_process_is_running());
    assert(stopped == 1 && !tls_cleared && !epic_ended);
    assert_quit_blocked();
    tls_cleared++;
}
void ios_jit_finish_process(void *peb, uint64_t generation) {
    assert(wine_process_is_running());
    assert(native_dead && retired == 1 && stopped == 1 && tls_cleared == 1);
    assert(!jit_finished && !epic_ended);
    assert(peb == &fixture_peb && generation == fixture_generation);
    jit_finished++;
}
static void madeira_epic_end_log(void) {
    assert(wine_process_is_running());
    assert(native_dead && jit_finished == 1 && !epic_ended);
    epic_ended++;
}
"""
    + finish_state
    + finalize
    + complete
    + function("int wine_process_is_running(")
    + r"""
static void *finish_thread(void *arg) {
    const sigset_t *inherited = arg;
    assert_same_mask(inherited);
    assert(!wine_session_finish_started);
    for (int call = 0; call < 2; call++) {
        wine_process_finish_session();
        assert(wine_session_finish_started);
        assert(retired == 1 && stopped == 1 && tls_cleared == 1);
        assert(!jit_finished && !epic_ended && wine_process_is_running());
        assert_same_mask(inherited);
    }
    return NULL;
}
int main(void) {
    sigset_t original, inherited;
    assert(!pthread_sigmask(SIG_SETMASK, NULL, &original));
    for (int mask = 0; mask < 4; mask++) {
        inherited = original;
        assert(!sigdelset(&inherited, SIGQUIT));
        assert(!sigdelset(&inherited, SIGUSR1));
        if (mask & 1) assert(!sigaddset(&inherited, SIGQUIT));
        if (mask & 2) assert(!sigaddset(&inherited, SIGUSR1));
        assert(!pthread_sigmask(SIG_SETMASK, &inherited, NULL));
        retired = stopped = tls_cleared = jit_finished = epic_ended = native_dead = 0;
        __atomic_store_n(&g_wine_running, 1, __ATOMIC_RELEASE);
        pthread_t thread;
        assert(!pthread_create(&thread, NULL, finish_thread, &inherited));
        assert(!pthread_join(thread, NULL));
        assert(wine_process_is_running() && !jit_finished && !epic_ended);
        /* The lifetime observer is an external boundary in this fixture.
         * Explicitly deliver completion only after the native thread exits. */
        native_dead = 1;
        wine_process_native_complete(&fixture_peb, fixture_generation);
        assert(jit_finished == 1 && epic_ended == 1 && !wine_process_is_running());
        assert_same_mask(&inherited);
    }
    assert(!pthread_sigmask(SIG_SETMASK, &original, NULL));
    puts("PASS: real finalization cleans once, restores inherited masks, and retains running until native completion ends the log");
    return 0;
}
"""
)
with tempfile.TemporaryDirectory(prefix="madeira-wine-finish-") as td:
    c = Path(td) / "check.c"
    binary = Path(td) / "check"
    c.write_text(code)
    for flags in [
        ["-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-g"],
        ["-O2"],
    ]:
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                *flags,
                str(c),
                "-o",
                str(binary),
            ],
            check=True,
            timeout=60,
        )
        subprocess.run([str(binary)], check=True, timeout=30)
