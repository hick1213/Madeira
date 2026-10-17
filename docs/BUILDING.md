# Building Madeira from a clean checkout (reproducibility record, 2026-09-16)

This is the "scripts to control compilation and installation" record the
LGPL relink obligation depends on (docs/LICENSING.md). Each step says
whether it has been re-executed from a clean checkout. A fresh recursive
clone of the repository at commit 8a8cabe was tested on 2026-09-16 (with
the submodule URLs redirected to the local forks, since nothing is pushed):
the app target does NOT build from the clone alone, because the inputs
marked "not in the repository" below are absent. Two further findings:
`app/Madeira/x86_64-vcruntime` is required by the project but ignored, and
the submodule commits (FEX, its nested rpmalloc fork, wine branch
`madeira-lgpl`, dxmt) exist only locally: the forks named in `.gitmodules`
(all under github.com/willfaust) do not yet carry them, so a recipient's
recursive clone fails at the first submodule until every fork is pushed. This document is the
remediation; steps marked UNVERIFIED have not yet been re-run from scratch.

## Inputs that are not in the repository

| Input | Why absent | How to obtain | Verified from clean |
|---|---|---|---|
| `toolchains/llvm-mingw-20260421-ucrt-macos-universal/` | 122 MB third-party toolchain | `llvm-mingw-20260421-ucrt-macos-universal.tar.xz` from https://github.com/mstorsjo/llvm-mingw/releases/tag/20260421, SHA-256 `bd85a3975723815cef28dbbd2ca2cb0c926f6b348a12a0453f39f7af273cb3f7`, extracted under `toolchains/` | tarball hash recorded; download UNVERIFIED |
| `toolchains/llvm-project/` + `toolchains/llvm-ios-build/` + `toolchains/llvm-host-build/` | LLVM built for iOS (hours) | upstream llvm-project at commit `8dfdcc7b7` ("[libc++] Fix memory leaks when throwing inside std::vector constructor"); configure `llvm-ios-build` with `-DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_SYSROOT=iphoneos -DCMAKE_BUILD_TYPE=Release -DLLVM_HOST_TRIPLE=arm64-apple-ios17.0 -DLLVM_DEFAULT_TARGET_TRIPLE=arm64-apple-ios17.0 -DLLVM_TARGET_ARCH=host -DLLVM_TARGETS_TO_BUILD= -DLLVM_ENABLE_PROJECTS= -DLLVM_BUILD_TOOLS=Off -DLLVM_INCLUDE_TESTS=Off -DLLVM_ENABLE_ZLIB=Off` (values read back from the existing CMakeCache); a host build for tablegen lives in `llvm-host-build` | recipe reconstructed; UNVERIFIED |
| `research/GPTK/Metal Shader Converter 4.0 beta 2.pkg` | Apple installer, 30 MB, licence-bound | Apple developer downloads; SHA-256 `1acc33c87ea663933df89721a998d066106685473020bcbe007cee7a16155734` (pinned in `build/madeira-d3d12/deps.sh`). Only needed to REBUILD the converter fetch; the library itself is tracked | n/a |
| `app/Madeira/x86_64-vcruntime/` | Microsoft Visual C++ 2015-2022 x64 runtime DLLs (concrt140, msvcp140*, vcamp140, vccorlib140, vcruntime140*), redistributable under Microsoft's terms, not under this repository's licence | extract from Microsoft's `vc_redist.x64.exe` (or copy from `C:\Windows\System32` of a licensed Windows install) into that folder | UNVERIFIED |
| `build/wine-mono/wine-mono-11.0.0/` (optional) | Wine Mono, the .NET Framework runtime Wine's mscoree loads (not Microsoft code; licences in `build/wine-mono/COPYING` and `THIRD-PARTY-NOTICES.md`), 41.6 MB download, 228 MB unpacked | `bash build/wine-mono/fetch.sh` downloads `wine-mono-11.0.0-x86.tar.xz` from https://dl.winehq.org/wine/wine-mono/11.0.0/ and checks its SHA-256; `--source` also fetches the matching source tarball. Version and hashes are pinned in `build/wine-mono/pin.sh`, which does not follow `WINE_MONO_VERSION` in `wine/dlls/mscoree/mscoree_private.h`. The Xcode phase "Bundle Wine Mono" runs `build/wine-mono/bundle.sh`, which copies it into `Madeira.app/wine-mono` without the `lib/mono/*-api` reference assemblies and patches the bundled `mscorlib.dll`: **a dirty hack** (4 IL bytes: `GC.Collect` with `GCCollectionMode.Optimized` returns, for Terraria), pinned to the exact file by SHA-256 so the build stops on any other mscorlib; TODO: replace it with a Wine Mono built from source or an upstream fix (see the TODO in `bundle.sh`). Without the folder the app builds without Mono, and .NET Framework programs fail with "Wine Mono is not installed". **Release builds leave Wine Mono out** (2026-10-06): the packaging step deletes `Madeira.app/wine-mono`, and the app downloads it from WineHQ on first use (setup or Settings › .NET Framework, `app/Madeira/WineMono.swift`) | fetch + bundle run on the development machine 2026-10-05; not from a clean checkout |
| A free Apple ID; StikDebug or a pairing file plus LocalDevVPN | signing and JIT runtime requirements | see `docs/JIT.md` | n/a |

## Native build chains (all in the repository)

Run in this order after the inputs above are in place. Outputs are
git-ignored and consumed by the app project.

1. `build/gnutls-ios/build.sh`: GMP 6.3.0, Nettle 3.10.1, GnuTLS 3.8.9 from
   the tracked tarballs in `build/gnutls-ios/src` (SHA256SUMS there) ->
   `app/Madeira/lib{gmp,nettle,hogweed,gnutls}.a` (these four outputs are
   also tracked). Verified: built on the development machine; not re-run
   from a clean checkout.
   `build/ffmpeg/build.sh`: FFmpeg 7.1.1 in an LGPL-only configuration (WMA,
   MPEG audio and PCM decoders; mp3/wav/mov demuxers; no H.264/HEVC/AAC),
   built from the tracked, unmodified release tarball in `build/ffmpeg/src`
   after verifying it against `build/ffmpeg/src/SHA256SUMS` -> headers in `toolchains/ffmpeg-ios/include`
   (read by `build/ntdll-unix/build.sh` for winegstreamer's unix side) and
   `app/Madeira/lib{avformat,avcodec,swresample,avutil}.a` (ignored; the app
   target links them together with VideoToolbox, CoreMedia, CoreVideo,
   AudioToolbox and CoreFoundation). The configure arguments are the ones the
   port was built and device-tested with on the WSL toolchain; the macOS form
   of the script is UNVERIFIED.
2. FEX (submodule, branch ios-port-2607):
   - `FEX/build-ios`: `build/fex-ios/build.sh` (same options as the development CMakeCache) -> `FEX/build-ios/FEXCore/Source/lib{FEXCore,FEXCore_Base,JemallocLibs}.a` and the `External/{cephes,fmt,SoftFloat-3e,xxhash}` archives. UNVERIFIED from clean.
   - `FEX/build-arm64ec`: `build/fex-arm64ec/build.sh` (configures with `FEX/Data/CMake/toolchain_mingw.cmake` and the recorded options on first run, builds target `arm64ecfex`, copies `Bin/libarm64ecfex.dll` to `app/Madeira/arm64ec-windows/xtajit64.dll`). The build step was verified this session; the first-run configure in the script is reconstructed from CMakeCache and UNVERIFIED.
3. Wine (submodule, branch madeira-lgpl):
   - unix side: `build/ntdll-unix/build.sh`, `build/wineserver/build.sh`,
     `build/win32u-unix/build.sh` -> `app/Madeira/lib{ntdll_unix,wineserver,win32u_unix}.a`. Verified on the development machine.
   - PE side: `build/wine-pe/build-ntdll.sh` (configures `wine/build-arm64ec` with `--enable-archs=arm64ec --without-x --disable-tests --enable-winegstreamer` on first run, builds `dlls/ntdll`, strips, pads to SizeOfImage + 0x50000, copies to the app). Other PE modules: `build/wine-pe/build-modules.sh <name>...` (same tree; it builds each module's DLL target `dlls/<name>/arm64ec-windows/<name>.dll`, strips it with `--strip-debug` like every shipped builtin and installs it into `app/Madeira/arm64ec-windows/`, or into `$DEST`). Building the DLL target rather than `make -C dlls/<name>` is also what winegstreamer needs (enabled by `--enable-winegstreamer` although GStreamer is absent, since its unix side is `build/ntdll-unix/winegstreamer_unixlib_ios.c`). Without arguments the script rebuilds the stock builtins added for games: `cryptsp`, `d3dx11_43`, `msvcp110`, `msvcr110` and `xaudio2_7` (committed in `app/Madeira/arm64ec-windows/` like every other builtin). It needs bison 3 for `tools/wrc` (macOS ships 2.3; Homebrew's is used when installed). The strip/pad step was verified this session; the configure step is UNVERIFIED from clean; build-modules.sh reproduced the five default DLLs at their shipped sizes on the development machine (2026-10-03).
   - `app/Madeira/arm64ec-windows/` is the DLL farm: every file in it is linked into the prefix (`system32` for x64 sessions, and `sysx64`), so a Wine module is only available if it was built and copied there. The native D3D12 path needs two stock modules in addition to the existing ones: `dcomp.dll` (`make -C dlls/dcomp`; a 64-bit Godot 4 engine loads it before it creates its D3D12 device, and gives up on D3D12 without it) and `ktmw32.dll` (`make -C dlls/ktmw32`; an optional import the same engine probes).
4. DXMT (submodule, branch ios-port):
   - unix side: `build/dxmt-ios/build.sh` (needs `toolchains/llvm-ios-build`) -> `app/Madeira/libdxmt_combined.a` (ignored; the app links it). Verified this session.
   - PE side: `meson setup dxmt/build-arm64ec dxmt -Dbuildtype=release -Dwine_build_path=../../wine/build-arm64ec --cross-file=dxmt/build-arm64ec-win.txt` then `ninja -C dxmt/build-arm64ec src/winemetal/winemetal.dll` (and d3d11.dll) -> copied to `app/Madeira/arm64ec-windows/`. Verified this session (winemetal.dll).
4b. In-app pairing (Built-in StikJIT on iOS 27): `build/rppairing-ios/build.sh`
   (Rust with the `aarch64-apple-ios` target; crates from crates.io at the
   versions in `build/rppairing-ios/Cargo.lock`) -> `app/Madeira/libmadeira_rppairing.a`
   (ignored; the app links it) and the bundled crate notices
   `app/Madeira/legal/LICENSES-rppairing-crates.txt` (tracked). `cargo test`
   in that folder runs its host tests. Verified on the development machine.
5. Native D3D12 runtime: `build/madeira-d3d12/build-pe.sh` -> `d3d12.dll`, `madeira_d3d12.dll` and the test executables in `app/Madeira/arm64ec-windows/` (tracked). Verified this session. `build/madeira-d3d12/fetch-converter.sh` re-verifies the converter library; `build/stage-licenses.sh` refreshes the bundled licence copies (the Xcode build fails if they are stale).
6. App: `xcodebuild -project app/Madeira.xcodeproj -scheme Madeira -destination 'generic/platform=iOS' -allowProvisioningUpdates build` (Debug is the configuration that runs the games; Release builds have crashed the guest), then zip `Payload/Madeira.app` into an IPA and sideload. Verified this session on the development machine.
7. WoW64 (32-bit programs, optional): `build/wine-i386/build.sh` (i386 Wine farm
   -> `app/Madeira/i386-windows/`), `build/fex-wow64/build.sh` (FEX WOW64 module
   -> `app/Madeira/aarch64-windows/xtajit.dll`) and the aarch64 `wow64.dll` /
   `wow64win.dll`; see docs/WOW64.md, "Building". UNVERIFIED on macOS.

## Status of the LGPL relink question

A recipient of a built package can obtain the complete corresponding
source of every LGPL library (Wine fork, GnuTLS, Nettle, GMP, FFmpeg) from the
repository, and the application source and build scripts above. Whether
they can actually relink depends on assembling the "not in the repository"
inputs and re-executing the UNVERIFIED steps; that end-to-end clean-machine
rebuild, signing and installation has NOT been performed. Until it is,
docs/LICENSING.md keeps the relink capability marked unverified. The
alternative the LGPL offers, shipping the application's object files, is
not currently done.

## Local build verification (2026-10-10)

A Debug build was compiled, signed and installed on an iPad Pro (M5) with
Xcode 26.5. This verifies the local build, not a clean-machine rebuild or
compatibility with every game. Native FEX (including JemallocLibs), LLVM,
FFmpeg, Wine Unix libraries, DXMT and pairing inputs were built locally.
The pinned llvm-mingw download and Microsoft x64 redistributable extraction
were also verified. Preserve the earlier UNVERIFIED labels for steps that
were not independently repeated from a clean machine.

Steam library UI requires the ignored Dock build outputs, even though its
owned-games refresh is native and does not start Wine. Before building:

```sh
bash build/madeira-dock/build.sh --check
bash build/stage-licenses.sh
python3 tools/check-app-bundle.py --source --require-steam app/Madeira
```

For 32-bit support, build `build/wine-i386/build.sh` and retain the aarch64
WoW64 modules. This local run staged 725 genuine PE32 Wine/DXMT modules.
MSI requires `msi.dll` and `msiexec.exe`; native DLL dependencies must also
be staged. Wine Mono is optional .NET Framework support: fetch it with
`bash build/wine-mono/fetch.sh --source` before a Debug build. It does not
provide modern CoreCLR. Release keeps its existing on-device Mono download.

After building, check the **built app**, not just the staged inputs:

```sh
python3 tools/check-app-bundle.py --require-steam --require-wow64 --require-mono --require-msi app/Build/Products/Debug-iphoneos/Madeira.app
codesign --verify --deep --strict app/Build/Products/Debug-iphoneos/Madeira.app
```

Select feature flags for the package being shipped. The checker validates PE
sections/import closure and native iOS images/dependencies; it does not verify
signing, JIT, downloads, rendering or gameplay. Missing selected features fail
instead of silently producing a reduced package.

On the local exFAT volume, direct PE linker outputs became corrupt after a
delay despite initially passing validation. Compile and strip on APFS, then
stage with ordinary copies and sibling temporary-file renames. The i386
script accepts absolute `MADEIRA_WINE_I386_BUILD_DIR` and
`MADEIRA_DXMT_I386_BUILD_DIR` overrides for this purpose. Validate staged
bytes again after synchronization. Do not enable experimental 64-bit media
merely because its PE modules are present.

The ARM64EC module script also accepts an absolute APFS build directory:

```sh
MADEIRA_WINE_PE_BUILD_DIR=/path/on/apfs/wine-build bash build/wine-pe/build-modules.sh xaudio2_8 xaudio2_9
```

Its default list includes genuine XAudio2 2.7, 2.8 and 2.9 providers; do not
rename an older provider to satisfy a newer import. The app checker requires
all three and the bundled FAudio zlib license. The selected modules are linked
and stripped on APFS before individual temporary copies are renamed into the
app inputs. Publication is atomic per file, not a transaction for the batch.

### Temporary storage for IPA checks (local workflow)

User requirement, 2026-10-11: ordinary IPA extraction and inspection use the
external disk, under
`/Volumes/untitled/github/Madeira/app/Build/Products/IPAInspection/`.
Create a separate task/build directory there for each run. ZIP inspection,
CRC, extracted resources, PE/import checks, hashes and inspection reports
belong there; explicitly set extraction destinations rather than letting a
temporary-directory default expand the IPA on the internal disk.

Steps that actually require APFS, including the PE linking/stripping above,
use an explicitly recorded, task-owned directory such as
`/private/tmp/madeira-apfs-<task>-<random>/` on the internal disk. Check the
underlying mounted filesystem before use; the external working volume is
exFAT and the internal Data volume is APFS in the current setup. Do not move
ordinary inspection to APFS without a concrete filesystem requirement.

When a required APFS step finishes, verify and copy its required outputs,
hashes and logs to the external task directory, stop/release its processes,
then promptly remove only that step's recorded temporary directory. If it
must remain for a running step or failed-run investigation, record the reason
and pending cleanup. Never delete by a broad `/tmp/madeira*` pattern or remove
shared build caches, existing IPAs, current build inputs or release evidence
as part of this cleanup. Preserve an IPA's sealed bytes during inspection;
removing sealed AppleDouble files requires a separate re-signing workflow.

Keep `$(SDKROOT)/usr/lib` first in the app's library search paths: an inherited
Homebrew `LIBRARY_PATH` otherwise selects a macOS liblzma during iOS linking.
Provision an explicit bundle identifier with the increased-memory-limit
entitlement and authorize the actual destination device before installation.

### CoreCLR memory configuration on iPad

The global `Documents/madeira.cfg` can export .NET runtime settings to every
launch (a game's own config may override them):

```ini
env.DOTNET_GCRegionRange = 0x400000000
env.DOTNET_GCHeapHardLimit = 0x80000000
```

These hexadecimal values reserve up to 16 GiB of virtual address space for
GC regions and cap the managed GC heap at 2 GiB. They do not allocate 16 GiB
of physical RAM or limit the entire Wine process to 2 GiB: native allocations,
JIT code, graphics and other heaps are separate. Microsoft documents the
region reservation default of 256 GiB and the distinct heap hard limit at
https://learn.microsoft.com/en-us/dotnet/core/runtime-config/garbage-collector .
The local device probe confirmed a 16 GiB reservation succeeds where the
default failed. Gameplay is still unverified: a later CoreCLR FailFast remains
under investigation. These values are an explicit device configuration,
not a universally validated app default or a fix for every runtime failure.

### Pure x64 images and the executable pool

Ordinary AMD64 images are decoded by FEX at their original PE addresses.
The native protection path now treats those bytes as host data, while retaining
Wine executable protection metadata and FEX self-modifying-code notifications.
ARM64 and CHPE/ARM64EC images retain their original executable pool path.
This prevents a large x64 image from consuming its whole SizeOfImage in the
native executable pool. Live exported DATA imports from native modules are
still bound to their native pool storage, including unpooled x64 importers.
`env.MADEIRA_X64_IMAGE_HOST_DATA = 0` restores the previous image-pool path
for comparison. This switch does not change anonymous JIT memory handling.

`python3 tests/host/check-x64-image-host-data.py` exercises the production
classifier and IAT binder using Wine PE layouts under ASan/UBSan, including
host-page tail bounds and native/hybrid/malformed cases. Native compilation
also passed. Device compatibility remains subject to the recorded runtime test.

### Default keyed events in child processes

Wine's Unix libraries share one Mach process in this port, but each emulated
Windows process has its own server handle table. The upstream global default
keyed-event handle belongs to the initial process. Reusing it in a child can
make a RunOnce wait fail immediately; its stack waiter node can then be
republished and corrupt the completion list.

The iOS override resolves NULL keyed-event handles using the current PEB and
server process ID. It registers the parent's original object and creates one
child object before child PE execution; explicit handles pass through unchanged.
Unknown identities and creation failures propagate errors. Socket teardown
clears the state, while server EOF releases that process's handles.

`tests/x64/runonce-stress.c` is a generic Win32 callback/contention diagnostic,
with bounded waits and a watchdog. On the local device, the pre-fix executable
passed 32 rounds with 8 workers when started directly, but failed as an Explorer
child in the same RunOnce completion instruction. The sanitized host regression
is `python3 tests/host/check-default-keyed-event.py`. Record the after-fix device
result separately; the host test alone does not establish successful gameplay.

The after-fix device run of the identical Explorer-child diagnostic completed
with Windows exit status 0 after the 32 rounds. The original access violation
was absent. This establishes the child callback/keyed-event regression result;
it is separate from the subsequent game/rendering validation.

### ARM64EC faultrep provider (2026-10-14)

The default PE rebuild list and source/device package checks now require
`arm64ec-windows/faultrep.dll`. An i386-only provider does not satisfy an x64
game import. To rebuild just this stock Wine module, use `JOBS=1`, a task-owned
APFS `MADEIRA_WINE_PE_BUILD_DIR`, APFS `TMPDIR`, and
`bash build/wine-pe/build-modules.sh faultrep`. The repository's
`toolchains/host-tools/bison/3.8.2/bin` can be prepended to command-local PATH
when the system Bison is too old. Preserve unrelated existing build trees.

App46's runtime repair batch also requires native
`aarch64-windows/services.exe` and `aarch64-windows/wineboot.exe` (PE machine
0xaa64). Source and device package checks reject missing, empty, malformed or
wrong-architecture helpers. These package checks do not prove installer execution
or guest shutdown on iPad.

App47 also requires `cjk-fonts/NotoSansCJKsc-Regular.otf` and its `LICENSE.txt`.
The static Noto Sans CJK SC2.004 asset is16,437,364bytes with SHA-256
`2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`.
It is copied with bounded streaming before the server opens the registry;
no Wine/FEX rebuild is needed. `tests/host/check-runtime-fonts.py` executes the
actual Foundation migration helper against isolated prefix fixtures.

App48 adds D3D12 dimension/cardinality checks before base/extended allocation
queries and committed/placed resource creation. Invalid descriptions return an
error before a Metal call; rejection diagnostics are capped. The focused check is
`python3 tests/host/check-d3d12-resource-dimensions.py`. This is not complete
resource-format or device-limit validation, and SOTTR recovery requires device
evidence.

For a single custom D3D12 PE rebuild, the existing winemetal DLL's exports can
generate a task-local ARM64EC import library with llvm-dlltool; a full Wine/FEX
or native DXMT rebuild is unnecessary. Link PE output on internal APFS and copy
it to the external workspace with buffered writes/fsync and SHA-256 readback:
the direct external link output in this task became zero-filled despite linker
success. Validate actual exports/imports, CHPE identity and bundle hashes before
installation. Evidence: `issue/epic-retry-20261015/sottr-zero-resource/`.

The App45 candidate's inspected DLL has an x64-compatible raw PE machine
0x8664, is recognized as ARM64EC by LLVM, and exports `ReportFault`. Package
checks establish presence/architecture; device loading and game compatibility
still require the user's launch. See the
[log/throughput report](../issue/epic-retry-20261014/game-logs-epic-perf/REPORT.md).

### SDL3 compatibility asset

`arm64ec-windows/madeira-steam-components/SDL3.dll` is built independently from
unmodified official SDL source commit `b1362f8a427b32e01df19b48c8dd4b6599d6cc6d`.
Use the pinned source archive/hash in
`issue/epic-retry-20261015/steam-sdl3-preload/component.json`; configure CMake for
Windows with the repository LLVM/MinGW x86_64 C/C++/windres tools, Release,
SDL_SHARED=ON, SDL_STATIC=OFF, SDL_TEST_LIBRARY=OFF, SDL_TESTS=OFF,
SDL_EXAMPLES=OFF and SDL_REVISION=SDL-release-3.4.0-98-gb1362f8a4. Build target
SDL3-shared with `--parallel 1`, then strip on internal APFS. Repin the checked
artifact hash/revision in `madeira-dock/src/sdl_component_pin.h` before rebuilding
Dock; bundle its SDL/HIDAPI/LLVM/MinGW notices. Never replace the user's root
32-bit SDL file. Verify the host/DLL hashes again after resource copying.
The component's actual on-device initialization is a separate validation gate.
