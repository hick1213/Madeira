/* Keep upstream synchronization intact; only NULL default keyed-event handles
 * need per-pseudo-process resolution on the shared iOS host. */
#define NtWaitForKeyedEvent ios_upstream_NtWaitForKeyedEvent
#define NtReleaseKeyedEvent ios_upstream_NtReleaseKeyedEvent
#include "../../wine/dlls/ntdll/unix/sync.c"
#undef NtWaitForKeyedEvent
#undef NtReleaseKeyedEvent

/* IOS_DEFAULT_KEYED_WRAPPERS_BEGIN */
extern NTSTATUS ios_default_keyed_event( HANDLE *handle );

NTSTATUS WINAPI NtWaitForKeyedEvent( HANDLE handle, const void *key,
                                    BOOLEAN alertable, const LARGE_INTEGER *timeout )
{
    NTSTATUS status;
    if (!handle && (status = ios_default_keyed_event( &handle ))) return status;
    return ios_upstream_NtWaitForKeyedEvent( handle, key, alertable, timeout );
}

NTSTATUS WINAPI NtReleaseKeyedEvent( HANDLE handle, const void *key,
                                   BOOLEAN alertable, const LARGE_INTEGER *timeout )
{
    NTSTATUS status;
    if (!handle && (status = ios_default_keyed_event( &handle ))) return status;
    return ios_upstream_NtReleaseKeyedEvent( handle, key, alertable, timeout );
}
