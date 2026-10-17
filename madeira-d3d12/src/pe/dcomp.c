/* Minimal DirectComposition Device1 bridge for Madeira composition swapchains.
 *
 * Supported: one topmost HWND target per device, one root visual containing an
 * IMadeiraCompositionSurface, and synchronous commits. Transforms, animations,
 * visual trees and DirectComposition drawing surfaces are explicitly refused.
 * The HWND always comes from CreateTargetForHwnd; no foreground-window guessing.
 */
#define COBJMACROS
#define CONST_VTABLE
#define WIN32_LEAN_AND_MEAN
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include <initguid.h>
#include <dcomp.h>
#include "madeira_composition.h"

/* The pinned MinGW winerror.h omits this documented DComp error. The value is
 * the Windows/Wine winerror.h definition, not a new Madeira HRESULT. */
#ifndef DCOMPOSITION_ERROR_WINDOW_ALREADY_COMPOSED
#define DCOMPOSITION_ERROR_WINDOW_ALREADY_COMPOSED ((HRESULT)0x88980800)
#endif

struct composition_device;
struct composition_visual;
struct composition_target;

/* An internal reference-counted holder permits snapshots under the device
 * lock without calling arbitrary external IUnknown methods while locked. */
struct surface_binding {
    LONG refs;
    IMadeiraCompositionSurface *surface;
};
struct composition_device {
    IDCompositionDevice iface;
    LONG refs;
    LONG committing;
    LONG committing_tid;
    CRITICAL_SECTION lock;
    struct composition_target *target; /* non-owning; protected by lock */
};
struct composition_visual {
    IDCompositionVisual iface;
    LONG refs;
    struct composition_device *device;
    struct surface_binding *content;
};
struct composition_target {
    IDCompositionTarget iface;
    LONG refs;
    BOOL destroying;
    struct composition_device *device;
    HWND hwnd;
    struct composition_visual *root;
    struct surface_binding *applied;
    struct composition_target *registry_next;
};

static SRWLOCK target_registry_lock = SRWLOCK_INIT;
static struct composition_target *target_registry;
static LONG live_devices;
static const IDCompositionDeviceVtbl device_vtable;
static const IDCompositionVisualVtbl visual_vtable;
static const IDCompositionTargetVtbl target_vtable;

static void binding_addref(struct surface_binding *binding)
{
    if (binding) InterlockedIncrement(&binding->refs);
}
static void binding_release(struct surface_binding *binding)
{
    if (binding && !InterlockedDecrement(&binding->refs)) {
        binding->surface->lpVtbl->Release(binding->surface);
        free(binding);
    }
}
static HRESULT binding_bind(struct surface_binding *binding, HWND hwnd)
{
    return binding->surface->lpVtbl->BindToHwnd(binding->surface, hwnd);
}
static ULONG STDMETHODCALLTYPE device_addref(IDCompositionDevice *iface)
{
    return InterlockedIncrement(&((struct composition_device *)iface)->refs);
}
static ULONG STDMETHODCALLTYPE device_release(IDCompositionDevice *iface)
{
    struct composition_device *device = (struct composition_device *)iface;
    ULONG refs = InterlockedDecrement(&device->refs);
    if (!refs) {
        DeleteCriticalSection(&device->lock);
        InterlockedDecrement(&live_devices);
        free(device);
    }
    return refs;
}
static HRESULT STDMETHODCALLTYPE device_query(IDCompositionDevice *iface, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_POINTER;
    if (!IsEqualIID(iid, &IID_IUnknown) && !IsEqualIID(iid, &IID_IDCompositionDevice)) return E_NOINTERFACE;
    *out = iface;
    device_addref(iface);
    return S_OK;
}
static ULONG STDMETHODCALLTYPE visual_addref(IDCompositionVisual *iface)
{
    return InterlockedIncrement(&((struct composition_visual *)iface)->refs);
}
static ULONG STDMETHODCALLTYPE visual_release(IDCompositionVisual *iface)
{
    struct composition_visual *visual = (struct composition_visual *)iface;
    ULONG refs = InterlockedDecrement(&visual->refs);
    if (!refs) {
        binding_release(visual->content);
        device_release(&visual->device->iface);
        free(visual);
    }
    return refs;
}
static HRESULT STDMETHODCALLTYPE visual_query(IDCompositionVisual *iface, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_POINTER;
    if (!IsEqualIID(iid, &IID_IUnknown) && !IsEqualIID(iid, &IID_IDCompositionVisual)) return E_NOINTERFACE;
    *out = iface;
    visual_addref(iface);
    return S_OK;
}
static ULONG STDMETHODCALLTYPE target_addref(IDCompositionTarget *iface)
{
    return InterlockedIncrement(&((struct composition_target *)iface)->refs);
}
static ULONG STDMETHODCALLTYPE target_release(IDCompositionTarget *iface)
{
    struct composition_target *target = (struct composition_target *)iface;
    struct composition_device *device = target->device;
    ULONG refs;
    EnterCriticalSection(&device->lock);
    refs = InterlockedDecrement(&target->refs);
    if (!refs) target->destroying = TRUE;
    LeaveCriticalSection(&device->lock);
    if (!refs) {
        /* Keep the registry entry until detachment finishes. A different device
         * cannot bind the same HWND and then be detached by this destruction. */
        if (target->applied) binding_bind(target->applied, NULL);
        EnterCriticalSection(&device->lock);
        AcquireSRWLockExclusive(&target_registry_lock);
        struct composition_target **link = &target_registry;
        while (*link && *link != target) link = &(*link)->registry_next;
        if (*link) *link = target->registry_next;
        device->target = NULL;
        ReleaseSRWLockExclusive(&target_registry_lock);
        LeaveCriticalSection(&device->lock);
        binding_release(target->applied);
        if (target->root) visual_release(&target->root->iface);
        device_release(&device->iface);
        free(target);
    }
    return refs;
}
static HRESULT STDMETHODCALLTYPE target_query(IDCompositionTarget *iface, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_POINTER;
    if (!IsEqualIID(iid, &IID_IUnknown) && !IsEqualIID(iid, &IID_IDCompositionTarget)) return E_NOINTERFACE;
    *out = iface;
    target_addref(iface);
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE visual_set_content(IDCompositionVisual *iface, IUnknown *content)
{
    struct composition_visual *visual = (struct composition_visual *)iface;
    struct surface_binding *next = NULL, *previous;
    if (content) {
        IMadeiraCompositionSurface *surface = NULL;
        HRESULT hr = IUnknown_QueryInterface(content, &IID_IMadeiraCompositionSurface, (void **)&surface);
        if (FAILED(hr)) return hr;
        if (!surface) return E_NOINTERFACE;
        next = calloc(1, sizeof(*next));
        if (!next) {
            surface->lpVtbl->Release(surface);
            return E_OUTOFMEMORY;
        }
        next->refs = 1;
        next->surface = surface;
    }
    EnterCriticalSection(&visual->device->lock);
    previous = visual->content;
    visual->content = next;
    LeaveCriticalSection(&visual->device->lock);
    binding_release(previous);
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE target_set_root(IDCompositionTarget *iface, IDCompositionVisual *root)
{
    struct composition_target *target = (struct composition_target *)iface;
    struct composition_visual *next = (struct composition_visual *)root, *previous;
    if (root && (root->lpVtbl != &visual_vtable || next->device != target->device)) return E_INVALIDARG;
    EnterCriticalSection(&target->device->lock);
    if (next) visual_addref(root);
    previous = target->root;
    target->root = next;
    LeaveCriticalSection(&target->device->lock);
    if (previous) visual_release(&previous->iface);
    return S_OK;
}
static HRESULT validate_hwnd(HWND hwnd)
{
    DWORD owner = 0;
    if (!hwnd || !IsWindow(hwnd) || !GetWindowThreadProcessId(hwnd, &owner))
        return HRESULT_FROM_WIN32(ERROR_INVALID_WINDOW_HANDLE);
    if (owner != GetCurrentProcessId()) return E_ACCESSDENIED;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE device_create_target(IDCompositionDevice *iface, HWND hwnd, BOOL topmost, IDCompositionTarget **out)
{
    struct composition_device *device = (struct composition_device *)iface;
    struct composition_target *target, *other;
    HRESULT hr;
    if (!out) return E_POINTER;
    *out = NULL;
    if (FAILED(hr = validate_hwnd(hwnd))) return hr;
    if (!topmost) return E_NOTIMPL;
    target = calloc(1, sizeof(*target));
    if (!target) return E_OUTOFMEMORY;
    target->iface.lpVtbl = &target_vtable;
    target->refs = 1;
    target->device = device;
    target->hwnd = hwnd;
    EnterCriticalSection(&device->lock);
    if (device->target) {
        LeaveCriticalSection(&device->lock);
        free(target);
        return E_NOTIMPL; /* this bridge deliberately supports one target */
    }
    AcquireSRWLockExclusive(&target_registry_lock);
    for (other = target_registry; other; other = other->registry_next) {
        if (other->hwnd == hwnd) break;
    }
    if (other) hr = DCOMPOSITION_ERROR_WINDOW_ALREADY_COMPOSED;
    else {
        target->registry_next = target_registry;
        target_registry = target;
        device->target = target;
        device_addref(iface);
        *out = &target->iface;
        hr = S_OK;
    }
    ReleaseSRWLockExclusive(&target_registry_lock);
    LeaveCriticalSection(&device->lock);
    if (FAILED(hr)) free(target);
    return hr;
}
static HRESULT STDMETHODCALLTYPE device_create_visual(IDCompositionDevice *iface, IDCompositionVisual **out)
{
    struct composition_visual *visual;
    if (!out) return E_POINTER;
    *out = NULL;
    visual = calloc(1, sizeof(*visual));
    if (!visual) return E_OUTOFMEMORY;
    visual->iface.lpVtbl = &visual_vtable;
    visual->refs = 1;
    visual->device = (struct composition_device *)iface;
    device_addref(iface);
    *out = &visual->iface;
    return S_OK;
}
static void target_set_applied(struct composition_target *target, struct surface_binding *binding)
{
    struct surface_binding *previous;
    EnterCriticalSection(&target->device->lock);
    binding_addref(binding);
    previous = target->applied;
    target->applied = binding;
    LeaveCriticalSection(&target->device->lock);
    binding_release(previous);
}
static HRESULT STDMETHODCALLTYPE device_commit(IDCompositionDevice *iface)
{
    struct composition_device *device = (struct composition_device *)iface;
    struct composition_target *target = NULL;
    struct surface_binding *next = NULL, *previous = NULL;
    HRESULT hr = S_OK, rollback;
    HWND hwnd = NULL;
    if (InterlockedCompareExchange(&device->committing, 1, 0)) return RPC_E_CALL_REJECTED;
    InterlockedExchange(&device->committing_tid, (LONG)GetCurrentThreadId());
    EnterCriticalSection(&device->lock);
    if (device->target && device->target->destroying) hr = RPC_E_CALL_REJECTED;
    else if ((target = device->target)) {
        target_addref(&target->iface);
        hwnd = target->hwnd;
        if (target->root) next = target->root->content;
        previous = target->applied;
        binding_addref(next);
        binding_addref(previous);
    }
    LeaveCriticalSection(&device->lock);
    if (target) {
        hr = validate_hwnd(hwnd);
        if (FAILED(hr)) {
            /* A destroyed/foreign HWND cannot stay bound. Pending root/content
             * remain intact so the failure is visible and no fake commit occurs. */
            if (previous) {
                HRESULT detach = binding_bind(previous, NULL);
                if (FAILED(detach)) hr = detach;
                else target_set_applied(target, NULL);
            }
        } else if (next != previous) {
            if (previous) hr = binding_bind(previous, NULL);
            if (SUCCEEDED(hr)) {
                if (next) hr = binding_bind(next, hwnd);
                if (FAILED(hr)) {
                    if (previous && FAILED(rollback = binding_bind(previous, hwnd))) {
                        target_set_applied(target, NULL);
                        hr = rollback;
                    }
                } else target_set_applied(target, next);
            }
        }
    }
    binding_release(next);
    binding_release(previous);
    InterlockedExchange(&device->committing_tid, 0);
    InterlockedExchange(&device->committing, 0);
    if (target) target_release(&target->iface);
    return hr;
}
static HRESULT STDMETHODCALLTYPE device_wait(IDCompositionDevice *iface)
{
    struct composition_device *device = (struct composition_device *)iface;
    while (InterlockedCompareExchange(&device->committing, 0, 0)) {
        /* Calling Wait from a surface callback inside this same Commit would
         * wait on itself. Ordinary concurrent callers wait for completion. */
        if ((DWORD)InterlockedCompareExchange(&device->committing_tid, 0, 0) == GetCurrentThreadId())
            return RPC_E_CALL_REJECTED;
        Sleep(0);
    }
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE device_statistics(IDCompositionDevice *iface, DCOMPOSITION_FRAME_STATISTICS *out)
{
    (void)iface;
    if (!out) return E_POINTER;
    memset(out, 0, sizeof(*out));
    return E_NOTIMPL;
}
static HRESULT unsupported_out(void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    return E_NOTIMPL;
}
#define DEVICE_OUT_STUB(name, type) \
static HRESULT STDMETHODCALLTYPE name(IDCompositionDevice *iface, type **out) \
{ (void)iface; return unsupported_out((void **)out); }
DEVICE_OUT_STUB(device_translate, IDCompositionTranslateTransform)
DEVICE_OUT_STUB(device_scale, IDCompositionScaleTransform)
DEVICE_OUT_STUB(device_rotate, IDCompositionRotateTransform)
DEVICE_OUT_STUB(device_skew, IDCompositionSkewTransform)
DEVICE_OUT_STUB(device_matrix, IDCompositionMatrixTransform)
DEVICE_OUT_STUB(device_translate3d, IDCompositionTranslateTransform3D)
DEVICE_OUT_STUB(device_scale3d, IDCompositionScaleTransform3D)
DEVICE_OUT_STUB(device_rotate3d, IDCompositionRotateTransform3D)
DEVICE_OUT_STUB(device_matrix3d, IDCompositionMatrixTransform3D)
DEVICE_OUT_STUB(device_effect, IDCompositionEffectGroup)
DEVICE_OUT_STUB(device_clip, IDCompositionRectangleClip)
DEVICE_OUT_STUB(device_animation, IDCompositionAnimation)
#undef DEVICE_OUT_STUB
static HRESULT STDMETHODCALLTYPE device_surface(IDCompositionDevice *iface, UINT w, UINT h, DXGI_FORMAT format, DXGI_ALPHA_MODE alpha, IDCompositionSurface **out)
{ (void)iface; (void)w; (void)h; (void)format; (void)alpha; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_virtual_surface(IDCompositionDevice *iface, UINT w, UINT h, DXGI_FORMAT format, DXGI_ALPHA_MODE alpha, IDCompositionVirtualSurface **out)
{ (void)iface; (void)w; (void)h; (void)format; (void)alpha; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_handle_surface(IDCompositionDevice *iface, HANDLE handle, IUnknown **out)
{ (void)iface; (void)handle; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_hwnd_surface(IDCompositionDevice *iface, HWND hwnd, IUnknown **out)
{ (void)iface; (void)hwnd; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_group(IDCompositionDevice *iface, IDCompositionTransform **items, UINT count, IDCompositionTransform **out)
{ (void)iface; (void)items; (void)count; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_group3d(IDCompositionDevice *iface, IDCompositionTransform3D **items, UINT count, IDCompositionTransform3D **out)
{ (void)iface; (void)items; (void)count; return unsupported_out((void **)out); }
static HRESULT STDMETHODCALLTYPE device_check_state(IDCompositionDevice *iface, BOOL *valid)
{ (void)iface; if (!valid) return E_POINTER; *valid = TRUE; return S_OK; }
#define VISUAL_STUB(name, type) \
static HRESULT STDMETHODCALLTYPE name(IDCompositionVisual *iface, type value) \
{ (void)iface; (void)value; return E_NOTIMPL; }
VISUAL_STUB(visual_animation, IDCompositionAnimation *)
VISUAL_STUB(visual_offset, float)
VISUAL_STUB(visual_transform, IDCompositionTransform *)
VISUAL_STUB(visual_matrix, const D2D_MATRIX_3X2_F *)
VISUAL_STUB(visual_transform_parent, IDCompositionVisual *)
VISUAL_STUB(visual_effect, IDCompositionEffect *)
VISUAL_STUB(visual_interpolation, enum DCOMPOSITION_BITMAP_INTERPOLATION_MODE)
VISUAL_STUB(visual_border, enum DCOMPOSITION_BORDER_MODE)
VISUAL_STUB(visual_clip, IDCompositionClip *)
VISUAL_STUB(visual_clip_rect, const D2D_RECT_F *)
VISUAL_STUB(visual_remove, IDCompositionVisual *)
VISUAL_STUB(visual_composite, enum DCOMPOSITION_COMPOSITE_MODE)
#undef VISUAL_STUB
static HRESULT STDMETHODCALLTYPE visual_add(IDCompositionVisual *iface, IDCompositionVisual *visual, BOOL above, IDCompositionVisual *reference)
{ (void)iface; (void)visual; (void)above; (void)reference; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE visual_remove_all(IDCompositionVisual *iface)
{ (void)iface; return E_NOTIMPL; }

static const IDCompositionDeviceVtbl device_vtable = {
    device_query, device_addref, device_release, device_commit, device_wait,
    device_statistics, device_create_target, device_create_visual, device_surface,
    device_virtual_surface, device_handle_surface, device_hwnd_surface,
    device_translate, device_scale, device_rotate, device_skew, device_matrix,
    device_group, device_translate3d, device_scale3d, device_rotate3d,
    device_matrix3d, device_group3d, device_effect, device_clip, device_animation,
    device_check_state
};
static const IDCompositionVisualVtbl visual_vtable = {
    visual_query, visual_addref, visual_release, visual_animation, visual_offset,
    visual_animation, visual_offset, visual_transform, visual_matrix,
    visual_transform_parent, visual_effect, visual_interpolation, visual_border,
    visual_clip, visual_clip_rect, visual_set_content, visual_add, visual_remove,
    visual_remove_all, visual_composite
};
static const IDCompositionTargetVtbl target_vtable = {
    target_query, target_addref, target_release, target_set_root
};

HRESULT WINAPI DCompositionCreateDevice(IDXGIDevice *rendering_device, REFIID iid, void **out)
{
    struct composition_device *device;
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_POINTER;
    if (rendering_device) return E_NOTIMPL;
    if (!IsEqualIID(iid, &IID_IDCompositionDevice) && !IsEqualIID(iid, &IID_IUnknown)) return E_NOINTERFACE;
    device = calloc(1, sizeof(*device));
    if (!device) return E_OUTOFMEMORY;
    device->iface.lpVtbl = &device_vtable;
    device->refs = 1;
    InitializeCriticalSection(&device->lock);
    InterlockedIncrement(&live_devices);
    *out = &device->iface;
    return S_OK;
}
HRESULT WINAPI DCompositionCreateDevice2(IUnknown *rendering_device, REFIID iid, void **out)
{ (void)rendering_device; (void)iid; return unsupported_out(out); }
HRESULT WINAPI DCompositionCreateDevice3(IUnknown *rendering_device, REFIID iid, void **out)
{ (void)rendering_device; (void)iid; return unsupported_out(out); }
HRESULT WINAPI DllCanUnloadNow(void)
{ return InterlockedCompareExchange(&live_devices, 0, 0) ? S_FALSE : S_OK; }
