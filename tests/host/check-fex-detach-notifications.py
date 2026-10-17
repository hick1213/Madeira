#!/usr/bin/env python3
"""Execute actual FEX notification bodies across its own CRT detach boundary.

The host stand-ins expose every tracker/context/thread-state access. The CRT
sequence invokes DllMain before real C++ global destructors, whose allocators
reenter these notification bodies. This verifies admission, not peer-thread
quiescence or the ARM64EC MinGW startup binary (checked separately).
"""

import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def fixture(source: str) -> str:
    declarations = re.findall(
        r"^constinit std::atomic<uint32_t> MemoryNotificationsClosed.*?;", source, re.M
    )
    boundary = "\n".join(declarations)
    # The original DLL linked MinGW's no-op DllMain; retain that baseline for RED.
    if 'extern "C" BOOL WINAPI DllMain(' in source:
        boundary += function(source, 'extern "C" BOOL WINAPI DllMain(')
    else:
        boundary += "BOOL DllMain(HMODULE, DWORD, LPVOID) { return TRUE; }"
    helpers = ""
    for signature in [
        "static uint32_t* IosUnmapLockDepthSlot(",
        "static bool* IosInLockedRWXReadSlot(",
    ]:
        if signature in source:
            helpers += function(source, signature)
    bodies = "\n".join(
        function(source, signature)
        for signature in [
            "void ProcessTerm(",
            "void NotifyMemoryAlloc(",
            "void NotifyMemoryFree(",
            "void NotifyMemoryProtect(",
            'extern "C" void NotifyImageMap(',
            "NTSTATUS NotifyMapViewOfSection(",
            "void NotifyUnmapViewOfSection(",
            "void FlushInstructionCacheHeavy(",
            "void BTCpu64FlushInstructionCache(",
            "void BTCpu64NotifyMemoryDirty(",
            "void BTCpu64NotifyReadFile(",
        ]
    )
    return r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
using HANDLE = void*;
using HMODULE = void*;
using LPVOID = void*;
using SIZE_T = size_t;
using ULONG = unsigned long;
using DWORD = unsigned long;
using BOOL = int;
using NTSTATUS = long;
#define WINAPI
constexpr BOOL TRUE = 1;
constexpr DWORD DLL_PROCESS_DETACH = 0, DLL_PROCESS_ATTACH = 1;
constexpr DWORD DLL_THREAD_ATTACH = 2, DLL_THREAD_DETACH = 3;
constexpr NTSTATUS STATUS_SUCCESS = 0;
constexpr ULONG MEM_RESET = 0x80000, MEM_RESET_UNDO = 0x1000000;
static bool poisoned, teardown;
static bool clobber_on_lock, teb_clobbered;
static unsigned tracker_accesses, cpu_accesses, tracker_ops, context_accesses;
static unsigned image_ops, queued, checks;
static int destructor_mode;
static void check(bool ok) { ++checks; assert(ok); }
static void live() { assert(!poisoned && "notification accessed destroyed FEX state"); }
struct Lock {
  std::recursive_mutex mutex;
  unsigned depth = 0;
  void lock() { mutex.lock(); ++depth; if (clobber_on_lock) teb_clobbered = true; }
  void unlock() { check(depth != 0); --depth; mutex.unlock(); }
};
static Lock ThreadCreationMutex;
struct Context {
  Lock mutex;
  Lock& GetCodeInvalidationMutex() { live(); ++context_accesses; return mutex; }
};
static Context context;
static Context* CTX = &context;
struct FrontendThreadData { bool InLockedRWXRead = false; };
static FrontendThreadData frontend;
static int thread;
static int* current_thread = &thread;
struct CPUArea {
  int* ThreadState() { return current_thread; }
};
static CPUArea GetCPUArea() { live(); ++cpu_accesses; return {}; }
static FrontendThreadData* GetFrontendThreadData(int*) { live(); return &frontend; }
alignas(8) static unsigned char teb[0x1800];
static void* NtCurrentTeb() {
  assert(!teb_clobbered && "x18 TEB read after native lock clobber");
  return teb;
}
// Host boundary for iOS TSD lookup; ARM64EC pointer retention is checked in PE.
static void* IOSLoadTEB() { return teb; }
static void notify_all();
struct Tracker {
  bool rwx = true;
  ~Tracker() { if (teardown) { poisoned = true; notify_all(); } }
  void HandleMemoryProtectionNotification(uint64_t, uint64_t, ULONG) { live(); ++tracker_ops; }
  void InvalidateAlignedInterval(uint64_t, uint64_t, bool) {
    live(); ++tracker_ops; CTX->GetCodeInvalidationMutex();
  }
  void HandleImageMap(const std::string&, uint64_t) { live(); ++tracker_ops; }
  std::pair<uint64_t,uint64_t> InvalidateContainingSection(uint64_t p, bool) {
    live(); ++tracker_ops; return {p, 4096};
  }
  bool BeginUntrackedWriteLocked(uint64_t, uint64_t) { live(); ++tracker_ops; return rwx; }
};
template<typename T> struct ObservedOptional {
  std::optional<T> value;
  explicit operator bool() const { live(); ++tracker_accesses; return value.has_value(); }
  T* operator->() { live(); ++tracker_accesses; return &*value; }
};
static ObservedOptional<Tracker> InvalidationTracker;
static bool ImageTracker = true;
static void HandleImageMap(uint64_t) { live(); ++image_ops; }
static void HandleImageUnmap(uint64_t, uint64_t) { live(); ++image_ops; }
static void QueuePendingImageMap(uint64_t) { live(); ++queued; }
namespace fextl { using string = std::string; }
namespace FEX::Windows {
static std::string GetSectionFilePath(uint64_t) { live(); return "module.dll"; }
static std::string BaseName(const std::string& p) { return p; }
}
namespace LogMan::Msg {
template<typename... T> void EFmt(const char*, T&&...) { live(); }
}
''' + boundary + helpers + bodies + r'''
static void notify_all() {
  void* address = reinterpret_cast<void*>(0x10000);
  for (bool after : {false, true}) {
    NotifyMemoryAlloc(address, 4096, 0, 4, after, 0);
    NotifyMemoryFree(address, 4096, 0, after, 0);
    NotifyMemoryProtect(address, 4096, 4, after, 0);
    NotifyUnmapViewOfSection(address, after, 0);
    BTCpu64NotifyReadFile(nullptr, address, 4096, after, 0);
  }
  NotifyImageMap(address);
  check(NotifyMapViewOfSection(nullptr, address, nullptr, 4096, 0, 4) == 0);
  FlushInstructionCacheHeavy(address, 4096);
  BTCpu64FlushInstructionCache(address, 4096);
  BTCpu64NotifyMemoryDirty(address, 4096);
}
// These are real process-exit C++ destructors, after the emulated CRT's DllMain.
struct ContextDestructor {
  ~ContextDestructor() {
    if (teardown) {
      poisoned = true;
      CTX = nullptr;
      notify_all();
      std::puts("PASS: context-free allocator reentry rejected");
    }
  }
};
static ContextDestructor context_destructor;
struct TrackerDestructor {
  ~TrackerDestructor() {
    if (teardown) {
      InvalidationTracker.value.reset();
      std::puts("PASS: tracker-destructor allocator reentry rejected");
    }
  }
};
static TrackerDestructor tracker_destructor;
int main(int argc, char** argv) {
  assert(argc == 2);
  InvalidationTracker.value.emplace();
  check(DllMain(nullptr, DLL_PROCESS_ATTACH, nullptr) == TRUE);
  if (!std::strcmp(argv[1], "active")) {
    notify_all();
    check(tracker_ops == 9 && image_ops == 2);
    check(ThreadCreationMutex.depth == 0 && context.mutex.depth == 0);
    unsigned before = tracker_ops;
    for (DWORD reason : {DLL_THREAD_ATTACH, DLL_THREAD_DETACH}) {
      check(DllMain(nullptr, reason, nullptr) == TRUE);
      notify_all();
    }
    ProcessTerm(nullptr, false, 0);
    // Other DLL detachers run before this DLL's own DllMain.
    notify_all();
    ProcessTerm(nullptr, true, 0);
    check(tracker_ops == before + 27);
    InvalidationTracker.value.reset();
    check(NotifyMapViewOfSection(nullptr, nullptr, nullptr, 0, 0, 0) == 0);
    check(queued == 1);
  } else if (!std::strcmp(argv[1], "destructors")) {
    check(DllMain(nullptr, DLL_PROCESS_DETACH, nullptr) == TRUE);
    teardown = true;
  } else if (!std::strcmp(argv[1], "closed")) {
    check(DllMain(nullptr, DLL_PROCESS_DETACH, reinterpret_cast<void*>(1)) == TRUE);
    check(DllMain(nullptr, DLL_THREAD_ATTACH, nullptr) == TRUE);
    poisoned = true;
    notify_all();
    check(!tracker_accesses && !cpu_accesses && !context_accesses);
  } else if (!std::strcmp(argv[1], "read-pair")) {
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, false, 0);
    check(ThreadCreationMutex.depth == 1 && context.mutex.depth == 1);
    current_thread = nullptr;
    check(DllMain(nullptr, DLL_PROCESS_DETACH, nullptr) == TRUE);
    InvalidationTracker.value.reset();
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, true, -1);
    check(ThreadCreationMutex.depth == 0 && context.mutex.depth == 0);
    poisoned = true;
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, true, 0);
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, false, 0);
  } else if (!std::strcmp(argv[1], "unmap-pair")) {
    NotifyUnmapViewOfSection(nullptr, false, 0);
    NotifyUnmapViewOfSection(nullptr, false, 0);
    check(ThreadCreationMutex.depth == 2);
    current_thread = nullptr;
    check(DllMain(nullptr, DLL_PROCESS_DETACH, nullptr) == TRUE);
    InvalidationTracker.value.reset();
    NotifyUnmapViewOfSection(nullptr, true, -1);
    NotifyUnmapViewOfSection(nullptr, true, 0);
    check(ThreadCreationMutex.depth == 0);
    poisoned = true;
    NotifyUnmapViewOfSection(nullptr, true, 0);
    NotifyUnmapViewOfSection(nullptr, false, 0);
  } else if (!std::strcmp(argv[1], "unmap-teb")) {
    clobber_on_lock = true;
    NotifyUnmapViewOfSection(nullptr, false, 0);
    check(teb_clobbered && ThreadCreationMutex.depth == 1);
    teb_clobbered = false; // Wine restores x18 when entering the AFTER callback.
    NotifyUnmapViewOfSection(nullptr, true, 0);
    check(ThreadCreationMutex.depth == 0);
  } else if (!std::strcmp(argv[1], "unmap-entry-teb")) {
    teb_clobbered = true;
    NotifyUnmapViewOfSection(nullptr, false, 0);
    check(ThreadCreationMutex.depth == 1);
    NotifyUnmapViewOfSection(nullptr, true, 0);
    check(ThreadCreationMutex.depth == 0);
  } else if (!std::strcmp(argv[1], "untracked-read")) {
    InvalidationTracker.value->rwx = false;
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, false, 0);
    check(ThreadCreationMutex.depth == 0 && context.mutex.depth == 0);
    check(DllMain(nullptr, DLL_PROCESS_DETACH, nullptr) == TRUE);
    poisoned = true;
    BTCpu64NotifyReadFile(nullptr, nullptr, 8, true, 0);
  } else { std::abort(); }
  std::printf("PASS: %s (%u checks)\n", argv[1], checks);
}
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, default=Path(__file__).resolve().parents[2]
                        / "FEX/Source/Windows/ARM64EC/Module.cpp")
    args = parser.parse_args()
    source = args.module.read_text()
    with tempfile.TemporaryDirectory(prefix="madeira-fex-detach-") as directory:
        cpp = Path(directory) / "check.cpp"
        binary = Path(directory) / "check"
        cpp.write_text(fixture(source))
        for flags in [["-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"], ["-O2"]]:
            subprocess.run(["clang++", "-std=c++20", "-DFEX_IOS_HOST=1", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", "-Wno-unused-variable", "-Wno-unused-function", "-pthread",
                            *flags, str(cpp), "-o", str(binary)], check=True, timeout=60)
            for case in ["active", "destructors", "closed", "read-pair", "unmap-pair", "unmap-teb", "unmap-entry-teb", "untracked-read"]:
                subprocess.run([str(binary), case], check=True, timeout=10)


if __name__ == "__main__":
    main()
