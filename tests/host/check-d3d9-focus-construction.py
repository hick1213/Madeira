#!/usr/bin/env python3
"""Execute the actual D3D9 constructor window fragment and focus filter.

WinAPI and device callbacks are controlled host boundaries. This exercises
production window-message/filter ordering, not the complete GPU constructor
or Windows ABI. Removing only the constructor guard must fail the same checks.
"""
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "dxmt/src/d3d9/d3d9_device.cpp"
GUARD = "  FilteredWindowMessages constructionMessages(m_focusMessagesFiltered);\n"

BOUNDARY = r'''
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <cstdint>
using HWND = void *;
using HANDLE = void *;
using UINT = unsigned;
using WPARAM = uintptr_t;
using LPARAM = intptr_t;
using LRESULT = intptr_t;
using LONG_PTR = intptr_t;
using BOOL = int;
#define CALLBACK
constexpr BOOL FALSE = 0;
constexpr UINT WM_SIZE = 5, WM_ACTIVATEAPP = 28, WM_STYLECHANGED = 125;
constexpr UINT WM_DISPLAYCHANGE = 126, WM_NCDESTROY = 130, WM_SYSCOMMAND = 274;
constexpr WPARAM SC_RESTORE = 0xf120;
constexpr int GWLP_WNDPROC = -4;
using WNDPROC = LRESULT (*)(HWND, UINT, WPARAM, LPARAM);
struct Params { HWND hDeviceWindow; bool Windowed; UINT BackBufferWidth, BackBufferHeight; };
class MTLD3D9Device;
static MTLD3D9Device *current;
static bool unicode;
static unsigned application[300], defaults[300];
static LRESULT original(HWND, UINT message, WPARAM, LPARAM) { ++application[message]; return 42; }
static HANDLE GetPropW(HWND, const wchar_t *name) {
  return std::wcscmp(name, L"DXMTD3D9FocusOrigProc") == 0
      ? reinterpret_cast<HANDLE>(original) : static_cast<HANDLE>(current);
}
static BOOL IsWindowUnicode(HWND) { return unicode; }
static LONG_PTR SetWindowLongPtrW(HWND, int, LONG_PTR) { return 0; }
static void RemovePropW(HWND, const wchar_t *) {}
static LRESULT DefWindowProcW(HWND, UINT m, WPARAM, LPARAM) { ++defaults[m]; return -7; }
static LRESULT DefWindowProcA(HWND w, UINT m, WPARAM p, LPARAM l) { return DefWindowProcW(w, m, p, l); }
static LRESULT CallWindowProcW(WNDPROC f, HWND w, UINT m, WPARAM p, LPARAM l) { return f(w, m, p, l); }
static LRESULT CallWindowProcA(WNDPROC f, HWND w, UINT m, WPARAM p, LPARAM l) { return f(w, m, p, l); }
static LRESULT focusWindowProc(HWND, UINT, WPARAM, LPARAM);
struct MTLD3D9SwapChain {
  MTLD3D9SwapChain(MTLD3D9Device *, bool, const Params &, HWND, bool) {}
  void AddRefPrivate() {}
};
class MTLD3D9Device {
public:
  bool m_focusMessagesFiltered = false;
  Params m_presentParams{};
  MTLD3D9SwapChain *m_implicitSwapChain = nullptr;
  unsigned activations = 0;
  ~MTLD3D9Device() { delete m_implicitSwapChain; }
  bool focusMessagesFiltered() const { return m_focusMessagesFiltered; }
  void onFocusActivation(bool) { ++activations; }
  void hookFocusWindowProc(HWND w) {
    current = this;
    focusWindowProc(w, WM_ACTIVATEAPP, 1, 0);
    focusWindowProc(w, WM_ACTIVATEAPP, 0, 0);
  }
  void enterFullscreenWindow(HWND w, UINT, UINT) {
    focusWindowProc(w, WM_SIZE, 0, 0);
    focusWindowProc(w, WM_STYLECHANGED, 0, 0);
    focusWindowProc(w, WM_DISPLAYCHANGE, 0, 0);
  }
  void constructionFragment(const Params &, HWND, bool);
};
'''

CHECKS = r'''
#define CHECK(condition) do { if (!(condition)) { \
  std::fprintf(stderr, "FAIL: %s at %d\n", #condition, __LINE__); return 1; } } while (0)
int main() {
  Params params{reinterpret_cast<HWND>(1), false, 640, 480};
  for (bool is_unicode : {false, true}) {
    for (bool inherited_filter : {false, true}) {
      unicode = is_unicode;
      std::memset(application, 0, sizeof(application));
      std::memset(defaults, 0, sizeof(defaults));
      MTLD3D9Device device;
      device.m_focusMessagesFiltered = inherited_filter;
      device.constructionFragment(params, params.hDeviceWindow, false);
      CHECK(device.m_implicitSwapChain != nullptr);
      CHECK(device.activations == 0);
      CHECK(defaults[WM_ACTIVATEAPP] == 2 && application[WM_ACTIVATEAPP] == 0);
      CHECK(defaults[WM_SIZE] == 1 && application[WM_SIZE] == 0);
      CHECK(defaults[WM_STYLECHANGED] == 1 && application[WM_STYLECHANGED] == 0);
      CHECK(application[WM_DISPLAYCHANGE] == 1 && defaults[WM_DISPLAYCHANGE] == 0);
      CHECK(device.m_focusMessagesFiltered == inherited_filter);
      if (!inherited_filter) {
        CHECK(focusWindowProc(params.hDeviceWindow, WM_ACTIVATEAPP, 1, 0) == 42);
        CHECK(device.activations == 1 && application[WM_ACTIVATEAPP] == 1);
      }
      CHECK(focusWindowProc(params.hDeviceWindow, WM_NCDESTROY, 0, 0) == 42);
      CHECK(application[WM_NCDESTROY] == 1);
      current = nullptr;
    }
  }
  std::puts("PASS: constructor focus boundary, ANSI/Unicode, fresh/inherited filter");
}
'''


def production(source: str) -> tuple[str, str, str]:
    """Preserve whole production struct/proc and contiguous ctor window code."""
    struct_start = source.index("struct FilteredWindowMessages {")
    filtered = source[struct_start:source.index("\n};", struct_start) + 3]
    focus_start = source.index("constexpr const wchar_t *kFocusProcProp")
    focus_end = source.index("\n}\n} // namespace", focus_start) + 2
    focus = source[focus_start:focus_end]
    ctor_start = source.index(GUARD, source.index("MTLD3D9Device::MTLD3D9Device("))
    ctor_end = source.index("  // Seed the device-wide queue latency", ctor_start)
    return filtered, focus, source[ctor_start:ctor_end]


def main() -> None:
    filtered, focus, ctor = production(SOURCE.read_text())
    with tempfile.TemporaryDirectory(prefix="madeira-d3d9-focus-construction-") as directory:
        folder = Path(directory)
        for omit_guard in (True, False):
            fragment = ctor.replace(GUARD, "", 1) if omit_guard else ctor
            code = "#include <initializer_list>\n" + BOUNDARY + filtered + "\n" + focus
            code += "\nvoid MTLD3D9Device::constructionFragment(const Params &validatedParams, "
            code += "HWND focusWindow, bool isEx) {\n" + fragment + "}\n" + CHECKS
            source = folder / "contract.cpp"
            source.write_text(code)
            executable = folder / "contract"
            subprocess.run([
                os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-g",
                str(source), "-o", str(executable),
            ], check=True)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
            if omit_guard:
                if result.returncode != 1 or "FAIL: device.activations == 0" not in result.stderr:
                    raise RuntimeError("Missing constructor guard did not fail activation assertion: " +
                                       result.stdout + result.stderr)
                print("RED: removing constructor guard calls activation during construction")
            else:
                if result.returncode or not result.stdout.startswith("PASS: constructor focus boundary"):
                    raise RuntimeError(result.stdout + result.stderr)
                print(result.stdout.strip())


if __name__ == "__main__":
    main()
