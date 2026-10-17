/* Ordinary PE32 D3D9 window-message diagnostic. No CRT or executable-memory
 * allocation. Same focus/device window; the WndProc only counts and forwards.
 * Build on APFS with the repository's i686-w64-mingw32-clang:
 *   -O2 -Wall -Wextra -Werror -ffreestanding -fno-builtin -nostdlib
 *   -Wl,--entry=_start -Wl,--large-address-aware -lkernel32 -luser32 -ld3d9
 * Exit 0 means all assertions passed, 1 is an assertion failure,
 * 124 is the 45-second watchdog, and 125 is watchdog setup failure.
 */
#define COBJMACROS
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <d3d9.h>
#include <stddef.h>

static BOOL failed;
static HANDLE finished_event;
static LONG reset_depth;
static LONG phase;
static LONG maximum_message_depth;

enum { PHASE_IDLE, PHASE_FULLSCREEN, PHASE_WINDOWED, PHASE_EXTERNAL,
       PHASE_FULLSCREEN_CREATE, PHASE_COUNT };
struct counters {
    LONG size;
    LONG style;
    LONG position;
    LONG resized_position;
    LONG display;
    LONG activation;
};
static struct counters counts[PHASE_COUNT];

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

struct line { char text[384]; unsigned length; };

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
    append(&line, "D9-WINDOW-RESET stage=");
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

static BOOL expect(const char *stage, BOOL success)
{
    struct line line = begin(stage);
    append(&line, success ? " PASS" : " FAIL error=");
    if (!success) {
        hex(&line, GetLastError());
        failed = TRUE;
    }
    output(&line);
    return success;
}

static BOOL expect_hr(const char *stage, HRESULT hr)
{
    struct line line = begin(stage);
    BOOL success = hr == D3D_OK;
    append(&line, " hr="); hex(&line, (DWORD)hr);
    append(&line, success ? " PASS" : " FAIL");
    if (!success) failed = TRUE;
    output(&line);
    return success;
}

static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    LONG current = phase;
    if (current > PHASE_IDLE && current < PHASE_COUNT) {
        struct counters *c = &counts[current];
        if (reset_depth > maximum_message_depth) maximum_message_depth = reset_depth;
        if (message == WM_SIZE) c->size++;
        if (message == WM_STYLECHANGED) c->style++;
        if (message == WM_WINDOWPOSCHANGED) {
            const WINDOWPOS *position = (const WINDOWPOS *)lparam;
            c->position++;
            if (position && !(position->flags & SWP_NOSIZE)) c->resized_position++;
        }
        if (message == WM_DISPLAYCHANGE) c->display++;
        if (message == WM_ACTIVATEAPP) c->activation++;
    }
    /* WM_NCDESTROY and every other message retain normal default handling.
     * This callback never calls D3D9 or initiates a resize itself. */
    return DefWindowProcA(window, message, wparam, lparam);
}

static void report(const char *stage, LONG index, BOOL resetting)
{
    const struct counters *c = &counts[index];
    struct line line = begin(stage);
    BOOL success = resetting ? !c->size && !c->style && !c->resized_position
                             : c->size > 0 && c->resized_position > 0;
    append(&line, " WM_SIZE="); hex(&line, (DWORD)c->size);
    append(&line, " WM_STYLECHANGED="); hex(&line, (DWORD)c->style);
    append(&line, " WM_WINDOWPOSCHANGED="); hex(&line, (DWORD)c->position);
    append(&line, " resize-position="); hex(&line, (DWORD)c->resized_position);
    append(&line, " WM_DISPLAYCHANGE="); hex(&line, (DWORD)c->display);
    append(&line, " WM_ACTIVATEAPP="); hex(&line, (DWORD)c->activation);
    append(&line, " max-reset-message-depth="); hex(&line, (DWORD)maximum_message_depth);
    append(&line, success ? " PASS" : " FAIL");
    if (!success) failed = TRUE;
    output(&line);
}

static void fill_parameters(D3DPRESENT_PARAMETERS *parameters, HWND window, BOOL windowed)
{
    memset(parameters, 0, sizeof(*parameters));
    parameters->BackBufferWidth = 640;
    parameters->BackBufferHeight = 480;
    parameters->BackBufferFormat = D3DFMT_A8R8G8B8;
    parameters->BackBufferCount = 1;
    parameters->SwapEffect = D3DSWAPEFFECT_DISCARD;
    parameters->hDeviceWindow = window;
    parameters->Windowed = windowed;
    parameters->FullScreen_RefreshRateInHz = windowed ? 0 : 60;
    parameters->PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
}

static void check_reset(IDirect3DDevice9 *device, HWND window, BOOL windowed)
{
    D3DPRESENT_PARAMETERS parameters;
    HRESULT hr;
    const char *stage = windowed ? "fullscreen-to-windowed" : "first-windowed-to-fullscreen";
    LONG index = windowed ? PHASE_WINDOWED : PHASE_FULLSCREEN;
    fill_parameters(&parameters, window, windowed);
    memset(&counts[index], 0, sizeof(counts[index]));
    maximum_message_depth = 0;
    phase = index;
    reset_depth++;
    hr = IDirect3DDevice9_Reset(device, &parameters);
    reset_depth--;
    phase = PHASE_IDLE;
    expect_hr(stage, hr);
    report(stage, index, TRUE);
}

static DWORD WINAPI watchdog(void *unused)
{
    DWORD wait;
    struct line line;
    (void)unused;
    wait = WaitForSingleObject(finished_event, 45000);
    if (wait != WAIT_OBJECT_0) {
        line = begin(wait == WAIT_TIMEOUT ? "TIMEOUT" : "watchdog-wait-failed");
        append(&line, " wait="); hex(&line, wait);
        output(&line);
        ExitProcess(wait == WAIT_TIMEOUT ? 124 : 125);
    }
    return 0;
}

void start(void)
{
    HANDLE watchdog_thread;
    HINSTANCE instance = GetModuleHandleA(NULL);
    WNDCLASSA window_class = {0};
    HWND window = NULL;
    IDirect3D9 *d3d = NULL;
    IDirect3DDevice9 *device = NULL;
    D3DPRESENT_PARAMETERS parameters;
    RECT rect;
    HRESULT hr;
    BOOL registered = FALSE;
    struct line line;

    finished_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!expect("CreateWatchdogEvent", finished_event != NULL)) ExitProcess(125);
    watchdog_thread = CreateThread(NULL, 0, watchdog, NULL, 0, NULL);
    if (!expect("CreateWatchdogThread", watchdog_thread != NULL)) {
        CloseHandle(finished_event);
        ExitProcess(125);
    }
    window_class.lpfnWndProc = window_proc;
    window_class.hInstance = instance;
    window_class.lpszClassName = "MadeiraD3D9WindowResetProbe";
    registered = RegisterClassA(&window_class) != 0;
    if (!expect("RegisterWindowClass", registered)) goto cleanup;
    window = CreateWindowExA(0, window_class.lpszClassName, "D3D9 window Reset diagnostic",
                            WS_OVERLAPPEDWINDOW | WS_VISIBLE, 50, 50, 320, 240,
                            NULL, NULL, instance, NULL);
    if (!expect("CreateWindow", window != NULL)) goto cleanup;
    d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!expect("Direct3DCreate9", d3d != NULL)) goto cleanup;
    fill_parameters(&parameters, window, TRUE);
    hr = IDirect3D9_CreateDevice(d3d, 0, D3DDEVTYPE_HAL, window,
                               D3DCREATE_HARDWARE_VERTEXPROCESSING | D3DCREATE_MULTITHREADED,
                               &parameters, &device);
    if (!expect_hr("CreateWindowedDevice", hr) || !expect("DeviceNonNull", device != NULL)) goto cleanup;
    line = begin("provider");
    append(&line, GetModuleHandleA("d3d9-emulated.dll") ? " emulated=1" : " emulated=0");
    append(&line, " hFocusWindow="); hex(&line, (DWORD)(ULONG_PTR)window);
    append(&line, " hDeviceWindow="); hex(&line, (DWORD)(ULONG_PTR)window);
    output(&line);

    check_reset(device, window, FALSE);
    check_reset(device, window, TRUE);
    if (!expect("GetExternalResizeRect", GetWindowRect(window, &rect))) goto cleanup;
    phase = PHASE_EXTERNAL;
    expect("ExternalSetWindowPos", SetWindowPos(window, NULL, 0, 0,
           rect.right - rect.left + 17, rect.bottom - rect.top + 19,
           SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOMOVE));
    phase = PHASE_IDLE;
    report("external-resize-delivered", PHASE_EXTERNAL, FALSE);

    /* Exercise construction separately: no device interface exists in the
     * application's hands while its fullscreen constructor processes traffic.
     * Activation may already be satisfied, so its count is informational.
     * The deterministic host fixture covers forced activation callbacks. */
    IDirect3DDevice9_Release(device);
    device = NULL;
    fill_parameters(&parameters, window, FALSE);
    phase = PHASE_FULLSCREEN_CREATE;
    hr = IDirect3D9_CreateDevice(d3d, 0, D3DDEVTYPE_HAL, window,
                               D3DCREATE_HARDWARE_VERTEXPROCESSING | D3DCREATE_MULTITHREADED,
                               &parameters, &device);
    phase = PHASE_IDLE;
    if (!expect_hr("CreateFirstFullscreenDevice", hr) ||
        !expect("FullscreenDeviceNonNull", device != NULL)) goto cleanup;
    report("first-fullscreen-construction", PHASE_FULLSCREEN_CREATE, TRUE);
    check_reset(device, window, TRUE);

cleanup:
    phase = PHASE_IDLE;
    if (device) IDirect3DDevice9_Release(device);
    if (d3d) IDirect3D9_Release(d3d);
    if (window && !DestroyWindow(window)) failed = TRUE;
    if (registered && !UnregisterClassA(window_class.lpszClassName, instance)) failed = TRUE;
    if (!SetEvent(finished_event) || WaitForSingleObject(watchdog_thread, 5000) != WAIT_OBJECT_0)
        ExitProcess(125);
    CloseHandle(watchdog_thread);
    CloseHandle(finished_event);
    line = begin(failed ? "FAIL-exit-1" : "PASS-exit-0");
    output(&line);
    ExitProcess(failed ? 1 : 0);
}
