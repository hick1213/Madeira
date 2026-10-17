/* Host regression: valid server-owned desktop handles have no client WND.
 * Production handle lookup and Wine headers are included by the Python runner.
 * TEB data and user_lock are modeled; this is not a device/concurrency test. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "win32u_private.h"
#include "ntuser_private.h"

static session_shm_t session;
const session_shm_t *shared_session = &session;
static struct ntuser_thread_info fixture_thread_info;
static DWORD current_pid = 0x20;
static int lock_depth, lock_calls, unlock_calls;
static WND local_window;

#undef GetCurrentProcessId
#define GetCurrentProcessId() (current_pid)
#define NtUserGetThreadInfo() (&fixture_thread_info)

static void require( int value, const char *message )
{
    if (!value)
    {
        fprintf( stderr, "FAIL: %s\n", message );
        exit( 1 );
    }
}

void user_lock(void)
{
    require( !lock_depth, "handle lookup must not recursively acquire user_lock" );
    lock_depth++;
    lock_calls++;
}

void user_unlock(void)
{
    require( lock_depth == 1, "handle lookup must unlock exactly once" );
    lock_depth--;
    unlock_calls++;
}

#include "production-window.inc"

static void prepare( unsigned int handle, unsigned short type, unsigned int pid, void *ptr )
{
    unsigned int index = USER_HANDLE_TO_INDEX( UlongToHandle( handle ) );
    volatile struct user_entry *entry;

    require( !lock_depth, "each case must start unlocked" );
    memset( (void *)&session, 0, sizeof(session) );
    memset( client_objects, 0, sizeof(client_objects) );
    memset( &fixture_thread_info, 0, sizeof(fixture_thread_info) );
    require( index < MAX_USER_HANDLES, "fixture must use an in-range handle" );
    entry = &session.user_entries[index];
    entry->type = type;
    entry->generation = HIWORD(handle);
    entry->pid = pid;
    entry->tid = 0x24;
    entry->id = 1;
    client_objects[index] = ptr;
    fixture_thread_info.top_window = 0x10020;
    fixture_thread_info.msg_window = 0x10022;
    lock_calls = unlock_calls = 0;
}

static void expect_object( unsigned int handle, unsigned short type, void *wanted, const char *message )
{
    void *ptr = get_user_handle_ptr( UlongToHandle( handle ), type );
    int locked = wanted && wanted != OBJ_OTHER_PROCESS;

    require( ptr == wanted, message );
    require( lock_depth == locked, "local pointers keep user_lock; NULL and sentinels release it" );
    require( lock_calls == 1 && unlock_calls == !locked, "handle lookup lock balance" );
    if (locked) user_unlock();
    lock_calls = unlock_calls = 0;
}

static void expect_window( unsigned int handle, WND *wanted, const char *message )
{
    WND *ptr = get_win_ptr( UlongToHandle( handle ) );
    int locked = wanted && wanted != WND_OTHER_PROCESS && wanted != WND_DESKTOP;

    require( ptr == wanted, message );
    require( lock_depth == locked, "desktop/other/invalid window pointers must return unlocked" );
    require( lock_calls == 1 && unlock_calls == !locked, "window lookup lock balance" );
    if (locked) user_unlock();
    lock_calls = unlock_calls = 0;
}

int main(void)
{
#ifdef WINE_IOS
    void *desktop_object = OBJ_OTHER_PROCESS;
    WND *desktop_window = WND_DESKTOP;
#else
    void *desktop_object = NULL;
    WND *desktop_window = NULL;
#endif

    prepare( 0x10020, NTUSER_OBJ_WINDOW, current_pid, NULL );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, desktop_object, "valid current-process desktop without WND" );
    expect_window( 0x10020, desktop_window, "desktop window must reach WND_DESKTOP on iOS" );
    expect_object( 0x0020, NTUSER_OBJ_WINDOW, desktop_object, "truncated desktop resolves validated generation" );
    expect_window( 0xffff0020, desktop_window, "sign-extended desktop resolves validated generation" );

    prepare( 0x10022, NTUSER_OBJ_WINDOW, current_pid, NULL );
    expect_object( 0x10022, NTUSER_OBJ_WINDOW, desktop_object, "valid message parent without WND" );
    expect_window( 0x10022, desktop_window, "message parent must reach WND_DESKTOP on iOS" );

    prepare( 0x10024, NTUSER_OBJ_WINDOW, current_pid, NULL );
    expect_object( 0x10024, NTUSER_OBJ_WINDOW, NULL, "ordinary missing WND stays invalid" );
    expect_window( 0x10024, NULL, "ordinary missing window stays invalid" );

    prepare( 0x10020, NTUSER_OBJ_WINDOW, current_pid, &local_window );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, &local_window, "real local WND takes precedence over desktop cache" );
    expect_window( 0x10020, &local_window, "real local window preserves lock ownership" );

    prepare( 0x10024, NTUSER_OBJ_WINDOW, current_pid, &local_window );
    expect_window( 0x10024, &local_window, "ordinary local window remains unchanged" );

    prepare( 0x10020, NTUSER_OBJ_WINDOW, 0x28, NULL );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, OBJ_OTHER_PROCESS, "other-process desktop keeps existing classification" );
    expect_window( 0x10020, WND_DESKTOP, "other-process desktop keeps WND_DESKTOP" );

    prepare( 0x10024, NTUSER_OBJ_WINDOW, 0x28, NULL );
    expect_window( 0x10024, WND_OTHER_PROCESS, "other-process ordinary window remains remote" );

    prepare( 0x20020, NTUSER_OBJ_WINDOW, current_pid, NULL );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, NULL, "stale desktop generation is rejected before cache" );
    expect_window( 0x10020, NULL, "stale desktop cache cannot validate a window" );
    expect_window( 0x20020, NULL, "valid generation not in desktop cache remains missing" );

    prepare( 0x10020, NTUSER_OBJ_MENU, current_pid, NULL );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, NULL, "wrong entry type rejects cached desktop" );
    expect_object( 0x10020, NTUSER_OBJ_MENU, NULL, "non-window object never gets desktop fallback" );
    expect_window( 0x10020, NULL, "menu entry is not a desktop window" );

    prepare( 0x10020, 0, current_pid, NULL );
    expect_object( 0x10020, NTUSER_OBJ_WINDOW, NULL, "free entry rejects cached desktop" );
    expect_window( 0x10020, NULL, "free desktop entry stays invalid" );

    prepare( 0x10022, NTUSER_OBJ_WINDOW, current_pid, NULL );
    session.user_entries[USER_HANDLE_TO_INDEX( UlongToHandle( 0x10022 ) )].generation = 2;
    expect_window( 0x10022, NULL, "stale message parent is rejected" );

    prepare( 0x10020, NTUSER_OBJ_WINDOW, current_pid, NULL );
    fixture_thread_info.top_window = fixture_thread_info.msg_window = 0;
    expect_window( 0x10020, NULL, "no cached system window cannot classify a missing WND" );

    require( get_user_handle_ptr( UlongToHandle( 0x10000 ), NTUSER_OBJ_WINDOW ) == NULL,
             "below-range handle is rejected" );
    require( !lock_depth && !lock_calls && !unlock_calls, "out-of-range handle does not acquire lock" );
#ifdef WINE_IOS
    puts( "PASS: server-owned desktop handle contract (iOS, ASan/UBSan)" );
#else
    puts( "PASS: server-owned desktop handle contract (other platforms unchanged, ASan/UBSan)" );
#endif
    return 0;
}
