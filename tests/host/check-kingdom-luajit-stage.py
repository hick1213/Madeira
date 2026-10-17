#!/usr/bin/env python3
"""Exercise real candidate staging and refusal paths; never launch a runtime."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/stage-kingdom-luajit.py"
OUTPUT = ROOT / "issue/multi-fixes-20261012/kingdom"
BUILD = Path(os.environ.get("MADEIRA_LUAJIT_BUILD_DIR", str(ROOT / ".prototype-build/windows-dynamic")))
ORIGINAL = ROOT / "app/Build/Products/CrashInvestigations/2026-10-12-kingdom-recurrence/lua51.dll"
EVIDENCE = ROOT / "app/Build/Products/IPAInspection/lua-compat-prototype"


class StageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "isolated staging tool has not been implemented")
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.destination = OUTPUT / ("host-test-" + uuid.uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(self.destination) if self.destination.is_dir() else None)

    def run_tool(self, destination=None, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--destination",
                               str(destination or self.destination), *extra],
                              capture_output=True, text=True, timeout=120)

    def require_reviewed_files(self, *paths: Path) -> None:
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            self.skipTest("reviewed/private fixtures unavailable: " + ", ".join(missing))

    def test_real_candidate_has_verified_exports_and_unchanged_original(self):
        runtime = ROOT / "experiments/legacy-luajit/runtime"
        build_sources = [BUILD / "source" / path.relative_to(runtime)
                         for path in runtime.rglob("*")
                         if path.is_file() and not path.name.startswith("._")]
        self.require_reviewed_files(
            BUILD / "lua51.dll", BUILD / "probe.exe", *build_sources,
            ORIGINAL, ORIGINAL.with_name("Kingdom Rush Frontiers.exe"),
            EVIDENCE / "root-final-host-3/corpus-v1.ljbc",
            EVIDENCE / "exported-source-sha256.json", EVIDENCE / "upstream.json",
            EVIDENCE / "root-final-host-3/results.json",
            EVIDENCE / "security-runtime/luasecurity-runtime-sb0j1zco/final-review/review-results.json",
            ROOT / "toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin/llvm-readobj",
        )
        before = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((self.destination / "manifest.json").read_text())
        self.assertEqual(manifest["pe_checks"]["original_exports"], 135)
        self.assertEqual(manifest["pe_checks"]["candidate_exports"], 135)
        self.assertEqual(len(manifest["pe_checks"]["required_game_imports"]), 16)
        self.assertFalse(manifest["windows_executed"])
        self.assertFalse(manifest["device_executed"])
        self.assertFalse(manifest["game_executed"])
        for name, record in manifest["payloads"].items():
            self.assertEqual(hashlib.sha256((self.destination / name).read_bytes()).hexdigest(), record["sha256"])
        self.assertEqual((self.destination / "corpus-v1.ljbc").read_bytes()[:4], b"\x1bLJ\x01")
        self.assertEqual(hashlib.sha256(ORIGINAL.read_bytes()).hexdigest(), before)
        self.assertFalse((self.destination / "Kingdom Rush Frontiers.exe").exists())

    def test_existing_destination_preserves_unrelated_file(self):
        self.destination.mkdir()
        sentinel = self.destination / "user-work"
        sentinel.write_bytes(b"preserve me")
        result = self.run_tool()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination already exists", result.stderr)
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"preserve me")

    def test_outside_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "candidate"
            result = self.run_tool(target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("direct child", result.stderr)
            self.assertFalse(target.exists())

    def test_symlink_destination_preserves_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            self.destination.symlink_to(target, target_is_directory=True)
            self.addCleanup(self.destination.unlink)
            result = self.run_tool()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("destination already exists", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_tampered_binary_is_refused_before_destination_creation(self):
        self.require_reviewed_files(BUILD / "lua51.dll", BUILD / "probe.exe",
                                    EVIDENCE / "root-final-host-3/corpus-v1.ljbc")
        with tempfile.TemporaryDirectory(prefix="kingdom-tampered-") as folder:
            build = Path(folder)
            shutil.copyfile(BUILD / "probe.exe", build / "probe.exe")
            data = bytearray((BUILD / "lua51.dll").read_bytes())
            data[-1] ^= 1
            (build / "lua51.dll").write_bytes(data)
            result = self.run_tool(None, "--build-dir", str(build))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pinned hash mismatch", result.stderr)
            self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
