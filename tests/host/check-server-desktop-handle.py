#!/usr/bin/env python3
"""Run Wine's handle validation/classification against controlled shared entries.

Removing the iOS server-owned desktop fallback must fail the desktop cases.
Broadening it beyond valid desktop/message entries must fail the invalid-handle
and ordinary-window cases. The harness uses Wine's real headers and production
functions; only TEB access and the user lock are replaced for the host process.
"""
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "wine/dlls/win32u/window.c"
CONTRACT = ROOT / "tests/host/server-desktop-handle-contract.c"


def function(source: str, signature: str) -> str:
    """Extract an entire file-scope production function without rewriting it."""
    start = source.index(signature)
    end = source.index("\n}", start) + 2
    return source[start:end] + "\n"


def main() -> None:
    source = SOURCE.read_text()
    declarations = "\n".join(
        line for line in source.splitlines()
        if line.startswith(("#define USER_HANDLE_TO_INDEX", "#define USER_HANDLE_FROM_INDEX",
                            "static void *client_objects"))
    )
    if len(declarations.splitlines()) != 3:
        raise RuntimeError("Production handle declarations were not found exactly once.")
    production = declarations + "\n" + "\n".join(
        function(source, signature)
        for signature in (
            "static BOOL is_valid_entry_uniq(",
            "static BOOL read_acquire_user_entry(",
            "static BOOL get_user_entry(",
            "void *get_user_handle_ptr(",
            "BOOL is_desktop_window(",
            "WND *get_win_ptr(",
        )
    )
    with tempfile.TemporaryDirectory(prefix="madeira-server-desktop-handle-") as directory:
        folder = Path(directory)
        (folder / "production-window.inc").write_text(production)
        compiler = os.environ.get("CC", "cc")
        for ios in (False, True):
            executable = folder / ("check-ios" if ios else "check-other")
            command = [
                compiler, "-std=gnu11", "-DWINE_UNIX_LIB", "-D__WINESRC__",
                "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-Wno-sign-compare", "-g",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(folder),
                "-I", str(ROOT / "wine/build-macos/include"),
                "-I", str(ROOT / "wine/include"),
                "-I", str(ROOT / "wine/dlls/win32u"),
                str(CONTRACT), "-o", str(executable),
            ]
            if ios:
                command.insert(1, "-DWINE_IOS=1")
            subprocess.run(command, check=True)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            if "PASS: server-owned desktop handle contract" not in result.stdout:
                raise RuntimeError("Native contract did not report completion.")
            print(result.stdout.strip())


if __name__ == "__main__":
    main()
