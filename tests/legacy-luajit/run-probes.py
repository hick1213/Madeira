"""Run actual reference/prototype executables; game bytecode is load-only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile
import zlib


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--prototype", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--game", type=Path)
    args = parser.parse_args()
    # A fresh report prevents accidental reuse of a previous successful result.
    args.report.mkdir(parents=True, exist_ok=False)
    results: list[dict] = []

    def run(name: str, command: list[str], marker: str, timeout: int = 120) -> bool:
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            stdout, stderr = completed.stdout, completed.stderr
            exit_code = completed.returncode
            error = None
        except (subprocess.TimeoutExpired, OSError) as exc:
            stdout = getattr(exc, "stdout", None) or b""
            stderr = getattr(exc, "stderr", None) or b""
            exit_code = None
            error = str(exc)
        (args.report / f"{name}.stdout.log").write_bytes(stdout)
        (args.report / f"{name}.stderr.log").write_bytes(stderr)
        token = marker.encode()
        passed = exit_code == 0 and any(
            line == token or line.startswith(token + b" ") for line in stdout.splitlines()
        )
        results.append({"name": name, "command": command, "exit": exit_code,
                        "error": error, "passed": passed})
        print(f"{name}: {'PASS' if passed else 'FAIL'} exit={exit_code}", flush=True)
        return passed

    reference = str(args.reference.resolve())
    prototype = str(args.prototype.resolve())
    corpus = str(args.corpus.resolve())
    owned = args.report / "corpus-v1.ljbc"
    completed_all_steps = False
    try:
        run("reference-source", [reference, "--corpus", corpus], "API_PASS checks=61")
        run("prototype-source", [prototype, "--expect-high", "--corpus", corpus], "API_PASS checks=61")
        dumped = run("reference-dump", [reference, "--dump", corpus, str(owned)], "OWNED_DUMP_PASS")
        if dumped and owned.read_bytes()[:4] == b"\x1bLJ\x01":
            run("reference-bytecode", [reference, "--corpus", str(owned)], "API_PASS checks=61")
            run("prototype-bytecode", [prototype, "--expect-high", "--corpus", str(owned)], "API_PASS checks=61")
        else:
            results.append({"name": "owned-bytecode-header", "passed": False})
        run("reference-stress", [reference, "--stress"], "STRESS_PASS threads=4 states=48")
        run("prototype-stress", [prototype, "--expect-high", "--stress"], "STRESS_PASS threads=4 states=48")
        if args.game:
            index = []
            total = 0
            # Extraction stays in the explicitly designated external report
            # directory. Archive names are metadata only, never output paths.
            with tempfile.TemporaryDirectory(prefix="load-only-", dir=args.report) as temporary:
                folder = Path(temporary)
                with zipfile.ZipFile(args.game) as archive:
                    chunks = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".lua")]
                    if not 1 <= len(chunks) <= 4096:
                        raise ValueError("unexpected game chunk count")
                    for number, entry in enumerate(chunks):
                        total += entry.file_size
                        if entry.file_size > 32 * 1024 * 1024 or total > 256 * 1024 * 1024:
                            raise ValueError("game chunk extraction limit exceeded")
                        content = archive.read(entry)  # Includes actual ZIP CRC validation.
                        if content[:4] != b"\x1bLJ\x01":
                            raise ValueError("game contains non-v1 Lua chunks")
                        destination = folder / f"{number:04d}.ljbc"
                        destination.write_bytes(content)
                        index.append({"name": entry.filename, "bytes": len(content),
                                      "sha256": hashlib.sha256(content).hexdigest()})
                manifest = folder / "manifest.txt"
                manifest.write_text("".join(str(folder / f"{number:04d}.ljbc") + "\n"
                                            for number in range(len(index))), encoding="utf-8")
                (args.report / "game-load-only-index.json").write_text(json.dumps(index, indent=2))
                marker = f"LOAD_ONLY_PASS count={len(index)}"
                run("reference-game-load-only", [reference, "--load-manifest", str(manifest)], marker)
                run("prototype-game-load-only", [prototype, "--expect-high", "--load-manifest", str(manifest)], marker)
        completed_all_steps = True
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
        results.append({"name": "infrastructure", "passed": False, "error": str(exc)})
    finally:
        inputs = {}
        for path in (args.reference, args.prototype, args.corpus):
            try:
                inputs[str(path)] = sha256(path)
            except OSError as exc:
                results.append({"name": "input-hash", "passed": False,
                                "path": str(path), "error": str(exc)})
        report = {"results": results,
                  "all_passed": completed_all_steps and bool(results) and all(r["passed"] for r in results),
                  "inputs": inputs,
                  "game_executed": False, "device_executed": False}
        (args.report / "results.json").write_text(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
