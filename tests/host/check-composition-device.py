#!/usr/bin/env python3
"""Run the real DirectComposition device C implementation on the host.

The first assertion catches the shipped Wine stub returning E_NOTIMPL instead
of creating a Device1. Windows locks, window validity and the external Metal
surface are the only host adapters; the COM state and lifetime are production.
"""

from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError(
        "Run without Python optimization; contract assertions are required."
    )

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "madeira-d3d12/src/pe/dcomp.c"
source = (
    PRODUCTION.read_text()
    if PRODUCTION.exists()
    else (ROOT / "Wine/dlls/dcomp/device.c")
    .read_text()
    .replace(
        '#include "wine/debug.h"',
        "#define FIXME(...) ((void)0)\n#define WINE_DEFAULT_DEBUG_CHANNEL(name)",
    )
)
code = (
    r"""
#define INITGUID
#define COBJMACROS
#define CONST_VTABLE
#define COM_NO_WINDOWS_H
#define WIN32_LEAN_AND_MEAN
#include <stdarg.h>
#include <assert.h>
#include <stdio.h>
#include <windef.h>
#include <winbase.h>
#include <wingdi.h>
#include <winuser.h>
#include <rpc.h>
#include <rpcndr.h>
#include <objbase.h>
#include <dcomp.h>
"""
    + source
    + (ROOT / "tests/host/composition-device-contract.c").read_text()
)
with tempfile.TemporaryDirectory(prefix="madeira-composition-device-") as td:
    c = Path(td) / "check.c"
    binary = Path(td) / "check"
    c.write_text(code)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            "-Wno-pragma-pack",
            "-Wno-ignored-attributes",
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all",
            "-I",
            str(ROOT / "Wine/build-macos/include"),
            "-I",
            str(ROOT / "Wine/include"),
            "-I",
            str(ROOT / "madeira-d3d12/src/pe"),
            str(c),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=20)
