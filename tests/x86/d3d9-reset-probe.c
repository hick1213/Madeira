/* Ordinary D3D9 Reset contract diagnostic for a PE32 Madeira session.
 * No CRT, shaders, executable-memory allocation, or cross-process access.
 * Build on APFS with the repository's i686-w64-mingw32-clang:
 *   -O2 -Wall -Wextra -Werror -ffreestanding -fno-builtin -nostdlib
 *   -Wl,--entry=_start -Wl,--large-address-aware -lkernel32 -luser32 -ld3d9
 * Exit 0 means every applicable assertion passed; 1 is a failed assertion,
 * 124 is the 60-second watchdog, and 125 is watchdog setup failure.
 */
#define COBJMACROS
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <d3d9.h>
#include <stddef.h>

/* Exact borrowed interface ABI/IID from dxmt/src/d3d9/d3d9_diag.hpp.
 * It has no IUnknown slots. Never AddRef or Release this borrowed pointer. */
struct reset_diag;
struct reset_diag_vtable {
    UINT (STDMETHODCALLTYPE *GetLosableResourceCount)(struct reset_diag *self);
};
struct reset_diag { const struct reset_diag_vtable *lpVtbl; };
static const GUID reset_diag_iid = {
    0xd2c7b12b, 0xd9d9, 0x4d9d, {0x8a, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x00, 0x01}
};

static BOOL failed;
static struct reset_diag *diag;
static HANDLE finished_event;

void *memset(void *destination, int value, size_t size)
{
    unsigned char *p = destination;
    while (size--) *p++ = (unsigned char)value;
    return destination;
}

void *memcpy(void *destination, const void *source, size_t size)
{
    unsigned char *p = destination;
    const unsigned char *q = source;
    while (size--) *p++ = *q++;
    return destination;
}

struct line { char text[320]; unsigned length; };

static void append(struct line *line, const char *text)
{
    while (*text && line->length < sizeof(line->text) - 1)
        line->text[line->length++] = *text++;
}

static void hex(struct line *line, DWORD value)
{
    static const char digits[] = "0123456789abcdef";
    append(line, "0x");
    for (unsigned shift = 32; shift; shift -= 4) {
        char digit[2] = {digits[(value >> (shift - 4)) & 15u], 0};
        append(line, digit);
    }
}

static struct line begin(const char *stage)
{
    struct line line;
    line.length = 0;
    append(&line, "D9-RESET stage=");
    append(&line, stage);
    return line;
}

static void output(struct line *line)
{
    HANDLE handle = GetStdHandle(STD_ERROR_HANDLE);
    DWORD written, offset = 0;
    append(line, "\n");
    line->text[line->length] = 0;
    OutputDebugStringA(line->text);
    if (handle && handle != INVALID_HANDLE_VALUE) {
        while (offset < line->length) {
            if (!WriteFile(handle, line->text + offset, line->length - offset,
                           &written, NULL) || !written) break;
            offset += written;
        }
    }
}

static BOOL expect_hr(const char *stage, HRESULT actual, HRESULT expected)
{
    struct line line = begin(stage);
    BOOL success = actual == expected;
    append(&line, " hr="); hex(&line, (DWORD)actual);
    append(&line, " expected="); hex(&line, (DWORD)expected);
    append(&line, success ? " PASS" : " FAIL");
    output(&line);
    if (!success) failed = TRUE;
    return success;
}

static BOOL expect_object(const char *stage, const void *object)
{
    struct line line = begin(stage);
    append(&line, object ? " nonnull=1 PASS" : " nonnull=0 FAIL error=");
    if (!object) {
        hex(&line, GetLastError());
        failed = TRUE;
    }
    output(&line);
    return object != NULL;
}

static void expect_count(const char *stage, UINT expected)
{
    struct line line = begin(stage);
    if (!diag) {
        append(&line, " diag=unsupported count=UNTESTED");
    } else {
        UINT actual = diag->lpVtbl->GetLosableResourceCount(diag);
        append(&line, " count="); hex(&line, actual);
        append(&line, " expected="); hex(&line, expected);
        append(&line, actual == expected ? " PASS" : " FAIL");
        if (actual != expected) failed = TRUE;
    }
    output(&line);
}

static DWORD WINAPI watchdog(void *unused)
{
    DWORD wait;
    struct line line;
    (void)unused;
    wait = WaitForSingleObject(finished_event, 60000);
    if (wait != WAIT_OBJECT_0) {
        line = begin(wait == WAIT_TIMEOUT ? "TIMEOUT" : "watchdog-wait-failed");
        append(&line, " wait="); hex(&line, wait);
        output(&line);
        ExitProcess(wait == WAIT_TIMEOUT ? 124 : 125);
    }
    return 0;
}

static HRESULT reset(IDirect3DDevice9 *device, HWND window)
{
    D3DPRESENT_PARAMETERS parameters = {0};
    parameters.BackBufferWidth = 640;
    parameters.BackBufferHeight = 480;
    parameters.BackBufferFormat = D3DFMT_A8R8G8B8;
    parameters.BackBufferCount = 1;
    parameters.SwapEffect = D3DSWAPEFFECT_DISCARD;
    parameters.hDeviceWindow = window;
    parameters.Windowed = TRUE;
    parameters.EnableAutoDepthStencil = TRUE;
    parameters.AutoDepthStencilFormat = D3DFMT_D24S8;
    parameters.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
    return IDirect3DDevice9_Reset(device, &parameters);
}

static void check_implicit_surfaces(IDirect3DDevice9 *device, HWND window)
{
    IDirect3DSurface9 *surface = NULL;
    HRESULT hr;
    hr = IDirect3DDevice9_GetBackBuffer(device, 0, 0, D3DBACKBUFFER_TYPE_MONO, &surface);
    if (expect_hr("GetBackBuffer", hr, D3D_OK) && surface) {
        expect_count("held-backbuffer", 1);
        expect_hr("Reset-held-backbuffer", reset(device, window), D3DERR_INVALIDCALL);
        IDirect3DSurface9_Release(surface);
        surface = NULL;
        expect_count("released-backbuffer", 0);
        expect_hr("Reset-released-backbuffer", reset(device, window), D3D_OK);
    } else {
        failed = TRUE;
        if (surface) IDirect3DSurface9_Release(surface);
    }
    surface = NULL;
    hr = IDirect3DDevice9_GetDepthStencilSurface(device, &surface);
    if (expect_hr("GetAutoDepthStencil", hr, D3D_OK) && surface) {
        expect_count("held-auto-depth-stencil", 1);
        expect_hr("Reset-held-auto-depth-stencil", reset(device, window), D3DERR_INVALIDCALL);
        IDirect3DSurface9_Release(surface);
        surface = NULL;
        expect_count("released-auto-depth-stencil", 0);
        expect_hr("Reset-released-auto-depth-stencil", reset(device, window), D3D_OK);
    } else {
        failed = TRUE;
        if (surface) IDirect3DSurface9_Release(surface);
    }
}

static void check_bound_render_target(IDirect3DDevice9 *device, HWND window)
{
    IDirect3DSurface9 *surface = NULL;
    HRESULT hr = IDirect3DDevice9_CreateRenderTarget(device, 64, 64, D3DFMT_A8R8G8B8,
                        D3DMULTISAMPLE_NONE, 0, FALSE, &surface, NULL);
    if (expect_hr("CreateDefaultRenderTarget", hr, D3D_OK) && surface) {
        expect_count("held-default-render-target", 1);
        expect_hr("Reset-held-default-render-target", reset(device, window), D3DERR_INVALIDCALL);
        expect_hr("UnbindDepthStencil-for-render-target",
                  IDirect3DDevice9_SetDepthStencilSurface(device, NULL), D3D_OK);
        expect_hr("BindDefaultRenderTarget", IDirect3DDevice9_SetRenderTarget(device, 0, surface), D3D_OK);
        IDirect3DSurface9_Release(surface);
        expect_count("bound-render-target-public-released", 0);
        expect_hr("Reset-private-bound-render-target", reset(device, window), D3D_OK);
        expect_count("reset-after-render-target", 0);
    } else {
        failed = TRUE;
        if (surface) IDirect3DSurface9_Release(surface);
    }
}

static void check_bound_render_texture(IDirect3DDevice9 *device, HWND window)
{
    IDirect3DTexture9 *texture = NULL;
    IDirect3DSurface9 *surface = NULL;
    HRESULT hr = IDirect3DDevice9_CreateTexture(device, 64, 64, 1, D3DUSAGE_RENDERTARGET,
                        D3DFMT_A8R8G8B8, D3DPOOL_DEFAULT, &texture, NULL);
    if (!expect_hr("CreateDefaultRenderTexture", hr, D3D_OK) || !texture) {
        failed = TRUE;
        if (texture) IDirect3DTexture9_Release(texture);
        return;
    }
    expect_count("held-default-render-texture", 1);
    expect_hr("Reset-held-default-render-texture", reset(device, window), D3DERR_INVALIDCALL);
    hr = IDirect3DTexture9_GetSurfaceLevel(texture, 0, &surface);
    if (expect_hr("GetRenderTextureSurface", hr, D3D_OK) && surface) {
        expect_count("held-render-texture-and-level", 1);
        expect_hr("UnbindDepthStencil-for-texture",
                  IDirect3DDevice9_SetDepthStencilSurface(device, NULL), D3D_OK);
        expect_hr("BindRenderTextureSurface", IDirect3DDevice9_SetRenderTarget(device, 0, surface), D3D_OK);
        IDirect3DTexture9_Release(texture);
        texture = NULL;
        expect_count("held-render-texture-level-only", 1);
        IDirect3DSurface9_Release(surface);
        surface = NULL;
        expect_count("bound-render-texture-public-released", 0);
        expect_hr("Reset-private-bound-render-texture", reset(device, window), D3D_OK);
        expect_count("reset-after-render-texture", 0);
    } else {
        failed = TRUE;
    }
    if (surface) IDirect3DSurface9_Release(surface);
    if (texture) IDirect3DTexture9_Release(texture);
}

void start(void)
{
    HANDLE watchdog_thread;
    HWND window = NULL;
    IDirect3D9 *d3d = NULL;
    IDirect3DDevice9 *device = NULL;
    D3DPRESENT_PARAMETERS parameters = {0};
    HRESULT hr;
    struct line line;
    BOOL emulated;

    finished_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!expect_object("CreateWatchdogEvent", finished_event)) ExitProcess(125);
    watchdog_thread = CreateThread(NULL, 0, watchdog, NULL, 0, NULL);
    if (!expect_object("CreateWatchdogThread", watchdog_thread)) {
        CloseHandle(finished_event);
        ExitProcess(125);
    }
    window = CreateWindowExA(0, "STATIC", "D3D9 Reset diagnostic",
                            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                            0, 0, 640, 480, NULL, NULL, GetModuleHandleA(NULL), NULL);
    if (!expect_object("CreateWindow", window)) goto cleanup;
    d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!expect_object("Direct3DCreate9", d3d)) goto cleanup;
    parameters.BackBufferWidth = 640;
    parameters.BackBufferHeight = 480;
    parameters.BackBufferFormat = D3DFMT_A8R8G8B8;
    parameters.BackBufferCount = 1;
    parameters.SwapEffect = D3DSWAPEFFECT_DISCARD;
    parameters.hDeviceWindow = window;
    parameters.Windowed = TRUE;
    parameters.EnableAutoDepthStencil = TRUE;
    parameters.AutoDepthStencilFormat = D3DFMT_D24S8;
    parameters.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
    hr = IDirect3D9_CreateDevice(d3d, 0, D3DDEVTYPE_HAL, window,
                               D3DCREATE_HARDWARE_VERTEXPROCESSING | D3DCREATE_MULTITHREADED,
                               &parameters, &device);
    if (!expect_hr("CreateDevice", hr, D3D_OK) || !device) { failed = TRUE; goto cleanup; }
    emulated = GetModuleHandleA("d3d9-emulated.dll") != NULL;
    line = begin("provider");
    append(&line, emulated ? " d3d9-emulated=loaded" : " d3d9-emulated=not-loaded");
    output(&line);
    hr = IDirect3DDevice9_QueryInterface(device, &reset_diag_iid, (void **)&diag);
    if (emulated) {
        if (!expect_hr("QueryResetDiagnostic", hr, S_OK) ||
            !expect_object("BorrowedResetDiagnostic", diag)) goto cleanup;
    } else if (hr != S_OK || !diag) {
        diag = NULL;
        if (hr != E_NOINTERFACE) failed = TRUE;
        line = begin("QueryResetDiagnostic");
        append(&line, " hr="); hex(&line, (DWORD)hr);
        append(&line, " borrowed-diag=unsupported behavior-assertions=active");
        output(&line);
    }
    expect_count("initial", 0);
    expect_hr("Reset-baseline", reset(device, window), D3D_OK);
    expect_count("baseline", 0);
    check_implicit_surfaces(device, window);
    check_bound_render_target(device, window);
    check_bound_render_texture(device, window);
    expect_count("final", 0);
    expect_hr("Reset-final", reset(device, window), D3D_OK);

cleanup:
    diag = NULL;
    if (device) IDirect3DDevice9_Release(device);
    if (d3d) IDirect3D9_Release(d3d);
    if (window && !DestroyWindow(window)) failed = TRUE;
    if (!SetEvent(finished_event) || WaitForSingleObject(watchdog_thread, 5000) != WAIT_OBJECT_0)
        ExitProcess(125);
    CloseHandle(watchdog_thread);
    CloseHandle(finished_event);
    line = begin(failed ? "FAIL-exit-1" : "PASS-exit-0");
    output(&line);
    ExitProcess(failed ? 1 : 0);
}
