/* Optional, bounded metadata-only tracing around the unchanged upstream call. */
#define NtFsControlFile ios_upstream_NtFsControlFile
#include "../../wine/dlls/ntdll/unix/file.c"
#undef NtFsControlFile

#include <mach/mach.h>
#include <mach/mach_vm.h>

NTSTATUS WINAPI NtFsControlFile( HANDLE handle, HANDLE event, PIO_APC_ROUTINE apc, void *apc_context,
                                 IO_STATUS_BLOCK *io, ULONG code, void *in_buffer, ULONG in_size,
                                 void *out_buffer, ULONG out_size )
{
    static unsigned int general_count, pipe_count;
    const char *enabled = getenv( "MADEIRA_FILE_CONTROL_TRACE" );
    unsigned int sequence = 0;
    unsigned int limit = 0;
    const char *bucket = "general";
    NTSTATUS status;
    TEB *teb = NULL;
    void *peb = NULL;
    ULONG_PTR pid = 0, tid = 0;

    if (enabled && !strcmp( enabled, "1" ))
    {
        /* Volume queries during launch must not consume the pipe handshake
         * budget. Counters are independent and IDs are paired with the bucket. */
        if ((code >> 16) == FILE_DEVICE_NAMED_PIPE)
        {
            bucket = "pipe";
            limit = 64;
            sequence = __atomic_add_fetch( &pipe_count, 1, __ATOMIC_RELAXED );
        }
        else
        {
            limit = 128;
            sequence = __atomic_add_fetch( &general_count, 1, __ATOMIC_RELAXED );
        }
        if (sequence <= limit)
        {
            teb = NtCurrentTeb();
            if (teb)
            {
                peb = teb->Peb;
                pid = (ULONG_PTR)teb->ClientId.UniqueProcess;
                tid = (ULONG_PTR)teb->ClientId.UniqueThread;
            }
            dprintf( 2, "[file-control] %s #%u ENTER peb=%p pid=%lx tid=%lx handle=%p event=%p "
                        "code=%08x in=%u out=%u\n",
                     bucket, sequence, peb, (unsigned long)pid, (unsigned long)tid,
                     handle, event, (unsigned int)code, (unsigned int)in_size, (unsigned int)out_size );
        }
    }

    status = ios_upstream_NtFsControlFile( handle, event, apc, apc_context, io, code,
                                         in_buffer, in_size, out_buffer, out_size );
    if (sequence && sequence <= limit)
    {
        IO_STATUS_BLOCK snapshot;
        mach_vm_size_t copied = 0;
        kern_return_t result = KERN_INVALID_ADDRESS;
        /* The original call owns validation. Logging must not dereference an
         * invalid caller pointer, or turn a reported failure into a crash. */
        if (io)
            result = mach_vm_read_overwrite( mach_task_self(), (mach_vm_address_t)(uintptr_t)io,
                                             sizeof(snapshot), (mach_vm_address_t)(uintptr_t)&snapshot,
                                             &copied );
        if (result == KERN_SUCCESS && copied == sizeof(snapshot))
            dprintf( 2, "[file-control] %s #%u RETURN peb=%p pid=%lx tid=%lx status=%08x "
                        "iosb_status=%08x information=%lx\n",
                     bucket, sequence, peb, (unsigned long)pid, (unsigned long)tid, (unsigned int)status,
                     (unsigned int)snapshot.Status, (unsigned long)snapshot.Information );
        else
            dprintf( 2, "[file-control] %s #%u RETURN peb=%p pid=%lx tid=%lx status=%08x "
                        "iosb=unreadable read_kr=%d bytes=%llu\n",
                     bucket, sequence, peb, (unsigned long)pid, (unsigned long)tid, (unsigned int)status,
                     result, (unsigned long long)copied );
    }
    return status;
}
