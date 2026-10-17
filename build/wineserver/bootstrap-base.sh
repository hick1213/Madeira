#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 125hz
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
# Sourced by build.sh after its compiler flags and maintained iOS replacements
# are defined. Build the remaining real upstream server sources from the
# source inventory; the caller then installs its replacements and renames every
# archive member. No prebuilt base archive is needed for a fresh checkout.
# Prefer an APFS workspace/generated build directories: the prerequisite
# configure/libtool chains use Unix filesystem features absent on exFAT.

bootstrap_wineserver_base() {
    local src name entry replaced old_obj archive
    local objects=()
    echo "=== Bootstrapping wineserver base from source ==="
    for src in $(sed -n '/^SOURCES =/,/^$/p' "$WINE_SRC/server/Makefile.in" | awk '$1 ~ /\.c$/ { print $1 }'); do
        name="${src%.c}"
        replaced=0
        for entry in "${PATCHED_FILES[@]}"; do
            old_obj="${entry##*:}"
            [ "$old_obj" != "$name.o" ] || replaced=1
        done
        [ "$replaced" = 0 ] || continue
        compile_one "$WINE_SRC/server/$src" "$name"
        objects+=("$OBJ_DIR/$name.o")
    done
    [ "${#objects[@]}" -gt 0 ] || { echo "ERROR: wineserver source inventory is empty" >&2; return 1; }
    archive="$OBJ_DIR/libwineserver.base.$$.a"
    ar rcs "$archive" "${objects[@]}"
    # Publish only after all replacements and the symbol sweep succeed. A
    # failed first build must not leave a partial base eligible for reuse.
    WORK_ARCHIVE="$archive"
}
