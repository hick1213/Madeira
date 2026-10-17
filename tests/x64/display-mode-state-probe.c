/* Isolated, single-display mode-state diagnostic. This executable changes the
 * virtual display while running and restores its original current/registry
 * modes on every ordinary exit path. Do not run alongside a game.
 * Contract: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw
 */
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <stdio.h>
#include <string.h>

static unsigned int failures, display_messages;
static WPARAM last_bpp;
static LPARAM last_dimensions;

static void check(BOOL ok, const char *contract)
{
    char line[256];
    snprintf(line, sizeof(line), "DISPLAY-STATE %s %s\n", ok ? "PASS" : "FAIL", contract);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
    if (!ok) failures++;
}

static LONG change(const WCHAR *device, DEVMODEW *mode, DWORD flags, const char *label)
{
    char line[192];
    LONG result = ChangeDisplaySettingsExW(device, mode, NULL, flags, NULL);
    snprintf(line, sizeof(line), "DISPLAY-STATE %s result=%ld flags=%#lx mode=%lux%lu\n",
             label, result, flags, mode ? mode->dmPelsWidth : 0, mode ? mode->dmPelsHeight : 0);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
    return result;
}

static BOOL read_mode(const WCHAR *device, DWORD index, DEVMODEW *mode)
{
    memset(mode, 0, sizeof(*mode));
    mode->dmSize = sizeof(*mode);
    return EnumDisplaySettingsExW(device, index, mode, 0);
}

static BOOL same_mode(const DEVMODEW *a, const DEVMODEW *b)
{
    return a->dmPelsWidth == b->dmPelsWidth && a->dmPelsHeight == b->dmPelsHeight &&
           a->dmBitsPerPel == b->dmBitsPerPel && a->dmDisplayFrequency == b->dmDisplayFrequency &&
           a->dmDisplayOrientation == b->dmDisplayOrientation && a->dmDisplayFlags == b->dmDisplayFlags &&
           a->dmPosition.x == b->dmPosition.x && a->dmPosition.y == b->dmPosition.y;
}

static void expect_modes(const WCHAR *device, const DEVMODEW *current, const DEVMODEW *registry,
                         const char *current_contract, const char *registry_contract)
{
    DEVMODEW actual;
    check(read_mode(device, ENUM_CURRENT_SETTINGS, &actual) && same_mode(&actual, current), current_contract);
    check(read_mode(device, ENUM_REGISTRY_SETTINGS, &actual) && same_mode(&actual, registry), registry_contract);
}

static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    if (message == WM_DISPLAYCHANGE) {
        display_messages++;
        last_bpp = wparam;
        last_dimensions = lparam;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

static void pump_messages(DWORD milliseconds)
{
    DWORD start = GetTickCount();
    MSG message;
    do {
        while (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if (GetTickCount() - start >= milliseconds) break;
        MsgWaitForMultipleObjects(0, NULL, FALSE, 10, QS_ALLINPUT);
    } while (TRUE);
}

static void expect_geometry(const WCHAR *device, const DEVMODEW *mode)
{
    POINT point = {mode->dmPosition.x, mode->dmPosition.y};
    HMONITOR monitor = MonitorFromPoint(point, MONITOR_DEFAULTTOPRIMARY);
    MONITORINFOEXW info = {0};
    info.cbSize = sizeof(info);
    check(GetMonitorInfoW(monitor, (MONITORINFO *)&info) && !lstrcmpiW(device, info.szDevice) &&
          info.rcMonitor.right - info.rcMonitor.left == (LONG)mode->dmPelsWidth &&
          info.rcMonitor.bottom - info.rcMonitor.top == (LONG)mode->dmPelsHeight,
          "GetMonitorInfo follows applied current mode");
    check(GetSystemMetrics(SM_CXSCREEN) == (int)mode->dmPelsWidth &&
          GetSystemMetrics(SM_CYSCREEN) == (int)mode->dmPelsHeight,
          "GetSystemMetrics follows applied current mode");
}

int main(void)
{
    static const WCHAR class_name[] = L"MadeiraDisplayStateProbe";
    static const WCHAR unknown_device[] = L"\\\\.\\DISPLAY999999";
    DISPLAY_DEVICEW display = {0};
    WCHAR device[CCHDEVICENAME] = {0};
    DEVMODEW original_current, original_registry, candidate, unsupported;
    WNDCLASSW cls = {0};
    HINSTANCE instance = GetModuleHandleW(NULL);
    HWND window = NULL;
    DWORD index;
    unsigned int attached = 0, messages_before;
    BOOL have_candidate = FALSE, may_have_changed = FALSE;

    SetProcessDPIAware();
    for (index = 0; ; index++) {
        memset(&display, 0, sizeof(display));
        display.cb = sizeof(display);
        if (!EnumDisplayDevicesW(NULL, index, &display, 0)) break;
        if (!(display.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)) continue;
        attached++;
        if (display.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)
            lstrcpynW(device, display.DeviceName, CCHDEVICENAME);
    }
    /* The documented NULL flush applies all displays. Refuse multi-display
     * machines instead of accidentally applying someone else's pending mode. */
    check(attached == 1 && device[0], "one attached primary display required for isolated probe");
    if (attached != 1 || !device[0]) goto done;
    check(read_mode(device, ENUM_CURRENT_SETTINGS, &original_current), "snapshot current mode");
    check(read_mode(device, ENUM_REGISTRY_SETTINGS, &original_registry), "snapshot registry mode");
    if (failures) goto done;
    original_current.dmDriverExtra = original_registry.dmDriverExtra = 0;
    /* Also restore after a defective CDS_TEST implementation changes state. */
    may_have_changed = TRUE;

    for (index = 0; index < 4096 && read_mode(device, index, &candidate); index++) {
        if (!candidate.dmPelsWidth || !candidate.dmPelsHeight || candidate.dmBitsPerPel != 32 ||
            (candidate.dmPelsWidth == original_current.dmPelsWidth &&
             candidate.dmPelsHeight == original_current.dmPelsHeight)) continue;
        candidate.dmDriverExtra = 0;
        if (change(device, &candidate, CDS_TEST, "select enumerated mode") == DISP_CHANGE_SUCCESSFUL) {
            have_candidate = TRUE;
            break;
        }
    }
    check(have_candidate, "different enumerated mode supports CDS_TEST");
    if (!have_candidate) goto done;
    expect_modes(device, &original_current, &original_registry,
                 "CDS_TEST preserves current", "CDS_TEST preserves registry");

    cls.lpfnWndProc = window_proc;
    cls.hInstance = instance;
    cls.lpszClassName = class_name;
    check(RegisterClassW(&cls) != 0, "register broadcast observer");
    if (failures) goto done;
    window = CreateWindowExW(0, class_name, L"Display state probe", WS_OVERLAPPED,
                             0, 0, 32, 32, NULL, NULL, instance, NULL);
    check(window != NULL, "create top-level broadcast observer");
    if (!window) goto done;
    pump_messages(100);
    messages_before = display_messages;

    check(change(unknown_device, &candidate, CDS_TEST, "unknown device") < 0, "unknown device rejected");
    unsupported = candidate;
    unsupported.dmPelsWidth = unsupported.dmPelsHeight = 0xffffffffu;
    unsupported.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT;
    check(change(device, &unsupported, CDS_TEST, "unsupported mode") < 0, "unsupported mode rejected");
    expect_modes(device, &original_current, &original_registry,
                 "rejected requests preserve current", "rejected requests preserve registry");

    /* Set before calling: even a driver that returns an error may have saved
     * registry state. Every following failure still reaches the restore block. */
    may_have_changed = TRUE;
    check(change(device, &candidate, CDS_UPDATEREGISTRY | CDS_NORESET, "stage registry") ==
          DISP_CHANGE_SUCCESSFUL, "stage succeeds");
    expect_modes(device, &original_current, &candidate,
                 "NORESET preserves current", "NORESET updates registry");
    pump_messages(100);
    check(display_messages == messages_before, "NORESET does not send display-change event");
    check(change(device, &original_current, CDS_TEST, "test while staged") == DISP_CHANGE_SUCCESSFUL,
          "test original mode while candidate staged");
    expect_modes(device, &original_current, &candidate,
                 "staged CDS_TEST preserves current", "staged CDS_TEST preserves registry");

    check(change(NULL, NULL, 0, "apply saved modes") == DISP_CHANGE_SUCCESSFUL, "NULL flush succeeds");
    expect_modes(device, &candidate, &candidate,
                 "NULL flush applies saved mode", "NULL flush retains registry");
    expect_geometry(device, &candidate);
    pump_messages(1000);
    check(display_messages > messages_before && last_bpp == candidate.dmBitsPerPel &&
          LOWORD(last_dimensions) == candidate.dmPelsWidth && HIWORD(last_dimensions) == candidate.dmPelsHeight,
          "WM_DISPLAYCHANGE reports applied dimensions and bpp");

    check(change(device, &original_current, CDS_FULLSCREEN, "temporary fullscreen") == DISP_CHANGE_SUCCESSFUL,
          "temporary fullscreen succeeds");
    expect_modes(device, &original_current, &candidate,
                 "temporary fullscreen changes current", "temporary fullscreen preserves registry");
    check(change(device, NULL, 0, "restore saved mode") == DISP_CHANGE_SUCCESSFUL,
          "NULL device mode restores saved registry");
    expect_modes(device, &candidate, &candidate,
                 "NULL mode restores registry rather than launch default", "saved registry retained");

done:
    if (may_have_changed) {
        check(change(device, &original_registry, CDS_UPDATEREGISTRY | CDS_NORESET, "cleanup registry") ==
              DISP_CHANGE_SUCCESSFUL, "cleanup registry succeeds");
        check(change(device, &original_current, 0, "cleanup current") == DISP_CHANGE_SUCCESSFUL,
              "cleanup current succeeds");
        expect_modes(device, &original_current, &original_registry,
                     "cleanup restores original current", "cleanup restores original registry");
        expect_geometry(device, &original_current);
    }
    if (window) DestroyWindow(window);
    UnregisterClassW(class_name, instance);
    check(!failures, "terminal display-mode state consistency");
    return failures ? 1 : 0;
}
