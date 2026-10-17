#ifndef MADEIRA_COMPOSITION_H
#define MADEIRA_COMPOSITION_H
#include <windows.h>
#include <unknwn.h>

/* Private bridge ABI, shared by dcomp.dll and the composition swapchain.
 * On a valid surface, NULL detach is infallible (S_OK), clears HWND/view/layer
 * and drains queued presents while preventing new caller enqueues.
 * Binding the same HWND is idempotent, and a different HWND
 * requires an explicit detach first. A failed non-NULL attach leaves the
 * previous HWND/view/layer binding unchanged. A successful bind uses the real target
 * window; it never selects the foreground window. IUnknown identity/refcount
 * remain those of the owning swapchain. */
static const IID IID_IMadeiraCompositionSurface =
    {0x8b6f2b77, 0x41a8, 0x4b60, {0x8e, 0x95, 0xc9, 0x35, 0x69, 0x5f, 0x61, 0x12}};

typedef struct IMadeiraCompositionSurface IMadeiraCompositionSurface;
typedef struct IMadeiraCompositionSurfaceVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(IMadeiraCompositionSurface *, REFIID, void **);
    ULONG (STDMETHODCALLTYPE *AddRef)(IMadeiraCompositionSurface *);
    ULONG (STDMETHODCALLTYPE *Release)(IMadeiraCompositionSurface *);
    HRESULT (STDMETHODCALLTYPE *BindToHwnd)(IMadeiraCompositionSurface *, HWND);
} IMadeiraCompositionSurfaceVtbl;
struct IMadeiraCompositionSurface {
    const IMadeiraCompositionSurfaceVtbl *lpVtbl;
};
#endif
