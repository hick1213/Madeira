#!/usr/bin/env python3
"""Run production D3D9 Surface lifetime edges under ASan/UBSan.

The host declaration omits unrelated Surface virtual methods, but the fields,
constructor, destructor, marks, public AddRef/Release, format arithmetic, and
ComObject/Com private-reference implementation come from production source.
Only Device ownership, Metal handles, and logging are boundary fixtures. This
does not validate Metal rendering or a complete device Reset on iOS.

The overlap case inserts one scheduling hook after the real ComObject public
decrement and observes contention through a wrapper around std::mutex. Both
lock/unlock operations still use std::mutex; no accounting logic is replaced.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DXMT = ROOT / "dxmt/src"
TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]',
    re.DOTALL,
)


def function(source: str, signature: str) -> str:
    """Copy an unchanged definition, ignoring braces inside comments/literals."""
    start = source.index(signature)
    depth = 0
    for token in TOKENS.finditer(source, start):
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            depth -= 1
            if depth == 0:
                return source[start:token.end()]
    raise ValueError(f"Unterminated definition: {signature}")


def host_source(surface: str | None = None) -> str:
    if surface is None:
        surface = (DXMT / "d3d9/d3d9_surface.cpp").read_text()
    header = (DXMT / "d3d9/d3d9_surface.hpp").read_text()
    device = (DXMT / "d3d9/d3d9_device.cpp").read_text()
    device_header = (DXMT / "d3d9/d3d9_device.hpp").read_text()
    formats = (DXMT / "d3d9/d3d9_format.cpp").read_text()
    format_header = (DXMT / "d3d9/d3d9_format.hpp").read_text()
    fields_start = header.index("private:\n  // Lifetime: device held by surface;")
    fields = header[fields_start:header.index("\npublic:\n  void markLosable();", fields_start)]
    fields = fields.replace("std::mutex m_publicRefMutex", "ObservedMutex m_publicRefMutex")
    constants = "\n".join(
        line for line in format_header.splitlines() if line.startswith("constexpr D3DFORMAT ")
    )
    format_functions = "\n".join(
        function(formats, signature)
        for signature in (
            "uint32_t\nD3DFormatBytesPerPixel(",
            "uint32_t\nD3DFormatRowPitch(",
            "uint32_t\nD3DFormatLockPitch(",
            "bool\nIsCompressedFormat(",
            "uint32_t\nD3DFormatRowCount(",
        )
    )
    hooks = "\n".join(
        function(device_header, signature)
        for signature in (
            "void\n  onLosableResourceCreated(",
            "void\n  onLosableResourceDestroyed(",
            "uint32_t\n  losableResourceCount() const",
        )
    )
    reset_gate = function(
        device,
        "if (!m_isEx && m_losableResourceCount.load(std::memory_order_relaxed) != 0) {",
    )
    definitions = "\n".join(
        function(surface, signature)
        for signature in (
            "MTLD3D9Surface::MTLD3D9Surface(",
            "MTLD3D9Surface::~MTLD3D9Surface()",
            "void\nMTLD3D9Surface::markLosable()",
            "void\nMTLD3D9Surface::markImplicitLosable()",
            "ULONG STDMETHODCALLTYPE\nMTLD3D9Surface::AddRef()",
            "ULONG STDMETHODCALLTYPE\nMTLD3D9Surface::Release()",
        )
    )
    return PREAMBLE + constants + format_functions + DEVICE_PREFIX + hooks + DEVICE_SUFFIX + reset_gate + r"""
    return D3D_OK;
  }
};

class MTLD3D9Surface final : public ComObject<IUnknown> {
public:
  MTLD3D9Surface(
      MTLD3D9Device *, const D3DSURFACE_DESC &, IUnknown *, WMT::Reference<WMT::Texture>,
      uint32_t, bool, WMTTextureType, WMT::Reference<WMT::Buffer> = {},
      void * = nullptr, uint32_t = 0, uint32_t = 0, void * = nullptr,
      Rc<dxmt::Texture> = nullptr, bool = false, IDirect3DBaseTexture9 * = nullptr);
  ~MTLD3D9Surface();
  ULONG STDMETHODCALLTYPE AddRef() override;
  ULONG STDMETHODCALLTYPE Release() override;
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void **) override { return E_NOINTERFACE; }
  void markLosable();
  void markImplicitLosable();
""" + fields + r"""
  struct DestructionWitness {
    ~DestructionWitness() { ++destructions; }
  } m_test_witness;
};
""" + definitions + TESTS


PREAMBLE = r"""
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#define WIN32_LEAN_AND_MEAN
#include <d3d9.h>
namespace surface_lifetime_test {
void afterPublicDecrement(const void *, uint32_t);
}
#include "com/com_object.hpp"
#include "com/com_pointer.hpp"
#define D3D9_CENSUS(...) ((void)0)

namespace surface_lifetime_test {
struct Schedule {
  std::mutex mutex;
  std::condition_variable changed;
  const void *object = nullptr;
  std::thread::id releaseThread;
  std::thread::id getterThread;
  bool active = false;
  bool releasePaused = false;
  bool getterBlocked = false;
  bool getterDone = false;
  bool releaseMayContinue = false;
};
static Schedule schedule;
[[noreturn]] static void timeout(const char *stage) {
  std::fprintf(stderr, "FAIL: deterministic overlap scheduling timeout at %s\n", stage);
  std::abort();
}
void afterPublicDecrement(const void *object, uint32_t count) {
  std::unique_lock lock(schedule.mutex);
  if (!schedule.active || object != schedule.object || count != 0 ||
      std::this_thread::get_id() != schedule.releaseThread)
    return;
  schedule.releasePaused = true;
  schedule.changed.notify_all();
  if (!schedule.changed.wait_for(lock, std::chrono::seconds(5),
                                [] { return schedule.releaseMayContinue; }))
    timeout("Release after public decrement");
}
// Surface mutex instrumentation only: record a failed actual try_lock, then
// perform the actual blocking lock. This lets the test distinguish a getter
// serialized behind Release from one that completed while Release was paused.
class ObservedMutex {
  std::mutex mutex;
public:
  void lock() {
    bool getter;
    {
      std::lock_guard lock(schedule.mutex);
      getter = schedule.active && std::this_thread::get_id() == schedule.getterThread;
    }
    if (!getter) {
      mutex.lock();
    } else if (!mutex.try_lock()) {
      {
        std::lock_guard lock(schedule.mutex);
        schedule.getterBlocked = true;
        schedule.changed.notify_all();
      }
      mutex.lock();
    }
  }
  void unlock() { mutex.unlock(); }
};
}
using surface_lifetime_test::ObservedMutex;
using WMTPixelFormat = unsigned;
using WMTTextureType = unsigned;
namespace WMT {
struct Texture {};
struct Buffer {};
template <class T> struct Reference {
  bool operator!=(std::nullptr_t) const { return false; }
  WMTPixelFormat pixelFormat() const { return 0; }
};
}
namespace dxmt {
template <class T> using Rc = std::shared_ptr<T>;
struct Texture {};
struct ComPrivateData {};
struct D3D9SurfaceLockState { uint32_t map_count = 0; bool dc_open = false; };
struct D9LazyMirrorHost {};
static unsigned destructions;
static void guest_free(void *p) { std::free(p); }
namespace str {
template <class... T> std::string format(const T &...) { return {}; }
}
struct Logger { static void warn(const std::string &) {} };
static bool d9SurfaceRefTraceEnabled() { return false; }
static void d9TraceSurfaceRef(
    const char *, const void *, const void *, ULONG, ULONG, const D3DSURFACE_DESC &) {}
"""

DEVICE_PREFIX = r"""
enum class DeviceState { Operational, NotReset };
struct MTLD3D9Device {
  std::atomic<unsigned> refs{1};
  std::atomic<unsigned> pins{0};
  std::atomic<unsigned> unpins{0};
  std::atomic<unsigned> created{0};
  std::atomic<unsigned> destroyed{0};
  bool m_isEx = false;
  std::atomic<uint32_t> m_losableResourceCount{0};
  std::atomic<int64_t> m_reportedTextureMemory{0};
  std::atomic<DeviceState> m_deviceState{DeviceState::Operational};
  std::function<void()> onFinalRelease;
  ULONG AddRef() { ++pins; return ++refs; }
  ULONG Release() {
    assert(refs != 0);
    ++unpins;
    unsigned result = --refs;
    if (!result && onFinalRelease) onFinalRelease();
    return result;
  }
  void traceLosableResource(
      const char *action, const char *, const void *, uint32_t, int64_t) {
    if (!std::strcmp(action, "created")) ++created;
    else { assert(!std::strcmp(action, "destroyed")); ++destroyed; }
  }
  void LogPresentRequest(const char *, const D3DPRESENT_PARAMETERS &, HRESULT) {}
"""

DEVICE_SUFFIX = r"""
  HRESULT resetGate() {
    D3DPRESENT_PARAMETERS parameters{};
    D3DPRESENT_PARAMETERS *pPresentationParameters = &parameters;
    (void)pPresentationParameters;
"""

TESTS = r"""
static void check(bool good, const char *message) {
  if (!good) { std::fprintf(stderr, "FAIL: %s\n", message); std::exit(1); }
}
static MTLD3D9Surface *makeSurface(MTLD3D9Device &device, bool selfPin = true,
                                 D3DPOOL pool = D3DPOOL_DEFAULT,
                                 IDirect3DBaseTexture9 *parent = nullptr) {
  D3DSURFACE_DESC desc{};
  desc.Format = D3DFMT_A8R8G8B8;
  desc.Usage = pool == D3DPOOL_DEFAULT ? D3DUSAGE_RENDERTARGET : 0;
  desc.Pool = pool;
  desc.Width = 64; desc.Height = 64;
  return new MTLD3D9Surface(&device, desc, nullptr, {}, 0, selfPin, 0,
                            {}, nullptr, 0, 0, nullptr, nullptr, false, parent);
}
static void initialCreate(MTLD3D9Surface *surface) {
  surface->markLosable();
  check(surface->AddRef() == 1, "CreateRenderTarget returns first public ref");
}
static void checkBalanced(const MTLD3D9Device &device, unsigned count) {
  check(device.refs == 1, "Device baseline pin restored");
  check(device.pins == count && device.unpins == count, "Device pin/unpin pairs balance");
  check(device.losableResourceCount() == 0, "No losable resources remain");
  check(device.m_reportedTextureMemory.load() == 0, "Reported texture memory restored");
}
static void reacquireCount() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device);
  initialCreate(surface);
  check(device.losableResourceCount() == 1, "Explicit create counts once");
  check(device.m_reportedTextureMemory.load() == 16384, "Explicit surface reports 64x64x4 bytes");
  check(device.resetGate() == D3DERR_INVALIDCALL, "Held DEFAULT resource refuses non-Ex Reset");
  Com<MTLD3D9Surface, false> binding = surface;
  check(surface->Release() == 0, "First public release reaches zero while bound");
  check(device.losableResourceCount() == 0, "Private-only binding does not block Reset");
  check(device.resetGate() == D3D_OK, "Private-only binding passes the production Reset gate");
  // The same production ref helper used by GetRenderTarget re-acquires it.
  auto *obtained = dxmt::ref(surface);
  check(obtained == surface, "Getter preserves Surface identity");
  std::fprintf(stderr, "OBSERVED: re-acquired DEFAULT Surface count=%u bytes=%lld\n",
               device.losableResourceCount(), (long long)device.m_reportedTextureMemory.load());
  check(device.losableResourceCount() == 1, "Re-acquired DEFAULT Surface must re-enter losable accounting");
  check(device.m_reportedTextureMemory.load() == 16384, "Re-acquired DEFAULT Surface restores memory report");
  check(device.resetGate() == D3DERR_INVALIDCALL, "Getter-held DEFAULT Surface refuses Reset");
  check(obtained->Release() == 0, "Re-acquired public reference releases");
  binding = nullptr;
  checkBalanced(device, 2);
  check(destructions == 1, "Surface destroyed once after binding release");
}
static void finalReleaseAfterUnbind() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device);
  initialCreate(surface);
  Com<MTLD3D9Surface, false> binding = surface;
  check(surface->Release() == 0, "First zero release consumes constructor self-pin");
  auto *obtained = dxmt::ref(surface);
  binding = nullptr;
  check(destructions == 0, "Re-acquired public reference owns the remaining private pin");
  check(obtained->GetPrivateRefCount() == 1, "Unbound surface has only the public-lifetime private pin");
  std::fprintf(stderr, "OBSERVED: final Release after self-pin consumed and binding removed\n");
  // The real ComObject::Release may delete the object here. Surface::Release
  // must capture everything it needs before calling it, including the Device.
  check(obtained->Release() == 0, "Final public release returns zero safely");
  check(destructions == 1, "Final public release destroys Surface exactly once");
  checkBalanced(device, 2);
}
static void releaseGetterOverlap() {
  using namespace surface_lifetime_test;
  MTLD3D9Device device;
  auto *surface = makeSurface(device);
  initialCreate(surface);
  Com<MTLD3D9Surface, false> binding = surface;
  {
    std::lock_guard lock(schedule.mutex);
    schedule.object = surface;
    schedule.active = true;
  }
  ULONG released = 123;
  MTLD3D9Surface *obtained = nullptr;
  std::thread release([&] {
    {
      std::lock_guard lock(schedule.mutex);
      schedule.releaseThread = std::this_thread::get_id();
    }
    released = surface->Release();
  });
  {
    std::unique_lock lock(schedule.mutex);
    if (!schedule.changed.wait_for(lock, std::chrono::seconds(5),
                                  [] { return schedule.releasePaused; }))
      timeout("waiting for Release decrement");
  }
  std::thread getter([&] {
    {
      std::lock_guard lock(schedule.mutex);
      schedule.getterThread = std::this_thread::get_id();
    }
    obtained = dxmt::ref(surface);
    {
      std::lock_guard lock(schedule.mutex);
      schedule.getterDone = true;
      schedule.changed.notify_all();
    }
  });
  bool getterWasBlocked;
  {
    std::unique_lock lock(schedule.mutex);
    if (!schedule.changed.wait_for(lock, std::chrono::seconds(5),
                                  [] { return schedule.getterBlocked || schedule.getterDone; }))
      timeout("waiting for getter contention or completion");
    getterWasBlocked = schedule.getterBlocked;
    schedule.releaseMayContinue = true;
    schedule.changed.notify_all();
  }
  release.join();
  getter.join();
  {
    std::lock_guard lock(schedule.mutex);
    schedule.active = false;
  }
  std::fprintf(stderr, "OBSERVED: overlapped getter blocked=%d count=%u bytes=%lld\n",
               getterWasBlocked, device.losableResourceCount(),
               (long long)device.m_reportedTextureMemory.load());
  check(released == 0 && obtained == surface, "Overlapping Release/getter preserve their COM results");
  check(device.losableResourceCount() == 1, "Overlapping getter must retain one losable resource");
  check(device.m_reportedTextureMemory.load() == 16384, "Overlapping getter must retain texture-memory report");
  check(device.resetGate() == D3DERR_INVALIDCALL, "Overlapping getter-held DEFAULT Surface refuses Reset");
  check(getterWasBlocked, "Getter serializes behind the paused zero-reference Release edge");
  check(obtained->Release() == 0, "Overlapping getter reference releases safely");
  binding = nullptr;
  checkBalanced(device, 2);
  check(destructions == 1, "Overlap cleanup destroys Surface exactly once");
}
static void ordinaryStandalone() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device);
  initialCreate(surface);
  surface->markLosable();
  check(device.losableResourceCount() == 1, "Repeated explicit mark does not count twice");
  check(surface->AddRef() == 2 && device.pins == 1, "Second public ref does not add a Device pin");
  check(surface->Release() == 1 && device.losableResourceCount() == 1, "Non-final release preserves accounting");
  check(surface->Release() == 0, "Unbound standalone releases safely through self-pin");
  check(destructions == 1, "Standalone final release destroys once");
  checkBalanced(device, 1);
}
static void implicitReacquire() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device, false);
  Com<MTLD3D9Surface, false> binding = surface;
  surface->markImplicitLosable(); surface->markImplicitLosable();
  check(device.losableResourceCount() == 0, "Implicit mark without public refs does not count");
  for (unsigned cycle = 0; cycle < 3; ++cycle) {
    check(dxmt::ref(surface) == surface, "Implicit getter returns same object");
    check(device.losableResourceCount() == 1, "Implicit public edge counts exactly once");
    check(device.m_reportedTextureMemory.load() == 0, "Implicit surface never reports explicit bytes");
    check(surface->AddRef() == 2 && device.losableResourceCount() == 1, "Extra implicit ref does not double-count");
    check(surface->Release() == 1, "Implicit non-final release returns one");
    check(surface->Release() == 0 && device.losableResourceCount() == 0, "Implicit zero edge removes count");
    check(surface->Release() == 0, "Redundant Release at zero is clamped");
  }
  binding = nullptr;
  check(destructions == 1, "Implicit private owner destroys once");
  checkBalanced(device, 3);
}
static void systemmem() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device, true, D3DPOOL_SYSTEMMEM);
  check(surface->AddRef() == 1, "SYSTEMMEM create returns public ref");
  Com<MTLD3D9Surface, false> binding = surface;
  check(surface->Release() == 0, "SYSTEMMEM first zero release");
  dxmt::ref(surface);
  check(device.losableResourceCount() == 0 && device.m_reportedTextureMemory.load() == 0,
        "SYSTEMMEM re-acquisition never counts as losable");
  check(device.resetGate() == D3D_OK, "SYSTEMMEM passes non-Ex Reset gate");
  check(surface->Release() == 0, "SYSTEMMEM re-acquired ref releases");
  binding = nullptr;
  checkBalanced(device, 2);
  check(destructions == 1, "SYSTEMMEM destroys once");
}
// Complete real base-texture interface; only the COM edge is invoked. The
// parent can destroy its Surface during delegated Release, just like m_levels.
struct ParentTexture final : ComObject<IDirect3DBaseTexture9> {
  Com<MTLD3D9Surface, false> owned;
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void **) override { return E_NOINTERFACE; }
  HRESULT STDMETHODCALLTYPE GetDevice(IDirect3DDevice9 **) override { std::abort(); }
  HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID, const void *, DWORD, DWORD) override { std::abort(); }
  HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID, void *, DWORD *) override { std::abort(); }
  HRESULT STDMETHODCALLTYPE FreePrivateData(REFGUID) override { std::abort(); }
  DWORD STDMETHODCALLTYPE SetPriority(DWORD) override { std::abort(); }
  DWORD STDMETHODCALLTYPE GetPriority() override { std::abort(); }
  void STDMETHODCALLTYPE PreLoad() override { std::abort(); }
  D3DRESOURCETYPE STDMETHODCALLTYPE GetType() override { std::abort(); }
  DWORD STDMETHODCALLTYPE SetLOD(DWORD) override { std::abort(); }
  DWORD STDMETHODCALLTYPE GetLOD() override { std::abort(); }
  DWORD STDMETHODCALLTYPE GetLevelCount() override { std::abort(); }
  HRESULT STDMETHODCALLTYPE SetAutoGenFilterType(D3DTEXTUREFILTERTYPE) override { std::abort(); }
  D3DTEXTUREFILTERTYPE STDMETHODCALLTYPE GetAutoGenFilterType() override { std::abort(); }
  void STDMETHODCALLTYPE GenerateMipSubLevels() override { std::abort(); }
};
static void parentDelegation() {
  MTLD3D9Device device;
  auto *parent = new ParentTexture;
  check(parent->AddRef() == 1, "Parent starts with one public ref");
  auto *surface = makeSurface(device, false, D3DPOOL_DEFAULT, parent);
  parent->owned = surface;
  check(surface->AddRef() == 2, "Surface AddRef returns the parent's counter");
  check(device.pins == 0 && device.losableResourceCount() == 0, "Delegated Surface does not pin/count independently");
  check(surface->Release() == 1, "Surface Release delegates to parent's counter");
  check(surface->Release() == 0, "Parent may delete the Surface during delegated Release");
  check(destructions == 1, "Delegated Surface destroyed exactly once by parent");
  checkBalanced(device, 0);
}
static void deviceLastUnpin() {
  MTLD3D9Device device;
  auto *surface = makeSurface(device, false);
  Com<MTLD3D9Surface, false> binding = surface;
  surface->markImplicitLosable();
  surface->AddRef();
  device.Release(); // App drops its Device ref; the Surface is the last pin.
  device.onFinalRelease = [&] { binding = nullptr; };
  check(surface->Release() == 0, "Device teardown can delete implicit Surface during last unpin");
  check(device.refs == 0 && device.pins == 1 && device.unpins == 2, "Final Device pin balanced with app release");
  check(device.losableResourceCount() == 0 && destructions == 1, "Loss count removed before Device destroys Surface");
}
} // namespace dxmt
int main(int argc, char **argv) {
  using namespace dxmt;
  check(argc == 2, "Expected exactly one case name");
  if (!std::strcmp(argv[1], "reacquire-count")) reacquireCount();
  else if (!std::strcmp(argv[1], "final-release-after-unbind")) finalReleaseAfterUnbind();
  else if (!std::strcmp(argv[1], "release-getter-overlap")) releaseGetterOverlap();
  else if (!std::strcmp(argv[1], "standalone")) ordinaryStandalone();
  else if (!std::strcmp(argv[1], "implicit")) implicitReacquire();
  else if (!std::strcmp(argv[1], "systemmem")) systemmem();
  else if (!std::strcmp(argv[1], "parent-delegation")) parentDelegation();
  else if (!std::strcmp(argv[1], "device-last-unpin")) deviceLastUnpin();
  else { std::fprintf(stderr, "Unknown case: %s\n", argv[1]); return 2; }
  std::printf("PASS: %s\n", argv[1]);
}
"""

CASES = (
    "reacquire-count",
    "final-release-after-unbind",
    "release-getter-overlap",
    "standalone",
    "implicit",
    "systemmem",
    "parent-delegation",
    "device-last-unpin",
)


def instrument_com_header(directory: Path) -> None:
    """Copy production COM code, adding only the deterministic pause hook."""
    source = (DXMT / "util/com/com_object.hpp").read_text()
    start = source.index("template <typename... Base> class ComObject :")
    end = source.index("template <typename... Base> class ComObjectWithInitialRef", start)
    body = source[start:end]
    decrement = "    uint32_t refCount = --m_refCount;\n"
    if body.count(decrement) != 1:
        raise ValueError("Expected one public decrement in production ComObject")
    body = body.replace(
        decrement,
        decrement + "    ::surface_lifetime_test::afterPublicDecrement(this, refCount);\n",
    )
    com_directory = directory / "com"
    com_directory.mkdir()
    (com_directory / "com_object.hpp").write_text(source[:start] + body + source[end:])
    (directory / "util_likely.hpp").write_text((DXMT / "util/util_likely.hpp").read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, help="Run one isolated lifetime edge")
    args = parser.parse_args()
    cases = (args.case,) if args.case else CASES
    failures = []
    with tempfile.TemporaryDirectory(prefix="madeira-d3d9-surface-lifetime-") as temporary:
        directory = Path(temporary)
        source = directory / "surface.cpp"
        binary = directory / "surface"
        instrument_com_header(directory)
        source.write_text(host_source())
        subprocess.run(
            [
                "clang++", "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-private-field", "-Wno-ignored-attributes", "-Wno-pragma-pack",
                "-Wno-microsoft-exception-spec",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-fno-omit-frame-pointer", "-pthread", "-I", str(directory),
                "-I", str(ROOT / "wine/build-macos/include"),
                "-I", str(ROOT / "wine/include"), "-I", str(DXMT / "util"),
                str(source), "-o", str(binary),
            ],
            check=True,
            timeout=60,
        )
        for case in cases:
            result = subprocess.run([str(binary), case], capture_output=True, text=True, timeout=20)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr, flush=True)
            if result.returncode:
                failures.append(case)
                print(f"FAIL: {case} exited {result.returncode}", flush=True)
    if failures:
        print("FAILED: " + ", ".join(failures), flush=True)
        return 1
    print("PASS: production Surface public/private lifetime and Reset accounting contracts", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
