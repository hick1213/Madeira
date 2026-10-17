#!/usr/bin/env python3
"""Exercise production virtual-display mode state with native Wine ABI types.

Compile the real screen-size, enumeration, name-validation, publish, and change
functions. Only UI/server notifications, synchronization gates, and 16-bit WCHAR
CRT helpers are host boundaries. Each scenario gets fresh process state and never touches a registry,
game, device, or persistent configuration. A failing contract exits nonzero.
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw
"""

from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "build/win32u-unix/sysparams_ios.c"
SCRATCH_PARENT = ROOT / "app/Build/Products/IPAInspection/future-display-probes"


def function(source: str, signature: str) -> str:
    """Extract a production definition, skipping any preceding declaration."""
    start = source.index(signature)
    while source.index(";", start) < source.index("{", start):
        start = source.index(signature, start + len(signature))
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


PRELUDE = r'''
#include <ctype.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <windef.h>
#include <winbase.h>
#include <wingdi.h>
#include <winuser.h>
#include <winternl.h>
#define WINE_ENUM_PHYSICAL_SETTINGS ((DWORD)-3)
#define WM_WINE_CLIPCURSOR 0x80001ff0u
#define SET_CURSOR_FSCLIP 0x20 /* wine/server_protocol.h */
static _Atomic unsigned int failures, checks;
static unsigned int cache_updates, work_invalidations, clip_resets;
static unsigned int ui_updates, desktop_messages, broadcast_messages, cursor_messages;
static int ui_width, ui_height;
static WPARAM broadcast_bpp;
static LPARAM broadcast_dimensions;
static pthread_mutex_t gate_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_condition = PTHREAD_COND_INITIALIZER;
static BOOL gate_enabled, gate_entered, gate_released;
static void (*broadcast_hook)(void);
static void require(BOOL ok, const char *contract)
{
    checks++;
    if (!ok) { fprintf(stderr, "FAIL: %s\n", contract); failures++; }
}
/* Host wchar_t is 32-bit, Wine WCHAR is 16-bit. Implement only the ASCII
 * CRT operations used by production device-name parsing, not mode logic. */
static int fixture_wcsnicmp(const WCHAR *a, const WCHAR *b, size_t count)
{
    size_t i;
    for (i = 0; i < count; i++) {
        int x = tolower((unsigned char)a[i]), y = tolower((unsigned char)b[i]);
        if (x != y) return x - y;
        if (!a[i]) return 0;
    }
    return 0;
}
static unsigned long fixture_wcstoul(const WCHAR *s, WCHAR **end, int base)
{
    const WCHAR *p = s;
    unsigned long value = 0;
    require(base == 10, "device index parser uses decimal");
    while (*p >= '0' && *p <= '9') value = value * 10 + *p++ - '0';
    *end = (WCHAR *)p;
    return value;
}
#define wcsnicmp fixture_wcsnicmp
#define wcstoul fixture_wcstoul
void WINAPI RtlSetLastWin32Error(ULONG error) { (void)error; }
static void update_display_cache(BOOL force)
{
    require(force, "cache forced on publication"); cache_updates++;
    pthread_mutex_lock(&gate_lock);
    if (gate_enabled && !gate_entered) {
        gate_entered = TRUE;
        pthread_cond_broadcast(&gate_condition);
        while (!gate_released) pthread_cond_wait(&gate_condition, &gate_lock);
    }
    pthread_mutex_unlock(&gate_lock);
}
static void ios_invalidate_work_area(void) { work_invalidations++; }
static BOOL NtUserClipCursor(const RECT *rect) { require(!rect, "clip reset uses NULL"); clip_resets++; return TRUE; }
static HWND get_desktop_window(void) { return (HWND)(UINT_PTR)0x10001; }
static HWND NtUserGetForegroundWindow(void) { return (HWND)(UINT_PTR)0x10002; }
static BOOL send_notify_message(HWND window, UINT message, WPARAM wparam, LPARAM lparam, BOOL ansi)
{
    require(window == get_desktop_window() && message == WM_DISPLAYCHANGE && !ansi && wparam == 32,
            "desktop display-change notification");
    (void)lparam; desktop_messages++; return TRUE;
}
static LRESULT send_message_timeout(HWND window, UINT message, WPARAM wparam, LPARAM lparam,
                                    UINT flags, UINT timeout, BOOL ansi)
{
    require(window == HWND_BROADCAST && message == WM_DISPLAYCHANGE &&
            flags == SMTO_ABORTIFHUNG && timeout == 2000 && !ansi,
            "bounded display-change broadcast");
    broadcast_messages++; broadcast_bpp = wparam; broadcast_dimensions = lparam;
    if (broadcast_hook) broadcast_hook();
    return 1;
}
static BOOL NtUserPostMessage(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    require(window == NtUserGetForegroundWindow() && message == WM_WINE_CLIPCURSOR &&
            wparam == SET_CURSOR_FSCLIP && !lparam, "foreground cursor update");
    cursor_messages++; return TRUE;
}
void winios_display_mode_changed(int w, int h) __attribute__((weak));
void winios_display_mode_changed(int w, int h)
{ ui_updates++; ui_width = w; ui_height = h; }
/* Keep real mutex behavior. A test may pause immediately after a production
 * state commit unlock, before that caller reaches the publisher. */
static int fixture_unlock(pthread_mutex_t *mutex);
#define pthread_mutex_unlock(mutex) fixture_unlock(mutex)
'''


def production_code() -> str:
    source = PRODUCTION.read_text()
    state_start = source.index("static int ios_screen_cur_w, ios_screen_cur_h;")
    state_end = source.index("static void ios_screen_size(", state_start)
    table_start = source.index("static const struct { short w, h; } ios_standard_modes[]")
    table_end = source.index("\n};", table_start) + len("\n};")
    signatures = (
        "static void ios_screen_size(",
        "static UINT get_display_index(",
        "static BOOL ios_mode_at_index(",
        "static BOOL ios_virtual_enum_display_settings(",
        "static BOOL ios_virtual_device_name(",
        "static void ios_publish_screen_size(",
        "static int ios_virtual_mode_set_enabled(",
        "static LONG ios_virtual_change_display_settings(",
    )
    return source[state_start:state_end] + source[table_start:table_end] + "\n" + "\n".join(
        function(source, sig) for sig in signatures
    )


DRIVER = r'''
static _Thread_local BOOL pause_after_commit;
static pthread_mutex_t commit_gate_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t commit_gate_condition = PTHREAD_COND_INITIALIZER;
static BOOL commit_gate_entered, commit_gate_released;
static int fixture_unlock(pthread_mutex_t *mutex)
{
    BOOL pause = pause_after_commit && mutex == &ios_screen_state_lock &&
                 ios_screen_cur_w == 640 && ios_screen_cur_h == 480;
    int result;
    if (pause) pause_after_commit = FALSE;
    result = (pthread_mutex_unlock)(mutex);
    if (pause) {
        pthread_mutex_lock(&commit_gate_lock);
        commit_gate_entered = TRUE;
        pthread_cond_broadcast(&commit_gate_condition);
        while (!commit_gate_released) pthread_cond_wait(&commit_gate_condition, &commit_gate_lock);
        (pthread_mutex_unlock)(&commit_gate_lock);
    }
    return result;
}
static WCHAR adapter[] = {'\\','\\','.','\\','D','I','S','P','L','A','Y','1',0};
static WCHAR wrong[] = {'\\','\\','.','\\','D','I','S','P','L','A','Y','9',0};
static UNICODE_STRING name = {sizeof(adapter) - sizeof(WCHAR), sizeof(adapter), adapter};
static UNICODE_STRING unknown = {sizeof(wrong) - sizeof(WCHAR), sizeof(wrong), wrong};
static DEVMODEW mode(int w, int h)
{
    DEVMODEW out = {.dmSize = sizeof(out)};
    out.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY;
    out.dmPelsWidth = w; out.dmPelsHeight = h;
    out.dmBitsPerPel = 32; out.dmDisplayFrequency = 60;
    return out;
}
static void expect(DWORD index, int w, int h, const char *contract)
{
    DEVMODEW out = {.dmSize = sizeof(out)};
    require(ios_virtual_enum_display_settings(index, &out, 0), "production mode query succeeds");
    require(out.dmPelsWidth == (DWORD)w && out.dmPelsHeight == (DWORD)h, contract);
}
static void expect_unpublished(void)
{
    require(!cache_updates && !work_invalidations && !clip_resets && !ui_updates &&
            !desktop_messages && !broadcast_messages && !cursor_messages,
            "non-applying request has no server/UI/broadcast side effects");
}
static void expect_publication(int w, int h)
{
    require(cache_updates == 1 && work_invalidations == 1 && clip_resets == 1 && ui_updates == 1,
            "one cache/work-area/cursor/UI publication");
    require(ui_width == w && ui_height == h, "host surface receives applied size");
    require(desktop_messages == 1 && broadcast_messages == 1 && cursor_messages == 1,
            "one desktop/broadcast/foreground notification");
    require(broadcast_bpp == 32 && LOWORD(broadcast_dimensions) == w && HIWORD(broadcast_dimensions) == h,
            "broadcast carries applied dimensions and bpp");
}
static _Atomic BOOL start_threads;
struct thread_modes { DEVMODEW value; };
static void *stage_writer(void *context)
{
    struct thread_modes *modes = context;
    UINT i;
    while (!atomic_load(&start_threads)) sched_yield();
    for (i = 0; i < 200; i++)
        require(ios_virtual_change_display_settings(&name, &modes->value, CDS_UPDATEREGISTRY | CDS_NORESET) ==
                DISP_CHANGE_SUCCESSFUL, "concurrent staging succeeds");
    return NULL;
}
static void *mode_reader(void *context)
{
    DEVMODEW out = {.dmSize = sizeof(out)};
    UINT i;
    (void)context;
    while (!atomic_load(&start_threads)) sched_yield();
    for (i = 0; i < 1000; i++) {
        require(ios_virtual_enum_display_settings(ENUM_CURRENT_SETTINGS, &out, 0) &&
                out.dmPelsWidth == 1280 && out.dmPelsHeight == 720, "concurrent staging does not alter current");
        require(ios_virtual_enum_display_settings(ENUM_REGISTRY_SETTINGS, &out, 0) &&
                ((out.dmPelsWidth == 1280 && out.dmPelsHeight == 720) ||
                 (out.dmPelsWidth == 800 && out.dmPelsHeight == 600) ||
                 (out.dmPelsWidth == 640 && out.dmPelsHeight == 480)), "concurrent registry dimension pair coherent");
    }
    return NULL;
}
static void *publish_once(void *context)
{
    if (context) ios_publish_screen_size(FALSE);
    else {
        DEVMODEW a = mode(800, 600);
        require(ios_virtual_change_display_settings(&name, &a, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
                "first concurrent apply succeeds");
    }
    return NULL;
}
static void *publish_after_commit_gate(void *context)
{
    DEVMODEW b = mode(640, 480);
    (void)context;
    pause_after_commit = TRUE;
    require(ios_virtual_change_display_settings(&name, &b, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
            "late publisher returns after its queued epoch was already consumed");
    return NULL;
}
static void reenter_from_displaychange(void)
{
    DEVMODEW b = mode(640, 480);
    broadcast_hook = NULL;
    require(ios_virtual_change_display_settings(&name, &b, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
            "same-thread WM_DISPLAYCHANGE re-entry returns without deadlock");
}
static void expect_latest_publication(BOOL startup)
{
    expect(ENUM_CURRENT_SETTINGS, 640, 480, "latest applied mode remains current");
    expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "concurrent temporary modes preserve registry");
    require(ui_width == 640 && ui_height == 480, "older publisher cannot leave stale final host surface");
    require(broadcast_bpp == 32 && LOWORD(broadcast_dimensions) == 640 && HIWORD(broadcast_dimensions) == 480,
            "final WM_DISPLAYCHANGE describes latest current mode");
    require(cache_updates == 2 && work_invalidations == 2 && clip_resets == 2 && ui_updates == 2,
            "owner consumes both publication epochs");
    require(desktop_messages == (startup ? 1u : 2u) && broadcast_messages == desktop_messages &&
            cursor_messages == desktop_messages, "startup silent publication does not discard later broadcast");
}
int main(int argc, char **argv)
{
    DEVMODEW a = mode(800, 600), b = mode(640, 480), invalid = mode(12345, 12345), enumerated;
    const char *scenario;
    LONG result;
    UINT index;
    BOOL found = FALSE;
    if (argc != 2) return 2;
    scenario = argv[1];
    setenv("MADEIRA_SCREEN_W", "1280", 1);
    setenv("MADEIRA_SCREEN_H", "720", 1);
    unsetenv("MADEIRA_EXTENDED_MODES");
    unsetenv("MADEIRA_VIRTUAL_MODE_SET");
    expect(ENUM_CURRENT_SETTINGS, 1280, 720, "initial current is session size");
    expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "initial registry is session size");
    for (index = 0; ios_virtual_enum_display_settings(index, &enumerated, 0); index++)
        if (enumerated.dmPelsWidth == a.dmPelsWidth && enumerated.dmPelsHeight == a.dmPelsHeight) found = TRUE;
    require(found, "candidate comes from production enumeration");

    if (!strcmp(scenario, "test-no-mutation")) {
        require(ios_virtual_change_display_settings(&name, &a, CDS_TEST) == DISP_CHANGE_SUCCESSFUL,
                "enumerated candidate test succeeds");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "test preserves current");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "test preserves registry");
        expect_unpublished();
    } else if (!strcmp(scenario, "partial-width") || !strcmp(scenario, "partial-height")) {
        a.dmFields &= !strcmp(scenario, "partial-width") ? ~DM_PELSHEIGHT : ~DM_PELSWIDTH;
        require(ios_virtual_change_display_settings(&name, &a, CDS_UPDATEREGISTRY | CDS_NORESET) ==
                DISP_CHANGE_BADMODE, "missing dimension field rejected like source_get_full_mode");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "partial fields preserve current");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "partial fields preserve registry");
        expect_unpublished();
    } else if (!strcmp(scenario, "noreset-without-update")) {
        require(ios_virtual_change_display_settings(&name, &a, CDS_NORESET) == DISP_CHANGE_BADFLAGS,
                "NORESET without UPDATEREGISTRY rejected");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "invalid flags preserve current");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "invalid flags preserve registry");
        expect_unpublished();
    } else if (!strcmp(scenario, "test-update-noreset") || !strcmp(scenario, "test-update")) {
        DWORD flags = CDS_TEST | CDS_UPDATEREGISTRY;
        if (!strcmp(scenario, "test-update-noreset")) flags |= CDS_NORESET;
        require(ios_virtual_change_display_settings(&name, &a, flags) ==
                DISP_CHANGE_SUCCESSFUL, "TEST with update/noreset validates candidate");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "combined TEST preserves current");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "combined TEST does not save registry");
        expect_unpublished();
    } else if (!strcmp(scenario, "reset-same-size")) {
        DEVMODEW original = mode(1280, 720);
        require(ios_virtual_change_display_settings(&name, &original, CDS_RESET) == DISP_CHANGE_SUCCESSFUL,
                "same-size RESET succeeds");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "RESET retains dimensions");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "RESET without UPDATE preserves registry");
        expect_publication(1280, 720);
    } else if (!strcmp(scenario, "update-now")) {
        require(ios_virtual_change_display_settings(&name, &a, CDS_UPDATEREGISTRY) == DISP_CHANGE_SUCCESSFUL,
                "immediate registry update succeeds");
        expect(ENUM_CURRENT_SETTINGS, 800, 600, "UPDATE applies current");
        expect(ENUM_REGISTRY_SETTINGS, 800, 600, "UPDATE saves registry");
        expect_publication(800, 600);
    } else if (!strncmp(scenario, "opt-out-", 8)) {
        DWORD flags = !strcmp(scenario, "opt-out-update") ? CDS_UPDATEREGISTRY : CDS_FULLSCREEN;
        setenv("MADEIRA_VIRTUAL_MODE_SET", "0", 1);
        require(ios_virtual_change_display_settings(&name, &a, flags) == DISP_CHANGE_SUCCESSFUL,
                "opt-out accepts supported request");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "opt-out does not program current");
        expect(ENUM_REGISTRY_SETTINGS, (flags & CDS_UPDATEREGISTRY) ? 800 : 1280,
               (flags & CDS_UPDATEREGISTRY) ? 600 : 720, "opt-out retains registry flag semantics");
        expect_unpublished();
    } else if (!strcmp(scenario, "commit-before-publisher")) {
        pthread_t first, second;
        gate_enabled = TRUE;
        if (pthread_create(&first, NULL, publish_once, NULL)) return 2;
        pthread_mutex_lock(&gate_lock);
        while (!gate_entered) pthread_cond_wait(&gate_condition, &gate_lock);
        pthread_mutex_unlock(&gate_lock);
        if (pthread_create(&second, NULL, publish_after_commit_gate, NULL)) return 2;
        pthread_mutex_lock(&commit_gate_lock);
        while (!commit_gate_entered) pthread_cond_wait(&commit_gate_condition, &commit_gate_lock);
        pthread_mutex_unlock(&commit_gate_lock);
        pthread_mutex_lock(&gate_lock);
        gate_released = TRUE;
        pthread_cond_broadcast(&gate_condition);
        pthread_mutex_unlock(&gate_lock);
        require(!pthread_join(first, NULL), "join owner before second caller reaches publisher");
        expect_latest_publication(FALSE);
        pthread_mutex_lock(&commit_gate_lock);
        commit_gate_released = TRUE;
        pthread_cond_broadcast(&commit_gate_condition);
        pthread_mutex_unlock(&commit_gate_lock);
        require(!pthread_join(second, NULL), "join delayed publisher");
        expect_latest_publication(FALSE);
    } else if (!strcmp(scenario, "concurrent-apply-publication") || !strcmp(scenario, "startup-apply-publication")) {
        pthread_t first;
        BOOL startup = !strcmp(scenario, "startup-apply-publication");
        gate_enabled = TRUE;
        if (pthread_create(&first, NULL, publish_once, startup ? &a : NULL)) return 2;
        pthread_mutex_lock(&gate_lock);
        while (!gate_entered) pthread_cond_wait(&gate_condition, &gate_lock);
        pthread_mutex_unlock(&gate_lock);
        require(ios_virtual_change_display_settings(&name, &b, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
                "second apply returns while prior publisher is paused");
        pthread_mutex_lock(&gate_lock);
        gate_released = TRUE;
        pthread_cond_broadcast(&gate_condition);
        pthread_mutex_unlock(&gate_lock);
        require(!pthread_join(first, NULL), "join first publisher");
        expect_latest_publication(startup);
    } else if (!strcmp(scenario, "reentrant-apply-publication")) {
        broadcast_hook = reenter_from_displaychange;
        require(ios_virtual_change_display_settings(&name, &a, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
                "outer apply completes after display-change re-entry");
        require(!broadcast_hook, "display-change hook was actually entered");
        expect_latest_publication(FALSE);
    } else if (!strcmp(scenario, "concurrent-stage-query")) {
        struct thread_modes values[2] = {{a}, {b}};
        pthread_t threads[3];
        BOOL created[3];
        UINT i;
        for (i = 0; i < 3; i++) {
            created[i] = pthread_create(&threads[i], NULL, i < 2 ? stage_writer : mode_reader,
                                        i < 2 ? &values[i] : NULL) == 0;
            require(created[i], "create concurrent state actor");
        }
        atomic_store(&start_threads, TRUE);
        for (i = 0; i < 3; i++) if (created[i]) require(!pthread_join(threads[i], NULL), "join concurrent actor");
        expect_unpublished();
    } else if (!strcmp(scenario, "bad-device") || !strcmp(scenario, "bad-mode")) {
        BOOL bad_device = !strcmp(scenario, "bad-device");
        result = ios_virtual_change_display_settings(bad_device ? &unknown : &name,
                                                     bad_device ? &a : &invalid, CDS_UPDATEREGISTRY | CDS_NORESET);
        require(result == (bad_device ? DISP_CHANGE_BADPARAM : DISP_CHANGE_BADMODE), "invalid staged request rejected");
        expect(ENUM_CURRENT_SETTINGS, 1280, 720, "invalid request preserves current");
        expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "invalid request preserves registry");
        expect_unpublished();
    } else if (!strncmp(scenario, "dynamic-", 8)) {
        require(ios_virtual_change_display_settings(&name, &a, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
                "temporary fullscreen succeeds");
        if (!strcmp(scenario, "dynamic-current")) expect(ENUM_CURRENT_SETTINGS, 800, 600, "dynamic current changed");
        else if (!strcmp(scenario, "dynamic-registry")) expect(ENUM_REGISTRY_SETTINGS, 1280, 720, "dynamic mode preserves registry");
        else if (!strcmp(scenario, "dynamic-publication")) expect_publication(800, 600);
        else if (!strcmp(scenario, "dynamic-null-restore")) {
            require(ios_virtual_change_display_settings(&name, NULL, 0) == DISP_CHANGE_SUCCESSFUL,
                    "NULL restore succeeds");
            expect(ENUM_CURRENT_SETTINGS, 1280, 720, "NULL restores original registry");
        } else return 2;
    } else {
        require(ios_virtual_change_display_settings(&name, &a, CDS_UPDATEREGISTRY | CDS_NORESET) == DISP_CHANGE_SUCCESSFUL,
                "stage succeeds");
        if (!strcmp(scenario, "stage-current")) expect(ENUM_CURRENT_SETTINGS, 1280, 720, "NORESET preserves current");
        else if (!strcmp(scenario, "stage-registry")) expect(ENUM_REGISTRY_SETTINGS, 800, 600, "NORESET saves registry");
        else if (!strcmp(scenario, "stage-unpublished")) expect_unpublished();
        else if (!strcmp(scenario, "stage-test-preserves")) {
            require(ios_virtual_change_display_settings(&name, &b, CDS_TEST) == DISP_CHANGE_SUCCESSFUL,
                    "different mode test succeeds while staged");
            expect(ENUM_CURRENT_SETTINGS, 1280, 720, "test while staged preserves current");
            expect(ENUM_REGISTRY_SETTINGS, 800, 600, "test while staged preserves saved registry");
            expect_unpublished();
        } else if (!strcmp(scenario, "stage-invalid-preserves")) {
            require(ios_virtual_change_display_settings(&name, &invalid, CDS_UPDATEREGISTRY | CDS_NORESET) ==
                    DISP_CHANGE_BADMODE, "invalid second stage rejected");
            expect(ENUM_CURRENT_SETTINGS, 1280, 720, "rejected second stage preserves current");
            expect(ENUM_REGISTRY_SETTINGS, 800, 600, "rejected second stage preserves saved registry");
            expect_unpublished();
        } else if (!strcmp(scenario, "stage-last-wins")) {
            require(ios_virtual_change_display_settings(&name, &b, CDS_UPDATEREGISTRY | CDS_NORESET) ==
                    DISP_CHANGE_SUCCESSFUL, "second valid stage succeeds");
            require(ios_virtual_change_display_settings(NULL, NULL, 0) == DISP_CHANGE_SUCCESSFUL, "NULL apply succeeds");
            expect(ENUM_CURRENT_SETTINGS, 640, 480, "NULL apply uses last saved mode");
            expect(ENUM_REGISTRY_SETTINGS, 640, 480, "last saved registry remains");
        } else if (!strcmp(scenario, "stage-fullscreen-restore")) {
            require(ios_virtual_change_display_settings(&name, &b, CDS_FULLSCREEN) == DISP_CHANGE_SUCCESSFUL,
                    "temporary mode after staging succeeds");
            expect(ENUM_CURRENT_SETTINGS, 640, 480, "temporary mode becomes current");
            expect(ENUM_REGISTRY_SETTINGS, 800, 600, "temporary mode preserves saved registry");
            require(ios_virtual_change_display_settings(&name, NULL, 0) == DISP_CHANGE_SUCCESSFUL, "NULL restore succeeds");
            expect(ENUM_CURRENT_SETTINGS, 800, 600, "NULL restore uses saved registry not launch default");
        } else if (!strcmp(scenario, "stage-null-apply") || !strcmp(scenario, "stage-publication") ||
                   !strcmp(scenario, "stage-no-size-apply")) {
            DEVMODEW no_size = {.dmSize = sizeof(no_size)};
            require(ios_virtual_change_display_settings(NULL,
                    !strcmp(scenario, "stage-no-size-apply") ? &no_size : NULL, 0) == DISP_CHANGE_SUCCESSFUL,
                    "NULL/no-size apply succeeds");
            if (strcmp(scenario, "stage-publication")) {
                expect(ENUM_CURRENT_SETTINGS, 800, 600, "NULL apply uses saved registry");
                expect(ENUM_REGISTRY_SETTINGS, 800, 600, "NULL apply retains saved registry");
            } else expect_publication(800, 600);
        } else return 2;
    }
    printf("%s: %s (%u checks, %u failed)\n", failures ? "FAIL" : "PASS", scenario, checks, failures);
    return failures ? 1 : 0;
}
'''


SCENARIOS = (
    "test-no-mutation", "bad-device", "bad-mode", "stage-current", "stage-registry",
    "partial-width", "partial-height", "noreset-without-update", "test-update-noreset",
    "test-update", "reset-same-size", "update-now", "opt-out-dynamic", "opt-out-update",
    "concurrent-stage-query", "stage-no-size-apply",
    "concurrent-apply-publication", "reentrant-apply-publication", "startup-apply-publication",
    "commit-before-publisher",
    "stage-unpublished", "stage-test-preserves", "stage-invalid-preserves", "stage-null-apply",
    "stage-publication", "stage-last-wins", "stage-fullscreen-restore", "dynamic-current",
    "dynamic-registry", "dynamic-publication", "dynamic-null-restore",
)


def main() -> int:
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="host-state-", dir=SCRATCH_PARENT) as directory:
        c = Path(directory) / "check.c"
        c.write_text(PRELUDE + production_code() + DRIVER)
        for label, options in (
            ("asan-ubsan", ["-O1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]),
            ("optimized", ["-O2"]),
        ):
            binary = Path(directory) / label
            subprocess.run(
                ["cc", "-std=gnu11", "-D__WINESRC__", "-Wall", "-Wextra", "-Werror",
                 "-Wno-pragma-pack", "-Wno-unused-parameter", "-Wno-unused-function", *options,
                 "-I", str(ROOT / "wine/build-macos/include"), "-I", str(ROOT / "wine/include"),
                 str(c), "-o", str(binary)], check=True, timeout=30,
            )
            for scenario in SCENARIOS:
                result = subprocess.run([str(binary), scenario], text=True, capture_output=True, timeout=10)
                print(f"{label}: {result.stdout.strip()}")
                if result.returncode:
                    failed += 1
                    print(result.stderr, end="")
            print(f"{label}: completed {len(SCENARIOS)} production scenarios")
    print(f"display-mode state: {failed} scenario runs failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
