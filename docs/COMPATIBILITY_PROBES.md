# Future-game compatibility probes

This work targets shared Windows API behavior that other games may use. It does
not reopen the completed Sora/Kingdom Rush investigations or claim compatibility
with an untested title. Steam and Epic both use this runtime, so a runtime fix
must be validated independently of its store.

## Current probe coverage

`tests/x64/dxgi-resource-contract-probe.c` exercises resource offer/reclaim and
format-specific multisampling through real D3D11/DXGI interfaces. It creates
textures, checks per-resource discard results, and verifies retained content by
GPU readback. Its MSAA cases compare capability queries with creation and
clear/resolve/readback; an unavailable feature is reported as SKIP, not rendered
success. This covers a class of future loading, memory-management and graphics
settings paths, not a particular game's workaround.

The production host check is `tests/host/check-dxgi-resource-contracts.py`. It
compiles the actual DXMT methods, format mapping and capability tables. Metal
device queries are explicit doubles: passing this check does not prove GPU
rendering, memory-pressure eviction or hardware performance.

`tests/x64/display-mode-state-probe.c` exercises the normal Win32 sequence for
saved, current and deferred display modes: validate a mode, save it with
`CDS_UPDATEREGISTRY | CDS_NORESET`, query current/registry separately, then apply
with a NULL request. It also checks temporary mode restoration and monitor,
desktop and display-change consistency. It restores the original mode on exit.
This is useful for future launchers and fullscreen/video-settings flows, in
addition to the existing DisplayConfig topology probe.

The production host check is `tests/host/check-display-mode-state.py`. UI and
server notifications are host boundaries; mode selection/state/publication
logic comes from `build/win32u-unix/sysparams_ios.c`. Registry mode here is scoped
to the running Wine session. Per-game next-launch settings remain owned by the
app; this change does not write arbitrary game preferences to the device.

The focused fixes initialize reclaim results for retained resources, constrain
MSAA support by format and maximum sample count, and preserve saved/deferred
display modes. Mode publication uses one publisher with latest-state convergence;
concurrent and synchronous callback requests queue work without holding the state
mutex across notifications. Tests cover both a paused publisher and a state
commit before its caller reaches publication.

Microsoft's contracts specify [per-resource reclaim results](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgidevice2-reclaimresources),
[format/sample-count-specific MSAA support](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11device-checkmultisamplequalitylevels),
and [saved/deferred display-mode semantics](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw).

Run the focused host checks from the repository root:

```sh
python3 tests/host/check-dxgi-resource-contracts.py
python3 tests/host/check-display-mode-state.py
python3 tests/host/check-virtual-display-config.py
```

The Windows probes are diagnostic executables, not iOS apps. Compile/link/strip
them in task-owned APFS scratch with the pinned llvm-mingw toolchain, archive
outputs and evidence externally, and promptly clean that scratch. Run them via
the ordinary game-launch path on the selected iPad; retain their terminal
PASS/FAIL/SKIP and process exit code. A compiled executable is not a device test.
See [BUILDING](BUILDING.md#temporary-storage-for-ipa-checks-local-workflow).

## Additional directions for future games

The next useful tests should target distinct runtime paths, rather than repeat a
successful menu boot of an old title:

- **Video intros and cutscenes:** test media-source playback separately from a
  game that directly requests a Media Foundation decoder transform, including
  seek, end-of-stream and returning to rendering. The iOS bridge has an Apple
  MP4/MOV parser path, while its transform path is specialized for WMA; that
  existing distinction means a video that works through one path does not prove
  all video APIs work. Do not replace unsupported transforms with fake success.
- **Frame pacing:** test a real presenting swapchain's frame statistics and
  latency behavior over multiple displayed frames. The current simplified
  `GetFrameStatistics` reports a zero QPC synchronization time. Correcting this
  needs real presentation-time evidence; sampling the clock at query time would
  not establish the time the reported frame was displayed.
- **Controller lifecycle:** add a future probe for connecting/disconnecting
  after launch and focus changes, with actual XInput/Raw Input events in the
  guest. The existing host/controller tester proves a different part of the
  chain and cannot establish every game's input path.
- **Longer loading and memory pressure:** test allocation/decommit/recommit,
  shared views, resource recreation and sustained gameplay under bounded memory.
  Record guest allocation errors separately from iOS process termination and
  GPU failures. A larger virtual address range or JIT pool does not supply more
  physical memory.
- **Runtime and startup dependencies:** use small probes for COM/WinRT class
  activation, runtime loading and launch-time services, with real failure codes.
  A bundled DLL or a store download is only one prerequisite; it cannot establish
  the corresponding API implementation or Windows kernel-service support.

These are investigation directions, not five additional completed fixes. Add a
reproducible probe before changing the runtime; keep unsupported paths explicit,
avoid game-name branches, and preserve a failing baseline before reporting a
fix. Each result should distinguish host logic, compiled Windows PE, actual
iPad API behavior and real-game evidence.

## Evidence location and remaining device gate

The current resource/MSAA and mode-state baseline reports and Windows PE probes
are under external `app/Build/Products/IPAInspection/future-graphics-probes/` and
`future-display-probes/`. The host probes reproduced concrete production defects;
their Windows counterparts have not yet run on He Ke's currently locked iPad.
Do not report a new game's successful launch from these host checks alone.

The frozen host evidence includes 347 graphics assertions, 31 mode scenarios,
the original 37 CCD scenarios, ASan/UBSan and O2 execution, independent Python
`-O` checks, and five independent TSan cases. Graphics eight runtime mutations
are independently compiled and rejected; the nine display mutation reports were
reviewed separately. Code and Python approvals are recorded under external
`future-code-review/` and `future-python-review/`.

The win32u build script also now creates a fresh archive before its single
FreeType merge. Two full 46-object rebuilds retain 89 archive members with no
duplicate accumulation; the compiled object payloads match the reviewed build.
The temporary archive is published only after successful assembly/merge and
its task-owned staging directory is cleaned on exit.

The 2026-10-12 package is 0.1.3/build12. ARM64EC, i386 and aarch64 D3D11 DLLs
are built from the same reviewed source; the actual IPA roundtrip checks all
three hashes. It passed the full iOS build and signature/resource validation
and is installed on He Ke's iPad. SDK startup was explicitly denied Locked,
so actual device probe execution remains pending. The package also contains the
Epic integration whose real sign-in/download/launch tests remain pending.
