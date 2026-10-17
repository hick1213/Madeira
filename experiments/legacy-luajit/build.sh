#!/usr/bin/env bash
# Isolated experimental runtime only. Never installs or replaces a game DLL.
set -euo pipefail
prototype_dir=$(cd "$(dirname "$0")" && pwd)
workspace_dir=$(cd "$prototype_dir/../.." && pwd)
target=${1:-macos}
case "$target" in macos|windows-static|windows-dynamic) ;; *) echo 'usage: build.sh [macos|windows-static|windows-dynamic]' >&2; exit 2 ;; esac
case "$workspace_dir" in /Users/*) ;; *) echo 'Run the prototype build in its approved internal APFS worktree.' >&2; exit 2 ;; esac
mkdir -p "$workspace_dir/.prototype-build"
build_dir=$(mktemp -d "$workspace_dir/.prototype-build/$target.XXXXXX")
cp -R "$prototype_dir/upstream" "$build_dir/source"
# Explicit derived files: generated headers/objects from another target are
# never copied. Fresh generation also covers the DynASM dependency upstream
# does not declare in its Makefile.
derived=(src/Makefile src/lj_compat_arena.c src/lj_compat_arena.h src/lj_obj.h
  src/lj_alloc.c src/lj_state.c src/lj_gc.c src/lj_err.c src/lj_ccallback.c src/lj_parse.c
  src/vm_x86.dasc src/host/buildvm.h src/host/buildvm_asm.c dynasm/dasm_x86.lua)
for relative in "${derived[@]}"; do cp "$prototype_dir/runtime/$relative" "$build_dir/source/$relative"; done
src="$build_dir/source/src"
# Old LuaJIT's integer-narrowing probe needs hardware overflow semantics.
# Modern Clang may otherwise optimize an out-of-range float cast and corrupt
# dumped constants. This option applies to the pristine baseline as well.
flags='-DLUAJIT_DISABLE_JIT -fno-strict-float-cast-overflow'
if [[ "$target" == macos ]]; then
  make -C "$src" -j6 BUILDMODE=static CC='clang -arch x86_64' \
    HOST_CC='clang -arch arm64' TARGET_SYS=Darwin MACOSX_DEPLOYMENT_TARGET=11.0 \
    XCFLAGS="$flags" TARGET_LDFLAGS='-Wl,-no_deduplicate,-no_pie'
  # VM bytecode dispatch offsets require contiguous code; linker deduplication
  # would invalidate them even in the unmodified reference runtime.
  clang -arch x86_64 -mmacosx-version-min=11.0 -O2 -Wall -Wextra -Werror \
    -pthread -I"$src" "$workspace_dir/tests/legacy-luajit/probe.c" \
    "$src/libluajit.a" -Wl,-no_deduplicate -o "$build_dir/probe"
else
  toolchain=${LUA_COMPAT_TOOLCHAIN:-/Volumes/untitled/github/Madeira/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}
  cc="$toolchain/x86_64-w64-mingw32-clang"
  [[ -x "$cc" ]] || { echo "Missing compiler: $cc" >&2; exit 2; }
  mode=static; [[ "$target" != windows-dynamic ]] || mode=dynamic
  make -C "$src" -j6 BUILDMODE="$mode" CC="$cc" HOST_CC='clang -arch arm64' \
    HOST_CFLAGS=-Wno-unused-command-line-argument TARGET_SYS=Windows XCFLAGS="$flags" \
    TARGET_AR="$toolchain/x86_64-w64-mingw32-ar rcus" \
    TARGET_STRIP="$toolchain/x86_64-w64-mingw32-strip"
  if [[ "$mode" == static ]]; then
    "$cc" -O2 -Wall -Wextra -Werror -I"$src" \
      "$workspace_dir/tests/legacy-luajit/probe.c" "$src/libluajit.a" -o "$build_dir/probe.exe"
  else
    cp "$src/lua51.dll" "$build_dir/lua51.dll"
    "$cc" -O2 -Wall -Wextra -Werror -I"$src" \
      "$workspace_dir/tests/legacy-luajit/probe.c" -L"$src" -llua51 -o "$build_dir/probe.exe"
  fi
fi
printf 'PROTOTYPE_BUILD_DIR=%s\n' "$build_dir"
