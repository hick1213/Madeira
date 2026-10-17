#!/usr/bin/env python3
"""Stage the reviewed, isolated x64 LuaJIT candidate. Never install or execute it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "issue/multi-fixes-20261012/kingdom"
EVIDENCE = ROOT / "app/Build/Products/IPAInspection/lua-compat-prototype"
GAME = ROOT / "app/Build/Products/CrashInvestigations/2026-10-12-kingdom-recurrence"
BUILD = Path(os.environ.get("MADEIRA_LUAJIT_BUILD_DIR", str(ROOT / ".prototype-build/windows-dynamic")))
READOBJ = ROOT / "toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin/llvm-readobj"
PINS = {
    "lua51.dll": "240d6608ae122f697f3c077a43f384abd6fa0c48a4734518fcf32fb78d1b9e93",
    "probe.exe": "273a6d861d848423c5c34b5b83db5f0f4934e433204e39d3be3d17a8acbf1255",
    "corpus.lua": "2e6a0de72970538055f01fe1544adaa89ba24e0f5c7b461aa0041263c83e2f5e",
    "corpus-v1.ljbc": "6a2b3ebd100373670e25f37e0e9885c06a76353973046f03a6692d9989ad0b6f",
}
DERIVED = (
    "src/Makefile", "src/lj_compat_arena.c", "src/lj_compat_arena.h",
    "src/lj_obj.h", "src/lj_alloc.c", "src/lj_state.c", "src/lj_gc.c",
    "src/lj_err.c", "src/lj_ccallback.c", "src/lj_parse.c", "src/vm_x86.dasc",
    "src/host/buildvm.h", "src/host/buildvm_asm.c", "dynasm/dasm_x86.lua",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pe_report(path: Path, scratch: Path, flag: str) -> str:
    result = subprocess.run([str(READOBJ), flag, str(path)], cwd=scratch,
                            capture_output=True, text=True, timeout=60, check=True)
    if "Format: COFF-x86-64\n" not in result.stdout:
        raise ValueError(f"not a Windows x64 PE: {path}")
    return result.stdout


def exports(report: str) -> set[str]:
    blocks = re.findall(r"Export \{\n(.*?)\n\}", report, re.S)
    names = [re.search(r"^  Name: (.+)$", block, re.M) for block in blocks]
    if not blocks or any(name is None for name in names):
        raise ValueError("missing or unnamed PE export")
    result = {name.group(1) for name in names if name is not None}
    if len(result) != len(blocks):
        raise ValueError("duplicate PE export")
    return result


def prepare(build: Path) -> tuple[dict[str, bytes], dict]:
    payloads = {"lua51.dll": (build / "lua51.dll").read_bytes(),
                "probe.exe": (build / "probe.exe").read_bytes(),
                "corpus.lua": (ROOT / "tests/legacy-luajit/corpus.lua").read_bytes(),
                "corpus-v1.ljbc": (EVIDENCE / "root-final-host-3/corpus-v1.ljbc").read_bytes()}
    for name, expected in PINS.items():
        if digest(payloads[name]) != expected:
            raise ValueError(f"pinned hash mismatch: {name}")
    if payloads["corpus-v1.ljbc"][:4] != b"\x1bLJ\x01":
        raise ValueError("owned fixture is not bytecode v1")

    frozen = json.loads((EVIDENCE / "exported-source-sha256.json").read_text())
    source_hashes = {}
    for relative in DERIVED:
        key = "experiments/legacy-luajit/runtime/" + relative
        current = (ROOT / key).read_bytes()
        if digest(current) != frozen[key] or (build / "source" / relative).read_bytes() != current:
            raise ValueError(f"reviewed runtime source mismatch: {relative}")
        source_hashes[relative] = digest(current)

    prior_reference = frozen["experiments/legacy-luajit/reference-build.sh"]
    current_reference = digest((ROOT / "experiments/legacy-luajit/reference-build.sh").read_bytes())
    # Use system temporary storage for PE processing rather than the source volume.
    with tempfile.TemporaryDirectory(prefix="kingdom-candidate-") as folder:
        scratch = Path(folder)
        (scratch / "lua51.dll").write_bytes(payloads["lua51.dll"])
        (scratch / "probe.exe").write_bytes(payloads["probe.exe"])
        input_hashes = {}
        for name, source in (("original-lua51.dll", GAME / "lua51.dll"),
                             ("original-game.exe", GAME / "Kingdom Rush Frontiers.exe")):
            with source.open("rb") as src, (scratch / name).open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            with (scratch / name).open("rb") as stream:
                value = hashlib.file_digest(stream, "sha256").hexdigest()
            input_hashes[name] = {"source": str(source), "sha256": value}
        reports = {
            "candidate-exports.txt": pe_report(scratch / "lua51.dll", scratch, "--coff-exports"),
            "original-exports.txt": pe_report(scratch / "original-lua51.dll", scratch, "--coff-exports"),
            "game-imports.txt": pe_report(scratch / "original-game.exe", scratch, "--coff-imports"),
            "probe-imports.txt": pe_report(scratch / "probe.exe", scratch, "--coff-imports"),
        }
    candidate, original = exports(reports["candidate-exports.txt"]), exports(reports["original-exports.txt"])
    if len(original) != 135 or len(candidate) != 135 or candidate != original:
        raise ValueError("candidate does not match the original 135 public exports")
    blocks = re.findall(r"Import \{\n(.*?)\n\}", reports["game-imports.txt"], re.S)
    lua = [block for block in blocks if re.search(r"^  Name: lua51\.dll$", block, re.M | re.I)]
    if len(lua) != 1:
        raise ValueError("expected one lua51.dll import table")
    required = re.findall(r"^  Symbol: (\S+) \(\d+\)$", lua[0], re.M)
    if len(required) != 16 or len(set(required)) != 16 or not set(required) <= candidate:
        raise ValueError("actual game's 16 named Lua imports are not satisfied")
    if not re.search(r"^  Name: lua51\.dll$", reports["probe-imports.txt"], re.M | re.I):
        raise ValueError("probe is not dynamically linked to lua51.dll")
    payloads.update({name: value.encode() for name, value in reports.items()})
    for name, source in {
        "COPYRIGHT": ROOT / "experiments/legacy-luajit/upstream/COPYRIGHT",
        "upstream.json": EVIDENCE / "upstream.json",
        "host-results.json": EVIDENCE / "root-final-host-3/results.json",
        "host-security-review.json": EVIDENCE / "security-runtime/luasecurity-runtime-sb0j1zco/final-review/review-results.json",
        "exported-source-sha256.json": EVIDENCE / "exported-source-sha256.json",
    }.items():
        payloads[name] = source.read_bytes()
    manifest = {
        "status": "isolated candidate; not installed or executed by this staging operation",
        "architecture": "Windows x64 PE; requires Madeira/FEX, not an ARM64EC Lua VM",
        "lua_trace_jit_enabled": False, "windows_executed": False,
        "device_executed": False, "game_executed": False,
        "game_scripts_prior_check": "575 bytecode-v1 chunks loaded only; never executed",
        "pe_checks": {"original_exports": len(original), "candidate_exports": len(candidate),
                      "required_game_imports": sorted(required), "missing_names": []},
        "source_build": str(build), "reviewed_runtime_source_sha256": source_hashes,
        "reference_build_script": {"frozen_sha256": prior_reference, "current_sha256": current_reference,
                                   "drift": prior_reference != current_reference,
                                   "note": "Reference script drift does not rebuild or validate a new binary; this candidate pins the final reviewed binary and runtime source."},
        "inspection_inputs": input_hashes,
        "limitations": ["Windows/Wine/FEX and physical device gates remain unexecuted.",
                        "Game startup, graphics, controls, saves, exit and performance remain unverified.",
                        "Shared arena has 2GiB virtual capacity; physical availability is unproven."],
    }
    return payloads, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path,
                        help="new direct child of issue/multi-fixes-20261012/kingdom")
    parser.add_argument("--build-dir", type=Path, default=BUILD,
                        help="existing final reviewed dynamic build; defaults to MADEIRA_LUAJIT_BUILD_DIR or .prototype-build/windows-dynamic; hashes must match")
    args = parser.parse_args()
    destination = args.destination.absolute()
    try:
        if destination.parent != OUTPUT or destination.name in ("", ".", ".."):
            raise ValueError(f"destination must be a new direct child of {OUTPUT}")
        if os.path.lexists(destination):
            raise ValueError("destination already exists; nothing overwritten")
        if OUTPUT.resolve() != OUTPUT:
            raise ValueError("output root must not contain symlinks")
        payloads, manifest = prepare(args.build_dir.resolve())
        payloads["README.txt"] = (
            "Isolated LuaJIT 2.0.4 x64 candidate. No game files or saves were replaced.\n"
            "Lua trace JIT is disabled; Madeira/FEX JIT remains required.\n"
            "Host semantic tests passed earlier; game chunks were LOAD-ONLY.\n"
            "Windows/FEX/iPad/gameplay and performance are untested.\n\n"
            "Next gate in a confirmed idle Madeira session, from this directory:\n"
            "probe.exe --expect-high --corpus corpus.lua\n"
            "probe.exe --expect-high --corpus corpus-v1.ljbc\n"
            "probe.exe --expect-high --stress\n"
            "Require exit 0 plus API_PASS checks=61 / STRESS_PASS threads=4 states=48.\n"
            "Do not select this DLL for an actual game until the device probe gates pass.\n"
        ).encode()
        manifest["payloads"] = {name: {"bytes": len(data), "sha256": digest(data)}
                                for name, data in payloads.items()}
        payloads["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        # Exclusive directory creation and exclusive files: a competing attempt
        # cannot overwrite an existing candidate. Never remove a partial result.
        parent = os.open(OUTPUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.mkdir(destination.name, dir_fd=parent)
            fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for name, data in payloads.items():
                    child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
                    with os.fdopen(child, "wb") as stream:
                        stream.write(data)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        print(f"STAGED {destination}; no runtime or game executed")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"STAGING_REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
