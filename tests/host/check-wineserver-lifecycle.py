#!/usr/bin/env python3
"""Run actual WineServerBridge pthread lifecycle methods with an OS/server harness."""

from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError(
        "Run without Python optimization; lifecycle assertions are required."
    )

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "app/Madeira/WineServerBridge.m").read_text()


def function(signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


marker = "/* IOS_WINESERVER_LIFECYCLE_BEGIN */"
end_marker = "/* IOS_WINESERVER_LIFECYCLE_END */"
if marker in source or end_marker in source:
    if source.count(marker) != 1 or source.count(end_marker) != 1:
        raise RuntimeError("Expected exactly one complete production lifecycle block.")
    start = source.index(marker) + len(marker)
    end = source.index(end_marker)
    if start >= end:
        raise RuntimeError("Production lifecycle markers are out of order.")
    lifecycle = source[start:end]
else:
    # Baseline: the actual old start/stop/getters. The Objective-C logging/NLS
    # setup is external to these methods; its body normal-return assignment is
    # reproduced here until the production cleanup wrapper exists.
    globals_start = source.index("static pthread_t g_wineserver_thread;")
    globals_end = source.index("static void *wineserver_thread_func", globals_start)
    lifecycle = (
        source[globals_start:globals_end]
        + r"""
static void *wineserver_thread_func(void *arg) {
    wineserver_thread_body(arg);
    g_wineserver_running = 0;
    return NULL;
}
"""
        + "\n".join(
            function(sig)
            for sig in (
                "int wineserver_start(",
                "int wineserver_is_running(",
                "int wineserver_is_ready(",
                "void wineserver_stop(",
            )
        )
    )

fixture = (ROOT / "tests/host/wineserver-lifecycle-contract.c").read_text()
prefix, suffix = fixture.split("/* PRODUCTION_LIFECYCLE */", 1)
with tempfile.TemporaryDirectory(prefix="madeira-server-lifecycle-") as td:
    c = Path(td) / "check.c"
    binary = Path(td) / "check"
    c.write_text(prefix + lifecycle + suffix)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            "-Wno-unused-function",
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all",
            str(c),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=25)
