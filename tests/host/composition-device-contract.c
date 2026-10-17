/* Production-state contract test. Only OS locks/window validity and the final
 * external surface are adapted; no Device/Visual/Target state is replaced. */
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sched.h>

static _Thread_local unsigned host_locks;
static BOOL host_window_alive = TRUE;
static const HWND host_hwnd = (HWND)(UINT_PTR)0x101;
static const HWND host_other_hwnd = (HWND)(UINT_PTR)0x102;
static const HWND host_foreign_hwnd = (HWND)(UINT_PTR)0x103;
static pthread_mutex_t host_registry_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t host_wait_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t host_wait_cond = PTHREAD_COND_INITIALIZER;
static BOOL host_bind_entered, host_bind_continue, host_wait_sleep, host_wait_done;
static LONG host_next_tid;
static _Thread_local DWORD host_tid;

void WINAPI InitializeCriticalSection(CRITICAL_SECTION *cs)
{
    pthread_mutex_t *mutex = malloc(sizeof(*mutex));
    pthread_mutexattr_t attr;
    assert(mutex);
    assert(!pthread_mutexattr_init(&attr));
    assert(!pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE));
    assert(!pthread_mutex_init(mutex, &attr));
    assert(!pthread_mutexattr_destroy(&attr));
    memset(cs, 0, sizeof(*cs));
    cs->DebugInfo = (RTL_CRITICAL_SECTION_DEBUG *)mutex;
}
void WINAPI DeleteCriticalSection(CRITICAL_SECTION *cs)
{
    pthread_mutex_t *mutex = (pthread_mutex_t *)cs->DebugInfo;
    assert(!host_locks);
    assert(!pthread_mutex_destroy(mutex));
    free(mutex);
}
void WINAPI EnterCriticalSection(CRITICAL_SECTION *cs)
{
    assert(!pthread_mutex_lock((pthread_mutex_t *)cs->DebugInfo));
    ++host_locks;
}
void WINAPI LeaveCriticalSection(CRITICAL_SECTION *cs)
{
    assert(host_locks);
    --host_locks;
    assert(!pthread_mutex_unlock((pthread_mutex_t *)cs->DebugInfo));
}
BOOL WINAPI IsWindow(HWND hwnd)
{
    assert(!host_locks);
    return hwnd == host_other_hwnd || hwnd == host_foreign_hwnd || (hwnd == host_hwnd && host_window_alive);
}
DWORD WINAPI GetCurrentProcessId(void) { return 42; }
DWORD WINAPI GetCurrentThreadId(void)
{
    if (!host_tid) host_tid = InterlockedIncrement(&host_next_tid);
    return host_tid;
}
void WINAPI Sleep(DWORD milliseconds)
{
    assert(!milliseconds);
    assert(!pthread_mutex_lock(&host_wait_mutex));
    host_wait_sleep = TRUE;
    assert(!pthread_cond_broadcast(&host_wait_cond));
    assert(!pthread_mutex_unlock(&host_wait_mutex));
    sched_yield();
}
DWORD WINAPI GetWindowThreadProcessId(HWND hwnd, DWORD *pid)
{
    assert(!host_locks);
    *pid = hwnd == host_foreign_hwnd ? 99 : 42;
    return IsWindow(hwnd) ? 100 : 0;
}
void WINAPI AcquireSRWLockExclusive(SRWLOCK *lock)
{
    (void)lock;
    assert(!pthread_mutex_lock(&host_registry_mutex));
    ++host_locks;
}
void WINAPI ReleaseSRWLockExclusive(SRWLOCK *lock)
{
    (void)lock;
    assert(host_locks);
    --host_locks;
    assert(!pthread_mutex_unlock(&host_registry_mutex));
}

/* This external surface implements the agreed private ABI independently of
 * the production header, so a changed GUID/slot/argument cannot self-validate. */
static const IID host_surface_iid =
    {0x8b6f2b77,0x41a8,0x4b60,{0x8e,0x95,0xc9,0x35,0x69,0x5f,0x61,0x12}};
struct host_surface {
    IUnknown iface;
    LONG refs;
    HWND bound;
    HRESULT attach_error;
    HRESULT detach_error;
    unsigned attachments, detachments;
    BOOL supported;
    IDCompositionDevice *reentrant_device;
    BOOL block_attachment;
};
struct host_surface_vtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(IUnknown *, REFIID, void **);
    ULONG (STDMETHODCALLTYPE *AddRef)(IUnknown *);
    ULONG (STDMETHODCALLTYPE *Release)(IUnknown *);
    HRESULT (STDMETHODCALLTYPE *BindToHwnd)(IUnknown *, HWND);
};
static ULONG STDMETHODCALLTYPE host_surface_addref(IUnknown *iface)
{
    assert(!host_locks);
    return InterlockedIncrement(&((struct host_surface *)iface)->refs);
}
static ULONG STDMETHODCALLTYPE host_surface_release(IUnknown *iface)
{
    assert(!host_locks);
    LONG count = InterlockedDecrement(&((struct host_surface *)iface)->refs);
    assert(count >= 1); /* test retains the external owner */
    return count;
}
static HRESULT STDMETHODCALLTYPE host_surface_query(IUnknown *iface, REFIID iid, void **out)
{
    struct host_surface *surface = (struct host_surface *)iface;
    assert(!host_locks);
    assert(out);
    *out = NULL;
    if (!IsEqualIID(iid, &IID_IUnknown) &&
        (!surface->supported || !IsEqualIID(iid, &host_surface_iid))) return E_NOINTERFACE;
    *out = iface;
    host_surface_addref(iface);
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE host_surface_bind(IUnknown *iface, HWND hwnd)
{
    struct host_surface *surface = (struct host_surface *)iface;
    assert(!host_locks);
    if (surface->reentrant_device) {
        IDCompositionVisual *visual = NULL;
        assert(IDCompositionDevice_Commit(surface->reentrant_device) == RPC_E_CALL_REJECTED);
        assert(IDCompositionDevice_WaitForCommitCompletion(surface->reentrant_device) == RPC_E_CALL_REJECTED);
        assert(IDCompositionDevice_CreateVisual(surface->reentrant_device, &visual) == S_OK);
        IDCompositionVisual_Release(visual);
    }
    if (!hwnd) {
        if (FAILED(surface->detach_error)) return surface->detach_error;
        surface->bound = NULL;
        ++surface->detachments;
        return S_OK;
    }
    if (FAILED(surface->attach_error)) return surface->attach_error;
    if (surface->block_attachment) {
        assert(!pthread_mutex_lock(&host_wait_mutex));
        host_bind_entered = TRUE;
        assert(!pthread_cond_broadcast(&host_wait_cond));
        while (!host_bind_continue) assert(!pthread_cond_wait(&host_wait_cond, &host_wait_mutex));
        assert(!pthread_mutex_unlock(&host_wait_mutex));
    }
    if (!IsWindow(hwnd) || (surface->bound && surface->bound != hwnd)) return E_INVALIDARG;
    surface->bound = hwnd;
    ++surface->attachments;
    return S_OK;
}
static const struct host_surface_vtbl host_surface_vtable = {
    host_surface_query, host_surface_addref, host_surface_release, host_surface_bind
};
static struct host_surface host_surface_new(BOOL supported)
{
    struct host_surface surface = {0};
    surface.iface.lpVtbl = (const IUnknownVtbl *)&host_surface_vtable;
    surface.refs = 1;
    surface.supported = supported;
    return surface;
}
static IDCompositionDevice *host_device_new(void)
{
    IDCompositionDevice *device = NULL;
    HRESULT hr = DCompositionCreateDevice(NULL, &IID_IDCompositionDevice, (void **)&device);
    if (hr != S_OK || !device) {
        fprintf(stderr, "FAIL: Device1 creation returned %#x instead of S_OK\n", (unsigned)hr);
        exit(1);
    }
    return device;
}
static void *host_commit_thread(void *argument)
{
    IDCompositionDevice *device = argument;
    for (unsigned i = 0; i < 500; ++i) {
        HRESULT hr = IDCompositionDevice_Commit(device);
        assert(hr == S_OK || hr == RPC_E_CALL_REJECTED);
    }
    return NULL;
}
static void *host_one_commit(void *argument)
{
    assert(IDCompositionDevice_Commit((IDCompositionDevice *)argument) == S_OK);
    return NULL;
}
static void *host_wait_completion(void *argument)
{
    HRESULT hr = IDCompositionDevice_WaitForCommitCompletion((IDCompositionDevice *)argument);
    assert(!pthread_mutex_lock(&host_wait_mutex));
    host_wait_done = TRUE;
    assert(!pthread_cond_broadcast(&host_wait_cond));
    assert(!pthread_mutex_unlock(&host_wait_mutex));
    assert(hr == S_OK);
    return NULL;
}
int main(void)
{
    IDCompositionDevice *device = host_device_new();
    IDCompositionDevice *other = host_device_new();
    IDCompositionVisual *visual = NULL, *foreign = NULL;
    IDCompositionTarget *target = NULL, *second = (void *)1;
    struct host_surface a = host_surface_new(TRUE), b = host_surface_new(TRUE);
    struct host_surface unsupported = host_surface_new(FALSE);
    void *out = (void *)1;
    const IID newer_iid = {0x75f6468d,0x1b8e,0x447c,{0x9b,0xc6,0x75,0xfe,0xa8,0x0b,0x5b,0x25}};

    assert(DCompositionCreateDevice(NULL, &IID_IDCompositionDevice, NULL) == E_POINTER);
    assert(DCompositionCreateDevice(NULL, &newer_iid, &out) == E_NOINTERFACE && !out);
    out = (void *)1;
    assert(DCompositionCreateDevice((IDXGIDevice *)&a, &IID_IDCompositionDevice, &out) == E_NOTIMPL && !out);
    assert(IDCompositionDevice_QueryInterface(device, &newer_iid, &out) == E_NOINTERFACE && !out);
    assert(IDCompositionDevice_QueryInterface(device, &IID_IUnknown, &out) == S_OK && out == device);
    IUnknown_Release((IUnknown *)out);
    out = (void *)1;
    assert(DCompositionCreateDevice2(NULL, &IID_IDCompositionDevice, &out) == E_NOTIMPL && !out);
    out = (void *)1;
    assert(DCompositionCreateDevice3(NULL, &IID_IDCompositionDevice, &out) == E_NOTIMPL && !out);
    IDCompositionSurface *surface_out = (void *)1;
    assert(IDCompositionDevice_CreateSurface(device, 10, 10, DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_ALPHA_MODE_IGNORE, &surface_out) == E_NOTIMPL && !surface_out);
    IDCompositionVirtualSurface *virtual_out = (void *)1;
    assert(IDCompositionDevice_CreateVirtualSurface(device, 10, 10, DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_ALPHA_MODE_IGNORE, &virtual_out) == E_NOTIMPL && !virtual_out);
    IDCompositionAnimation *animation_out = (void *)1;
    assert(IDCompositionDevice_CreateAnimation(device, &animation_out) == E_NOTIMPL && !animation_out);
    DCOMPOSITION_FRAME_STATISTICS statistics;
    memset(&statistics, 0x55, sizeof(statistics));
    assert(IDCompositionDevice_GetFrameStatistics(device, &statistics) == E_NOTIMPL);
    const unsigned char *bytes = (const unsigned char *)&statistics;
    for (unsigned i = 0; i < sizeof(statistics); ++i) assert(!bytes[i]);
    assert(IDCompositionDevice_CreateVisual(device, NULL) == E_POINTER);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, NULL, TRUE, &target) == HRESULT_FROM_WIN32(ERROR_INVALID_WINDOW_HANDLE) && !target);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, host_foreign_hwnd, TRUE, &target) == E_ACCESSDENIED && !target);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, host_hwnd, FALSE, &target) == E_NOTIMPL && !target);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, host_hwnd, TRUE, &target) == S_OK);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, host_other_hwnd, TRUE, &second) == E_NOTIMPL && !second);
    assert(IDCompositionDevice_CreateTargetForHwnd(other, host_hwnd, TRUE, &second) == DCOMPOSITION_ERROR_WINDOW_ALREADY_COMPOSED && !second);
    assert(IDCompositionDevice_CreateVisual(device, &visual) == S_OK);
    assert(IDCompositionDevice_CreateVisual(other, &foreign) == S_OK);
    assert(IDCompositionTarget_SetRoot(target, foreign) == E_INVALIDARG);
    IDCompositionVisual_Release(foreign);
    IDCompositionDevice_Release(other);
    assert(IDCompositionVisual_SetOffsetX(visual, 10.0f) == E_NOTIMPL);
    assert(IDCompositionVisual_SetContent(visual, &unsupported.iface) == E_NOINTERFACE);
    assert(unsupported.refs == 1);
    assert(IDCompositionVisual_SetContent(visual, &a.iface) == S_OK && a.refs == 2);
    assert(IDCompositionTarget_SetRoot(target, visual) == S_OK && !a.bound);
    a.reentrant_device = device;
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);
    a.reentrant_device = NULL;
    assert(IDCompositionDevice_WaitForCommitCompletion(device) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && a.attachments == 1 && !a.detachments);

    /* Pending edits cannot affect a displayed target until Commit succeeds. */
    b.attach_error = E_ACCESSDENIED;
    assert(IDCompositionVisual_SetContent(visual, &b.iface) == S_OK && a.bound == host_hwnd);
    assert(IDCompositionDevice_Commit(device) == E_ACCESSDENIED);
    assert(a.bound == host_hwnd && !b.bound && a.refs == 2 && b.refs == 2);
    b.attach_error = S_OK;
    assert(IDCompositionDevice_Commit(device) == S_OK && !a.bound && b.bound == host_hwnd);
    assert(a.refs == 1 && b.refs == 2);
    /* A rollback failure is visible too: no applied binding may be claimed
     * after both the new attachment and restoring the old attachment fail. */
    a.attach_error = E_ACCESSDENIED;
    b.attach_error = E_FAIL;
    assert(IDCompositionVisual_SetContent(visual, &a.iface) == S_OK);
    assert(IDCompositionDevice_Commit(device) == E_FAIL && !a.bound && !b.bound);
    assert(a.refs == 2 && b.refs == 1);
    a.attach_error = S_OK;
    b.attach_error = S_OK;
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);
    assert(IDCompositionVisual_SetContent(visual, &b.iface) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && b.bound == host_hwnd);
    b.detach_error = E_FAIL;
    assert(IDCompositionTarget_SetRoot(target, NULL) == S_OK && b.bound == host_hwnd);
    assert(IDCompositionDevice_Commit(device) == E_FAIL && b.bound == host_hwnd);
    b.detach_error = S_OK;
    assert(IDCompositionDevice_Commit(device) == S_OK && !b.bound);
    assert(IDCompositionTarget_SetRoot(target, visual) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && b.bound == host_hwnd);
    assert(IDCompositionVisual_SetContent(visual, NULL) == S_OK && b.bound == host_hwnd);
    assert(IDCompositionDevice_Commit(device) == S_OK && !b.bound && b.refs == 1);

    assert(IDCompositionVisual_SetContent(visual, &a.iface) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);
    host_window_alive = FALSE;
    assert(IDCompositionDevice_Commit(device) == HRESULT_FROM_WIN32(ERROR_INVALID_WINDOW_HANDLE));
    assert(!a.bound);
    host_window_alive = TRUE;
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);
    pthread_t threads[8];
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_create(&threads[i], NULL, host_commit_thread, device));
    for (unsigned i = 0; i < 500; ++i) {
        assert(IDCompositionVisual_SetContent(visual, (i & 1) ? &a.iface : &b.iface) == S_OK);
        assert(IDCompositionTarget_SetRoot(target, (i % 3) ? visual : NULL) == S_OK);
    }
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_join(threads[i], NULL));
    assert(IDCompositionVisual_SetContent(visual, &a.iface) == S_OK);
    assert(IDCompositionTarget_SetRoot(target, visual) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK);
    assert(a.bound == host_hwnd);
    assert(a.refs == 2 && b.refs == 1);

    /* Synchronize against an unfinished real Commit. The old busy-rejection
     * implementation returns early; a correct waiter is still pending when
     * the external attachment is held, then returns S_OK after it is released. */
    pthread_t committing_thread, waiting_thread;
    b.block_attachment = TRUE;
    assert(IDCompositionVisual_SetContent(visual, &b.iface) == S_OK);
    assert(!pthread_create(&committing_thread, NULL, host_one_commit, device));
    assert(!pthread_mutex_lock(&host_wait_mutex));
    while (!host_bind_entered) assert(!pthread_cond_wait(&host_wait_cond, &host_wait_mutex));
    assert(!pthread_mutex_unlock(&host_wait_mutex));
    assert(!pthread_create(&waiting_thread, NULL, host_wait_completion, device));
    assert(!pthread_mutex_lock(&host_wait_mutex));
    while (!host_wait_sleep && !host_wait_done) assert(!pthread_cond_wait(&host_wait_cond, &host_wait_mutex));
    assert(!host_wait_done);
    host_bind_continue = TRUE;
    assert(!pthread_cond_broadcast(&host_wait_cond));
    assert(!pthread_mutex_unlock(&host_wait_mutex));
    assert(!pthread_join(committing_thread, NULL));
    assert(!pthread_join(waiting_thread, NULL));
    b.block_attachment = FALSE;
    assert(b.bound == host_hwnd);
    assert(IDCompositionVisual_SetContent(visual, &a.iface) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);

    /* Releasing the target races an in-flight snapshot, not arbitrary COM
     * caller misuse: worker threads hold only the device reference. */
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_create(&threads[i], NULL, host_commit_thread, device));
    IDCompositionTarget_Release(target);
    target = NULL;
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_join(threads[i], NULL));
    assert(!a.bound);
    assert(IDCompositionDevice_CreateTargetForHwnd(device, host_hwnd, TRUE, &target) == S_OK);
    assert(IDCompositionTarget_SetRoot(target, visual) == S_OK);
    assert(IDCompositionDevice_Commit(device) == S_OK && a.bound == host_hwnd);

    /* Children keep the device alive; final target destruction unbinds without
     * a reference cycle, while the visual remains independently usable. */
    IDCompositionDevice_Release(device);
    IDCompositionTarget_Release(target);
    assert(!a.bound);
    assert(IDCompositionVisual_SetContent(visual, NULL) == S_OK && a.refs == 1);
    IDCompositionVisual_Release(visual);
    other = host_device_new();
    assert(IDCompositionDevice_CreateTargetForHwnd(other, host_hwnd, TRUE, &second) == S_OK);
    IDCompositionTarget_Release(second);
    IDCompositionDevice_Release(other);
    assert(a.refs == 1 && b.refs == 1 && unsupported.refs == 1);
    assert(!host_locks);
    puts("PASS: deferred attach/detach, replacement rollback, errors, ownership, reentry, concurrent commits, destroyed HWND");
    return 0;
}
