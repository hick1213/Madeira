#!/bin/bash
# Build the limited Device1 composition bridge. Work files and stripping stay
# on the host temporary volume; callers explicitly choose where to publish.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
MINGW="${MINGW:-$REPO_ROOT/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
SRC="$REPO_ROOT/madeira-d3d12/src/pe"
OUT="${OUT:-$DIR/out-pe}"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/madeira-dcomp-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/include" "$OUT"

# MinGW's dcomp.h contains C++ overloads. Generate the C ABI from the repository's
# real Windows IDL with the pinned toolchain instead of maintaining a new ABI.
for HEADER in dcomptypes dcomp; do
    "$MINGW/arm64ec-w64-mingw32-widl" --win64 -h \
        -H "$STAGE/include/$HEADER.h" -I "$REPO_ROOT/wine/include" \
        "$REPO_ROOT/wine/include/$HEADER.idl"
done
"$MINGW/arm64ec-w64-mingw32-clang" -shared -std=c11 -O2 -Wall -Wextra -Werror \
    -ffunction-sections -fdata-sections -Wl,--gc-sections \
    -I "$STAGE/include" -I "$SRC" \
    -o "$STAGE/dcomp.dll" "$SRC/dcomp.c" "$SRC/dcomp.def" \
    -luser32 -lkernel32 -luuid
"$MINGW/llvm-strip" --strip-all "$STAGE/dcomp.dll"
"$MINGW/llvm-readobj" --file-headers --coff-imports --coff-exports "$STAGE/dcomp.dll" \
    > "$STAGE/dcomp-report.txt"
cp "$STAGE/dcomp.dll" "$OUT/dcomp.dll.tmp"
mv "$OUT/dcomp.dll.tmp" "$OUT/dcomp.dll"
cp "$STAGE/dcomp-report.txt" "$OUT/dcomp-report.txt"
echo "Built ARM64EC dcomp.dll in $OUT (not installed)."
