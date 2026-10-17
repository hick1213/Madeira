#!/usr/bin/env python3
"""Run the actual bridge/owner TLS cleanup in an isolated Darwin ARM64 process."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def extract(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Actual TPIDRRO_EL0 canary requires Darwin ARM64")
    bridge = (ROOT / "app/Madeira/WineProcessBridge.m").read_text()
    signal = (ROOT / "build/ntdll-unix/signal_arm64_ios.c").read_text()
    helper = extract(bridge, "static void wine_process_clear_foreign_teb_slot(")
    owner = (
        extract(signal, "void ios_clear_thread_teb_mirror(")
        if "void ios_clear_thread_teb_mirror(" in signal
        else ""
    )
    init_start = signal.index("/* Create TLS key for TEB storage")
    initialization = extract(signal[init_start:], "if (!ios_teb_tls_key_created)")
    fixture = (Path(__file__).with_name("wine-teb-cleanup-contract.c")).read_text()
    code = fixture.replace("/* PRODUCTION_CLEANUP */", owner + "\n" + helper)
    code = code.replace("/* PRODUCTION_INITIALIZATION */", initialization)
    result = {"bridge_sha256": hashlib.sha256(bridge.encode()).hexdigest(),
              "signal_sha256": hashlib.sha256(signal.encode()).hexdigest()}
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
        (args.evidence / "actual-helper.c").write_text(owner + "\n" + helper)
        (args.evidence / "contract.c").write_text(code)
    with tempfile.TemporaryDirectory(prefix="madeira-teb-cleanup-") as td:
        source = Path(td) / "contract.c"
        binary = Path(td) / "contract"
        source.write_text(code)
        for name, flags in [("sanitizers", ["-fsanitize=address,undefined",
                                            "-fno-sanitize-recover=all"]),
                            ("optimized", ["-O2"])]:
            command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                       *flags, str(source), "-pthread", "-o", str(binary)]
            compile_run = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if compile_run.returncode:
                raise RuntimeError(compile_run.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            result[name] = {"compile_command": command,
                            "exit_code": run.returncode,
                            "stdout": run.stdout, "stderr": run.stderr}
            print(f"{name}: exit {run.returncode}\n{run.stdout}{run.stderr}", end="")
    if args.evidence:
        (args.evidence / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return int(any(result[name]["exit_code"] for name in ("sanitizers", "optimized")))


if __name__ == "__main__":
    raise SystemExit(main())
