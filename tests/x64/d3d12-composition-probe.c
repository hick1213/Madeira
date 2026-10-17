/* Real composition contract probe; no custom presenter or foreground HWND.
 * https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgifactory2-createswapchainforcomposition
 * Missing composition creation/binding, resize and detach must fail the test.
 * API_PASS does not assert visually verified pixels. */
#define _WIN32_WINNT 0x0a00
#define WIDL_C_INLINE_WRAPPERS
#define COBJMACROS
#define INITGUID
#include <initguid.h>
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
/* Pinned mingw dcomp.h is C++-only (overloaded declarations). These are
 * ABI-only C declarations from that header: IUnknown, exact method slots,
 * and opaque unused slots. Calls still reach the real dcomp.dll objects. */
typedef struct IDCompositionDevice IDCompositionDevice;
typedef struct IDCompositionTarget IDCompositionTarget;
typedef struct IDCompositionVisual IDCompositionVisual;
#define UNKNOWN_METHODS(T) HRESULT (STDMETHODCALLTYPE *QueryInterface)(T *, REFIID, void **); \
    ULONG (STDMETHODCALLTYPE *AddRef)(T *); ULONG (STDMETHODCALLTYPE *Release)(T *)
typedef struct {
    UNKNOWN_METHODS(IDCompositionDevice);
    HRESULT (STDMETHODCALLTYPE *Commit)(IDCompositionDevice *);
    HRESULT (STDMETHODCALLTYPE *WaitForCommitCompletion)(IDCompositionDevice *);
    HRESULT (STDMETHODCALLTYPE *GetFrameStatistics)(IDCompositionDevice *, void *);
    HRESULT (STDMETHODCALLTYPE *CreateTargetForHwnd)(IDCompositionDevice *, HWND, BOOL, IDCompositionTarget **);
    HRESULT (STDMETHODCALLTYPE *CreateVisual)(IDCompositionDevice *, IDCompositionVisual **);
    HRESULT (STDMETHODCALLTYPE *unused[19])(void);
} CompositionDeviceVtbl;
struct IDCompositionDevice { const CompositionDeviceVtbl *lpVtbl; };
typedef struct {
    UNKNOWN_METHODS(IDCompositionTarget);
    HRESULT (STDMETHODCALLTYPE *SetRoot)(IDCompositionTarget *, IDCompositionVisual *);
} CompositionTargetVtbl;
struct IDCompositionTarget { const CompositionTargetVtbl *lpVtbl; };
typedef struct {
    UNKNOWN_METHODS(IDCompositionVisual);
    HRESULT (STDMETHODCALLTYPE *unused_before_content[12])(void);
    HRESULT (STDMETHODCALLTYPE *SetContent)(IDCompositionVisual *, IUnknown *);
    HRESULT (STDMETHODCALLTYPE *unused_after_content[4])(void);
} CompositionVisualVtbl;
struct IDCompositionVisual { const CompositionVisualVtbl *lpVtbl; };
DEFINE_GUID(IID_IDCompositionDevice,0xc37ea93a,0xe7aa,0x450d,0xb1,0x6f,0x97,0x46,0xcb,0x04,0x07,0xf3);
#define IDCompositionDevice_Commit(p) ((p)->lpVtbl->Commit(p))
#define IDCompositionDevice_CreateTargetForHwnd(p,h,t,o) ((p)->lpVtbl->CreateTargetForHwnd(p,h,t,o))
#define IDCompositionDevice_CreateVisual(p,o) ((p)->lpVtbl->CreateVisual(p,o))
#define IDCompositionDevice_Release(p) ((p)->lpVtbl->Release(p))
#define IDCompositionTarget_SetRoot(p,v) ((p)->lpVtbl->SetRoot(p,v))
#define IDCompositionTarget_Release(p) ((p)->lpVtbl->Release(p))
#define IDCompositionVisual_SetContent(p,c) ((p)->lpVtbl->SetContent(p,c))
#define IDCompositionVisual_Release(p) ((p)->lpVtbl->Release(p))
#include <stdio.h>
#include <stdint.h>

typedef HRESULT (WINAPI *create_device_fn)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);
typedef HRESULT (WINAPI *create_factory_fn)(REFIID, void **);
typedef HRESULT (WINAPI *create_composition_fn)(IDXGIDevice *, REFIID, void **);
static volatile LONG done, frame_count;
static BOOL running = TRUE, failed;
static void note(const char *stage, HRESULT hr) {
    char line[256];
    typedef int (__cdecl *debug_fn)(const char *);
    HMODULE nt = GetModuleHandleA("ntdll.dll");
    debug_fn debug = nt ? (debug_fn)(uintptr_t)GetProcAddress(nt, "__wine_dbg_output") : NULL;
    snprintf(line, sizeof(line), "COMPOSITION stage=%s hr=0x%08lx frames=%ld\n", stage,
             (unsigned long)hr, (long)InterlockedCompareExchange(&frame_count, 0, 0));
    OutputDebugStringA(line);
    if (debug) debug(line);
}
static BOOL check(const char *stage, HRESULT hr) {
    note(stage, hr);
    if (FAILED(hr)) { failed = TRUE; return FALSE; }
    return TRUE;
}
static HRESULT win_error(void) {
    DWORD error = GetLastError();
    return error ? HRESULT_FROM_WIN32(error) : E_FAIL;
}
#define OBJECT(stage, call, pointer) do { if (!check(stage, (call))) goto cleanup; \
    if (!(pointer)) { check(stage "-null", E_UNEXPECTED); goto cleanup; } } while (0)
#define RELEASE(type, pointer) do { if (pointer) { type##_Release(pointer); pointer = NULL; } } while (0)
static DWORD WINAPI watchdog(void *unused) {
    (void)unused; Sleep(15000);
    if (!InterlockedCompareExchange(&done, 0, 0)) {
        note("TIMEOUT-exit-90", HRESULT_FROM_WIN32(WAIT_TIMEOUT)); ExitProcess(90);
    }
    return 0;
}
static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_CLOSE) { running = FALSE; return 0; }
    if (message == WM_DESTROY) { running = FALSE; PostQuitMessage(0); return 0; }
    return DefWindowProcA(window, message, wparam, lparam);
}
static void pump(void) {
    MSG m;
    while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE)) {
        if (m.message == WM_QUIT) running = FALSE;
        else { TranslateMessage(&m); DispatchMessageA(&m); }
    }
}
struct probe {
    ID3D12Device *device; ID3D12CommandQueue *queue; ID3D12CommandAllocator *allocator;
    ID3D12GraphicsCommandList *list; ID3D12DescriptorHeap *heap; ID3D12Fence *fence;
    IDXGISwapChain3 *swap; ID3D12Resource *buffers[2]; HANDLE event; UINT64 serial; UINT stride; BOOL submitted;
};
static void release_buffers(struct probe *p) {
    for (unsigned i = 0; i < 2; ++i)
        if (p->buffers[i]) { ID3D12Resource_Release(p->buffers[i]); p->buffers[i] = NULL; }
}
static BOOL acquire_buffers(struct probe *p, UINT width, UINT height) {
    D3D12_CPU_DESCRIPTOR_HANDLE handle = ID3D12DescriptorHeap_GetCPUDescriptorHandleForHeapStart(p->heap);
    for (unsigned i = 0; i < 2; ++i) {
        D3D12_RESOURCE_DESC desc;
        if (!check("GetBuffer", IDXGISwapChain3_GetBuffer(p->swap, i, &IID_ID3D12Resource, (void **)&p->buffers[i]))) return FALSE;
        if (!p->buffers[i]) return check("GetBuffer-null", E_UNEXPECTED);
        desc = ID3D12Resource_GetDesc(p->buffers[i]);
        if (desc.Width != width || desc.Height != height) return check("GetBuffer-size", E_UNEXPECTED);
        ID3D12Device_CreateRenderTargetView(p->device, p->buffers[i], NULL, handle);
        handle.ptr += p->stride;
    }
    return TRUE;
}
static BOOL wait_gpu(struct probe *p) {
    HRESULT hr = ID3D12CommandQueue_Signal(p->queue, p->fence, ++p->serial);
    UINT64 completed;
    if (FAILED(hr)) return check("Signal", hr);
    completed = ID3D12Fence_GetCompletedValue(p->fence);
    if (completed == UINT64_MAX) return check("Fence-device-removed", DXGI_ERROR_DEVICE_REMOVED);
    if (completed < p->serial) {
        hr = ID3D12Fence_SetEventOnCompletion(p->fence, p->serial, p->event);
        if (FAILED(hr)) return check("SetEventOnCompletion", hr);
        DWORD wait = WaitForSingleObject(p->event, 2000);
        if (wait != WAIT_OBJECT_0) return check("GPU-wait", wait == WAIT_FAILED ? win_error() : HRESULT_FROM_WIN32(WAIT_TIMEOUT));
    }
    completed = ID3D12Fence_GetCompletedValue(p->fence);
    if (completed == UINT64_MAX || completed < p->serial) return check("GPU-incomplete", E_UNEXPECTED);
    p->submitted = FALSE;
    return TRUE;
}
static BOOL render(struct probe *p, BOOL cyan) {
    const FLOAT color[4] = { cyan ? 0.0f : 1.0f, cyan ? 1.0f : 0.0f, 1.0f, 1.0f };
    UINT index = IDXGISwapChain3_GetCurrentBackBufferIndex(p->swap);
    D3D12_RESOURCE_BARRIER barrier = {0};
    D3D12_CPU_DESCRIPTOR_HANDLE handle = ID3D12DescriptorHeap_GetCPUDescriptorHandleForHeapStart(p->heap);
    HRESULT hr;
    if (index >= 2) return check("back-buffer-index", E_UNEXPECTED);
    hr = ID3D12CommandAllocator_Reset(p->allocator); if (FAILED(hr)) return check("AllocatorReset", hr);
    hr = ID3D12GraphicsCommandList_Reset(p->list, p->allocator, NULL); if (FAILED(hr)) return check("ListReset", hr);
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = p->buffers[index]; barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT; barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
    ID3D12GraphicsCommandList_ResourceBarrier(p->list, 1, &barrier);
    handle.ptr += index * p->stride;
    ID3D12GraphicsCommandList_ClearRenderTargetView(p->list, handle, color, 0, NULL);
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET; barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
    ID3D12GraphicsCommandList_ResourceBarrier(p->list, 1, &barrier);
    hr = ID3D12GraphicsCommandList_Close(p->list); if (FAILED(hr)) return check("ListClose", hr);
    { ID3D12CommandList *lists[] = {(ID3D12CommandList *)p->list}; ID3D12CommandQueue_ExecuteCommandLists(p->queue, 1, lists); p->submitted = TRUE; }
    hr = IDXGISwapChain3_Present(p->swap, 1, 0);
    if (InterlockedCompareExchange(&frame_count, 0, 0) < 3 || FAILED(hr)) note("Present", hr);
    if (FAILED(hr)) { failed = TRUE; return FALSE; }
    InterlockedIncrement(&frame_count);
    return wait_gpu(p);
}
int main(void) {
    struct probe p = {0}; IDXGIFactory2 *factory = NULL; IDXGISwapChain1 *swap1 = NULL;
    IDCompositionDevice *composition = NULL; IDCompositionTarget *target = NULL; IDCompositionVisual *visual = NULL;
    HWND window = NULL; HMODULE d3d12 = NULL, dxgi = NULL, dcomp = NULL;
    HINSTANCE instance = GetModuleHandleA(NULL); WNDCLASSA wc = {0}; RECT rectangle = {0, 0, 640, 480};
    DXGI_SWAP_CHAIN_DESC1 desc = {0}; D3D12_COMMAND_QUEUE_DESC qd = {0}; D3D12_DESCRIPTOR_HEAP_DESC hd = {0};
    BOOL com_initialized = FALSE, class_registered = FALSE; HRESULT hr;
    HANDLE timer = CreateThread(NULL, 0, watchdog, NULL, 0, NULL);
    if (!timer) { check("watchdog", win_error()); goto cleanup; } CloseHandle(timer);
    hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED); if (!check("CoInitializeEx", hr)) goto cleanup; com_initialized = TRUE;
    wc.lpfnWndProc = window_proc; wc.hInstance = instance; wc.lpszClassName = "D3D12CompositionProbe"; wc.hCursor = LoadCursorA(NULL, (LPCSTR)IDC_ARROW);
    if (!RegisterClassA(&wc)) { check("RegisterClass", win_error()); goto cleanup; } class_registered = TRUE;
    if (!AdjustWindowRect(&rectangle, WS_OVERLAPPEDWINDOW, FALSE)) { check("AdjustWindowRect", win_error()); goto cleanup; }
    window = CreateWindowExA(WS_EX_NOREDIRECTIONBITMAP, wc.lpszClassName, "Composition: magenta then cyan", WS_OVERLAPPEDWINDOW,
                            CW_USEDEFAULT, CW_USEDEFAULT, rectangle.right - rectangle.left, rectangle.bottom - rectangle.top,
                            NULL, NULL, instance, NULL);
    if (!window) { check("CreateWindow", win_error()); goto cleanup; } ShowWindow(window, SW_SHOW); UpdateWindow(window);
    d3d12 = LoadLibraryA("d3d12.dll"); dxgi = LoadLibraryA("dxgi.dll"); dcomp = LoadLibraryA("dcomp.dll");
    create_device_fn create_device = d3d12 ? (create_device_fn)(uintptr_t)GetProcAddress(d3d12, "D3D12CreateDevice") : NULL;
    create_factory_fn create_factory = dxgi ? (create_factory_fn)(uintptr_t)GetProcAddress(dxgi, "CreateDXGIFactory1") : NULL;
    create_composition_fn create_composition = dcomp ? (create_composition_fn)(uintptr_t)GetProcAddress(dcomp, "DCompositionCreateDevice") : NULL;
    if (!create_device || !create_factory || !create_composition) { check("resolve-standard-exports", E_NOINTERFACE); goto cleanup; }
    OBJECT("D3D12CreateDevice", create_device(NULL, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device, (void **)&p.device), p.device);
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    OBJECT("CreateCommandQueue", ID3D12Device_CreateCommandQueue(p.device, &qd, &IID_ID3D12CommandQueue, (void **)&p.queue), p.queue);
    OBJECT("CreateDXGIFactory1", create_factory(&IID_IDXGIFactory2, (void **)&factory), factory);
    desc.Width = 640; desc.Height = 480; desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM; desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; desc.BufferCount = 2; desc.Scaling = DXGI_SCALING_STRETCH;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL; desc.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
    OBJECT("CreateSwapChainForComposition", IDXGIFactory2_CreateSwapChainForComposition(factory, (IUnknown *)p.queue, &desc, NULL, &swap1), swap1);
    OBJECT("SwapChain3-QI", IDXGISwapChain1_QueryInterface(swap1, &IID_IDXGISwapChain3, (void **)&p.swap), p.swap);
    OBJECT("DCompositionCreateDevice", create_composition(NULL, &IID_IDCompositionDevice, (void **)&composition), composition);
    OBJECT("CreateTargetForHwnd", IDCompositionDevice_CreateTargetForHwnd(composition, window, TRUE, &target), target);
    OBJECT("CreateVisual", IDCompositionDevice_CreateVisual(composition, &visual), visual);
    if (!check("SetContent", IDCompositionVisual_SetContent(visual, (IUnknown *)swap1))) goto cleanup;
    if (!check("SetRoot", IDCompositionTarget_SetRoot(target, visual))) goto cleanup;
    if (!check("Commit", IDCompositionDevice_Commit(composition))) goto cleanup;
    RELEASE(IDXGISwapChain1, swap1);
    OBJECT("CreateAllocator", ID3D12Device_CreateCommandAllocator(p.device, D3D12_COMMAND_LIST_TYPE_DIRECT, &IID_ID3D12CommandAllocator, (void **)&p.allocator), p.allocator);
    OBJECT("CreateList", ID3D12Device_CreateCommandList(p.device, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, p.allocator, NULL, &IID_ID3D12GraphicsCommandList, (void **)&p.list), p.list);
    if (!check("InitialListClose", ID3D12GraphicsCommandList_Close(p.list))) goto cleanup;
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV; hd.NumDescriptors = 2;
    OBJECT("CreateRTVHeap", ID3D12Device_CreateDescriptorHeap(p.device, &hd, &IID_ID3D12DescriptorHeap, (void **)&p.heap), p.heap);
    p.stride = ID3D12Device_GetDescriptorHandleIncrementSize(p.device, D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    if (!p.stride) { check("RTV-stride", E_UNEXPECTED); goto cleanup; }
    OBJECT("CreateFence", ID3D12Device_CreateFence(p.device, 0, D3D12_FENCE_FLAG_NONE, &IID_ID3D12Fence, (void **)&p.fence), p.fence);
    p.event = CreateEventA(NULL, FALSE, FALSE, NULL); if (!p.event) { check("CreateEvent", win_error()); goto cleanup; }
    if (!acquire_buffers(&p, 640, 480)) goto cleanup;
    for (unsigned frame = 0; frame < 24 && running; ++frame) {
        pump(); if (!running) break;
        if (frame == 12) {
            if (!wait_gpu(&p)) goto cleanup;
            if (!check("ResizeListReset", ID3D12GraphicsCommandList_Reset(p.list, p.allocator, NULL))) goto cleanup;
            if (!check("ResizeListClose", ID3D12GraphicsCommandList_Close(p.list))) goto cleanup;
            release_buffers(&p);
            if (!SetWindowPos(window, NULL, 0, 0, 400, 300, SWP_NOMOVE | SWP_NOZORDER)) { check("ResizeWindow", win_error()); goto cleanup; }
            if (!check("ResizeBuffers", IDXGISwapChain3_ResizeBuffers(p.swap, 2, 384, 256, desc.Format, 0))) goto cleanup;
            if (!acquire_buffers(&p, 384, 256)) goto cleanup;
        }
        if (!render(&p, frame >= 12)) goto cleanup;
        Sleep(50);
    }
    if (InterlockedCompareExchange(&frame_count, 0, 0) != 24) { check("EarlyClose-incomplete", HRESULT_FROM_WIN32(ERROR_CANCELLED)); goto cleanup; }
    SendMessageA(window, WM_CLOSE, 0, 0); pump();
    if (running) { check("WM_CLOSE-not-delivered", E_UNEXPECTED); goto cleanup; }
cleanup:
    /* Preserve the first failure; retire submitted GPU work before destroying
     * buffers if Present or a wait failed after ExecuteCommandLists. */
    if (p.submitted && p.queue && p.fence && p.event) wait_gpu(&p);
    if (target) check("DetachRoot", IDCompositionTarget_SetRoot(target, NULL));
    if (visual) check("DetachContent", IDCompositionVisual_SetContent(visual, NULL));
    if (composition) check("DetachCommit", IDCompositionDevice_Commit(composition));
    release_buffers(&p);
    RELEASE(ID3D12GraphicsCommandList, p.list); RELEASE(ID3D12CommandAllocator, p.allocator); RELEASE(ID3D12DescriptorHeap, p.heap);
    RELEASE(ID3D12Fence, p.fence); RELEASE(IDCompositionTarget, target); RELEASE(IDCompositionVisual, visual); RELEASE(IDCompositionDevice, composition);
    RELEASE(IDXGISwapChain1, swap1); RELEASE(IDXGISwapChain3, p.swap); RELEASE(IDXGIFactory2, factory); RELEASE(ID3D12CommandQueue, p.queue); RELEASE(ID3D12Device, p.device);
    if (p.event) CloseHandle(p.event); if (window && !DestroyWindow(window)) check("DestroyWindow", win_error());
    if (class_registered && !UnregisterClassA(wc.lpszClassName, instance)) check("UnregisterClass", win_error());
    if (com_initialized) CoUninitialize(); if (dcomp) FreeLibrary(dcomp); if (dxgi) FreeLibrary(dxgi); if (d3d12) FreeLibrary(d3d12);
    InterlockedExchange(&done, 1);
    note(failed ? "FAIL-exit-1" : "API_PASS-visible-NOT_VERIFIED-exit-0", failed ? E_FAIL : S_OK);
    return failed ? 1 : 0;
}
