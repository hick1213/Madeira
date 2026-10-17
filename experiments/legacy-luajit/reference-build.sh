#!/usr/bin/env bash
# Build pristine pinned sources; generated files stay on the approved APFS tree.
set -euo pipefail
prototype_dir=$(cd "$(dirname "$0")" && pwd)
workspace_dir=$(cd "$prototype_dir/../.." && pwd)
reference_dir="$prototype_dir/reference"
toolchain_dir=/Volumes/untitled/github/Madeira/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin
case "$workspace_dir" in /Users/*) ;; *) echo 'Run reference builds in the approved internal APFS worktree.' >&2; exit 2 ;; esac
if [[ ! -d "$reference_dir" ]]; then cp -R "$prototype_dir/upstream" "$reference_dir"; fi
case "${1:-macos}" in
  macos)
    make -C "$reference_dir/src" clean CC='clang -arch x86_64' \
      HOST_CC='clang -arch arm64' TARGET_SYS=Darwin
    make -C "$reference_dir/src" -j6 Q= BUILDMODE=static \
      CC='clang -arch x86_64' HOST_CC='clang -arch arm64' TARGET_SYS=Darwin \
      MACOSX_DEPLOYMENT_TARGET=11.0 XCFLAGS='-DLUAJIT_DISABLE_JIT -fno-strict-float-cast-overflow' \
      TARGET_LDFLAGS='-Wl,-no_deduplicate,-no_pie'
    clang -arch x86_64 -mmacosx-version-min=11.0 -O2 -Wall -Wextra \
      -I"$reference_dir/src" "$workspace_dir/tests/legacy-luajit/probe-baseline.c" \
      "$reference_dir/src/libluajit.a" \
      -Wl,-no_deduplicate,-no_pie,-pagezero_size,0x10000,-image_base,0x100000000 \
      -o "$reference_dir/src/probe-baseline"
    ;;
  windows)
    windows_dir="$reference_dir/windows-build"
    if [[ ! -d "$windows_dir" ]]; then
      mkdir "$windows_dir"
      cp -R "$prototype_dir/upstream/." "$windows_dir/"
    fi
    make -C "$windows_dir/src" clean CC="$toolchain_dir/x86_64-w64-mingw32-clang" \
      HOST_CC='clang -arch arm64' TARGET_SYS=Windows
    make -C "$windows_dir/src" -j6 Q= BUILDMODE=static \
      CC="$toolchain_dir/x86_64-w64-mingw32-clang" \
      HOST_CC='clang -arch arm64' HOST_CFLAGS=-Wno-unused-command-line-argument \
      TARGET_SYS=Windows XCFLAGS='-DLUAJIT_DISABLE_JIT -fno-strict-float-cast-overflow' \
      TARGET_AR="$toolchain_dir/x86_64-w64-mingw32-ar rcus" \
      TARGET_STRIP="$toolchain_dir/x86_64-w64-mingw32-strip"
    "$toolchain_dir/x86_64-w64-mingw32-clang" -O2 -Wall -Wextra \
      -I"$windows_dir/src" "$workspace_dir/tests/legacy-luajit/probe-baseline.c" \
      "$windows_dir/src/libluajit.a" -o "$windows_dir/src/probe-baseline.exe"
    ;;
  *) echo 'usage: reference-build.sh [macos|windows]' >&2; exit 2 ;;
esac
