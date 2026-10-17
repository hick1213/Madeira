/* Generic PE32 Win32 geometry diagnostic. It changes no display settings and
 * creates only its own hidden, ordinary window, then destroys it. No CRT;
 * imports are limited to kernel32.dll and user32.dll. Build on APFS with:
 * i686-w64-mingw32-clang -O2 -Wall -Wextra -Werror -nostdlib
 *   -Wl,--entry=_start window-geometry-probe.c -lkernel32 -luser32 -o probe.exe
 */
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <stdint.h>
#include <limits.h>

static volatile LONG finished;
static BOOL failed;

struct line {
    char text[384];
    unsigned length;
};

static void append(struct line *line, const char *text)
{
    while (*text && line->length < sizeof(line->text) - 1)
        line->text[line->length++] = *text++;
}

static void number(struct line *line, LONG value)
{
    char digits[10];
    unsigned count = 0;
    ULONG magnitude = (ULONG)value;
    if (value < 0) {
        append(line, "-");
        magnitude = 0u - magnitude;
    }
    do {
        digits[count++] = (char)('0' + magnitude % 10u);
        magnitude /= 10u;
    } while (magnitude);
    while (count && line->length < sizeof(line->text) - 1)
        line->text[line->length++] = digits[--count];
}

static struct line begin(const char *stage)
{
    struct line line;
    line.length = 0;
    append(&line, "GEOMETRY stage=");
    append(&line, stage);
    return line;
}

static void output(struct line *line)
{
    DWORD written;
    unsigned offset = 0;
    HANDLE handle = GetStdHandle(STD_ERROR_HANDLE);
    append(line, "\n");
    line->text[line->length] = 0;
    /* OutputDebugString also exposes results if this GUI launch has no stderr. */
    OutputDebugStringA(line->text);
    if (handle && handle != INVALID_HANDLE_VALUE) {
        while (offset < line->length) {
            if (!WriteFile(handle, line->text + offset, line->length - offset,
                           &written, NULL) || !written)
                break;
            offset += written;
        }
    }
}

static void status(const char *stage, BOOL success, DWORD error)
{
    struct line line = begin(stage);
    append(&line, " ok=");
    number(&line, success != FALSE);
    append(&line, " error=");
    /* Error values are unsigned; use fixed-width hexadecimal without a CRT. */
    append(&line, "0x");
    for (unsigned shift = 32; shift; shift -= 4) {
        static const char hex[] = "0123456789abcdef";
        line.text[line.length++] = hex[(error >> (shift - 4)) & 15u];
    }
    output(&line);
    if (!success) failed = TRUE;
}

static void value(const char *stage, LONG result)
{
    struct line line = begin(stage);
    append(&line, " value=");
    number(&line, result);
    output(&line);
}

static void rectangle(const char *stage, const RECT *rect)
{
    struct line line = begin(stage);
    append(&line, " left="); number(&line, rect->left);
    append(&line, " top="); number(&line, rect->top);
    append(&line, " right="); number(&line, rect->right);
    append(&line, " bottom="); number(&line, rect->bottom);
    output(&line);
}

static BOOL equal_rect(const volatile RECT *left, const volatile RECT *right)
{
    return left->left == right->left && left->top == right->top &&
           left->right == right->right && left->bottom == right->bottom;
}

static BOOL rectangle_written(const RECT *rect)
{
    return rect->left != 1337 || rect->top != 1337 ||
           rect->right != 1337 || rect->bottom != 1337;
}

static BOOL positive_rectangle(const RECT *rect)
{
    return (int64_t)rect->right - rect->left > 0 &&
           (int64_t)rect->bottom - rect->top > 0;
}

static BOOL reference_offset(const RECT *source, int64_t dx, int64_t dy,
                             volatile RECT *destination)
{
    int64_t left = (int64_t)source->left + dx;
    int64_t top = (int64_t)source->top + dy;
    int64_t right = (int64_t)source->right + dx;
    int64_t bottom = (int64_t)source->bottom + dy;
    if (left < LONG_MIN || left > LONG_MAX || top < LONG_MIN || top > LONG_MAX ||
        right < LONG_MIN || right > LONG_MAX || bottom < LONG_MIN || bottom > LONG_MAX ||
        dx < LONG_MIN || dx > LONG_MAX || dy < LONG_MIN || dy > LONG_MAX)
        return FALSE;
    /* Volatile fields keep this reference independent of vectorized RECT APIs. */
    destination->left = (LONG)left;
    destination->top = (LONG)top;
    destination->right = (LONG)right;
    destination->bottom = (LONG)bottom;
    return TRUE;
}

static BOOL checked_offset(const char *stage, RECT *rect, int64_t dx, int64_t dy)
{
    RECT expected;
    BOOL success;
    if (!reference_offset(rect, dx, dy, &expected)) {
        status("rectangle-offset-representable", FALSE, 0);
        return FALSE;
    }
    success = OffsetRect(rect, (int)dx, (int)dy);
    status(stage, success, 0);
    rectangle(stage, rect);
    status("offset-matches-scalar", equal_rect(rect, &expected), 0);
    return success && equal_rect(rect, &expected);
}

static BOOL center_rectangles(const RECT *parent, const RECT *child,
                              LONG *center_x, LONG *center_y)
{
    RECT working_parent = {1337, 1337, 1337, 1337};
    RECT working_child = {1337, 1337, 1337, 1337};
    BOOL success;
    int64_t x, y;
    rectangle("center-parent-input", parent);
    rectangle("center-child-input", child);
    success = CopyRect(&working_parent, parent);
    status("CopyRect-parent", success, 0);
    rectangle("CopyRect-parent", &working_parent);
    status("copy-parent-matches-scalar", equal_rect(&working_parent, parent), 0);
    if (!success || !equal_rect(&working_parent, parent)) return FALSE;
    success = CopyRect(&working_child, child);
    status("CopyRect-child", success, 0);
    rectangle("CopyRect-child", &working_child);
    status("copy-child-matches-scalar", equal_rect(&working_child, child), 0);
    if (!success || !equal_rect(&working_child, child)) return FALSE;
    if (!checked_offset("OffsetRect-child-origin", &working_child,
                        -(int64_t)child->left, -(int64_t)child->top)) return FALSE;
    if (!checked_offset("OffsetRect-parent-origin", &working_parent,
                        -(int64_t)parent->left, -(int64_t)parent->top)) return FALSE;
    if (!checked_offset("OffsetRect-parent-minus-child-size", &working_parent,
                        -(int64_t)working_child.right,
                        -(int64_t)working_child.bottom)) return FALSE;
    x = (int64_t)parent->left + working_parent.right / 2;
    y = (int64_t)parent->top + working_parent.bottom / 2;
    if (x < LONG_MIN || x > LONG_MAX || y < LONG_MIN || y > LONG_MAX) {
        status("rectangle-center-representable", FALSE, 0);
        return FALSE;
    }
    *center_x = (LONG)x;
    *center_y = (LONG)y;
    value("rectangle-center-x", *center_x);
    value("rectangle-center-y", *center_y);
    return TRUE;
}

static BOOL read_window_rect(const char *stage, HWND window, BOOL client,
                             RECT *rect)
{
    BOOL success;
    DWORD error;
    rect->left = rect->top = rect->right = rect->bottom = 1337;
    success = client ? GetClientRect(window, rect) : GetWindowRect(window, rect);
    error = success ? 0 : GetLastError();
    status(stage, success, error);
    if (success) {
        rectangle(stage, rect);
        if (!rectangle_written(rect)) {
            status("rectangle-output-written", FALSE, 0);
            return FALSE;
        }
    }
    return success;
}

static BOOL read_monitor(const char *stage, HMONITOR monitor)
{
    MONITORINFO info = {0};
    BOOL success, monitor_written, work_written, monitor_positive, work_positive;
    DWORD error;
    info.cbSize = sizeof(info);
    info.rcMonitor.left = info.rcMonitor.top = 1337;
    info.rcMonitor.right = info.rcMonitor.bottom = 1337;
    info.rcWork.left = info.rcWork.top = 1337;
    info.rcWork.right = info.rcWork.bottom = 1337;
    if (!monitor) {
        status(stage, FALSE, 0);
        return FALSE;
    }
    success = GetMonitorInfoW(monitor, &info);
    error = success ? 0 : GetLastError();
    status(stage, success, error);
    if (success) {
        rectangle(stage, &info.rcMonitor);
        rectangle("monitor-work-area", &info.rcWork);
        value("monitor-is-primary", (info.dwFlags & MONITORINFOF_PRIMARY) != 0);
        monitor_written = rectangle_written(&info.rcMonitor);
        work_written = rectangle_written(&info.rcWork);
        monitor_positive = positive_rectangle(&info.rcMonitor);
        work_positive = positive_rectangle(&info.rcWork);
        status("monitor-rectangle-written", monitor_written, 0);
        status("monitor-work-rectangle-written", work_written, 0);
        status("monitor-rectangle-positive", monitor_positive, 0);
        status("monitor-work-rectangle-positive", work_positive, 0);
        success = monitor_written && work_written && monitor_positive && work_positive;
    }
    return success;
}

static LRESULT CALLBACK window_proc(HWND window, UINT message,
                                   WPARAM wparam, LPARAM lparam)
{
    return DefWindowProcW(window, message, wparam, lparam);
}

static DWORD WINAPI watchdog(void *unused)
{
    (void)unused;
    Sleep(15000);
    if (!InterlockedCompareExchange(&finished, 0, 0)) {
        struct line line = begin("TIMEOUT-exit-90");
        output(&line);
        ExitProcess(90);
    }
    return 0;
}

static void inspect_centered_window(const RECT *bounds, const RECT *desktop_rect)
{
    static const WCHAR class_name[] = L"MadeiraGeometryProbe";
    WNDCLASSW wc = {0};
    HINSTANCE instance = GetModuleHandleW(NULL);
    RECT rect = {0};
    HWND window;
    BOOL success;
    DWORD error;
    const int width = 320, height = 240;
    int64_t span_x = (int64_t)bounds->right - bounds->left;
    int64_t span_y = (int64_t)bounds->bottom - bounds->top;
    int64_t x, y;

    /* Check before conversion, so the probe cannot itself wrap LONG geometry. */
    if (span_x < width || span_y < height) {
        status("center-bounds-valid", FALSE, 0);
        return;
    }
    x = (int64_t)bounds->left + (span_x - width) / 2;
    y = (int64_t)bounds->top + (span_y - height) / 2;
    if (x < LONG_MIN || x > LONG_MAX || y < LONG_MIN || y > LONG_MAX) {
        status("center-coordinates-valid", FALSE, 0);
        return;
    }
    value("requested-window-x", (LONG)x);
    value("requested-window-y", (LONG)y);
    value("requested-window-width", width);
    value("requested-window-height", height);
    wc.lpfnWndProc = window_proc;
    wc.hInstance = instance;
    wc.lpszClassName = class_name;
    success = RegisterClassW(&wc) != 0;
    error = success ? 0 : GetLastError();
    status("RegisterClassW", success, error);
    if (!success) return;

    window = CreateWindowExW(0, class_name, L"Geometry probe", WS_OVERLAPPEDWINDOW,
                             (int)x, (int)y, width, height,
                             NULL, NULL, instance, NULL);
    error = window ? 0 : GetLastError();
    status("CreateWindowExW", window != NULL, error);
    if (window) {
        if (read_window_rect("test-window-rect", window, FALSE, &rect) && desktop_rect) {
            LONG center_x, center_y;
            int64_t old_width = (int64_t)rect.right - rect.left;
            int64_t old_height = (int64_t)rect.bottom - rect.top;
            if (center_rectangles(desktop_rect, &rect, &center_x, &center_y)) {
                success = SetWindowPos(window, NULL, center_x, center_y, 0, 0,
                                       SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
                error = success ? 0 : GetLastError();
                status("SetWindowPos-center", success, error);
                if (read_window_rect("centered-test-window-rect", window, FALSE, &rect)) {
                    status("center-position-matches-requested",
                           rect.left == center_x && rect.top == center_y, 0);
                    status("center-size-preserved",
                           (int64_t)rect.right - rect.left == old_width &&
                           (int64_t)rect.bottom - rect.top == old_height, 0);
                }
            }
        }
        read_window_rect("test-client-rect", window, TRUE, &rect);
        read_monitor("test-window-monitor", MonitorFromWindow(window, MONITOR_DEFAULTTONULL));
        success = DestroyWindow(window);
        error = success ? 0 : GetLastError();
        status("DestroyWindow", success, error);
    }
    success = UnregisterClassW(class_name, instance);
    error = success ? 0 : GetLastError();
    status("UnregisterClassW", success, error);
}

void start(void)
{
    HWND desktop;
    RECT desktop_rect = {0}, client_rect = {0}, work_area = {0};
    POINT origin = {0, 0};
    BOOL have_desktop_rect, have_work_area;
    HANDLE timer;
    int width, height, virtual_width, virtual_height;
    const RECT known_parent = {-20, 40, 620, 520};
    const RECT known_child = {30, 50, 350, 290};
    LONG known_x, known_y;
    struct line line = begin("begin-PE32");
    output(&line);
    timer = CreateThread(NULL, 0, watchdog, NULL, 0, NULL);
    status("CreateThread-watchdog", timer != NULL, timer ? 0 : GetLastError());
    if (!timer) ExitProcess(1);
    CloseHandle(timer);

    if (center_rectangles(&known_parent, &known_child, &known_x, &known_y))
        status("known-rectangle-center-140-160", known_x == 140 && known_y == 160, 0);

    desktop = GetDesktopWindow();
    status("GetDesktopWindow", desktop != NULL, 0);
    have_desktop_rect = FALSE;
    if (desktop) {
        have_desktop_rect = read_window_rect("desktop-window-rect", desktop, FALSE, &desktop_rect);
        read_window_rect("desktop-client-rect", desktop, TRUE, &client_rect);
        read_monitor("desktop-monitor", MonitorFromWindow(desktop, MONITOR_DEFAULTTOPRIMARY));
    }
    width = GetSystemMetrics(SM_CXSCREEN);
    height = GetSystemMetrics(SM_CYSCREEN);
    virtual_width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    virtual_height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    value("SM_CXSCREEN", width);
    value("SM_CYSCREEN", height);
    value("SM_XVIRTUALSCREEN", GetSystemMetrics(SM_XVIRTUALSCREEN));
    value("SM_YVIRTUALSCREEN", GetSystemMetrics(SM_YVIRTUALSCREEN));
    value("SM_CXVIRTUALSCREEN", virtual_width);
    value("SM_CYVIRTUALSCREEN", virtual_height);
    value("SM_CMONITORS", GetSystemMetrics(SM_CMONITORS));
    value("IsProcessDPIAware", IsProcessDPIAware());
    status("screen-sizes-positive", width > 0 && height > 0 &&
           virtual_width > 0 && virtual_height > 0, 0);
    have_work_area = SystemParametersInfoW(SPI_GETWORKAREA, 0, &work_area, 0);
    status("SPI_GETWORKAREA", have_work_area, have_work_area ? 0 : GetLastError());
    if (have_work_area) rectangle("system-work-area", &work_area);
    read_monitor("origin-monitor", MonitorFromPoint(origin, MONITOR_DEFAULTTONULL));
    if (have_work_area)
        inspect_centered_window(&work_area, have_desktop_rect ? &desktop_rect : NULL);
    else if (have_desktop_rect)
        inspect_centered_window(&desktop_rect, &desktop_rect);

    line = begin(failed ? "FAIL-exit-1" : "READ_PASS-exit-0");
    output(&line);
    InterlockedExchange(&finished, 1);
    ExitProcess(failed ? 1 : 0);
}
