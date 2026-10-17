#!/usr/bin/env python3
"""Compile and exercise the native snapshot core against owned APFS fixtures.

The explicit no-op baseline is a test fixture for the previously missing feature,
not a previous production implementation. It must compile and fail at runtime.
Compiler outputs/reports stay on the external project volume. Only APFS fixture
trees use an exact Home child and are removed in the parent finally block, even
when a native test crashes or times out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/LocalSaveSnapshot.swift"
REPORTS = ROOT / "app/Build/Products/IPAInspection/local-save-snapshot"


def execute(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Own a process session so timed-out native helpers cannot outlive a test."""
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

MAIN = r'''
import Foundation
import CryptoKit
import Darwin

enum TestFailure: Error { case failed(String) }
@main struct Main {
 static func stop(_ process: Process) {
  if process.isRunning { _ = Darwin.kill(process.processIdentifier, SIGTERM) }
  process.waitUntilExit()
 }
 static func main() {
  if CommandLine.arguments.count >= 4, CommandLine.arguments[1] == "--replace-loop" {
   guard setpgid(0, getpgid(getppid())) == 0 else { exit(4) }
   for _ in 0..<200_000 {
    guard renameatx_np(AT_FDCWD, CommandLine.arguments[2], AT_FDCWD, CommandLine.arguments[3], UInt32(RENAME_SWAP)) == 0 else { exit(3) }
    usleep(200)
   }
   exit(0)
  }
  if CommandLine.arguments.count >= 3, CommandLine.arguments[1] == "--write-loop" {
   guard setpgid(0, getpgid(getppid())) == 0 else { exit(4) }
   let fd = open(CommandLine.arguments[2], O_WRONLY | O_CLOEXEC)
   guard fd >= 0 else { exit(2) }
   let block = Data(repeating: 0x41, count: 64 * 1024)
   for _ in 0..<20_000 {
    for offset in stride(from: 0, to: 8 * 1024 * 1024, by: block.count) {
     _ = block.withUnsafeBytes { pwrite(fd, $0.baseAddress!, $0.count, off_t(offset)) }
    }
    usleep(500)
   }
   close(fd); exit(0)
  }
  do { try run() } catch { fputs("FAIL: \(error)\n", stderr); exit(1) }
 }
 static func run() throws {
  let fixture = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
  let fm = FileManager.default
  let docs = fixture.appendingPathComponent("Documents", isDirectory: true)
  let game = docs.appendingPathComponent("wine/drive_c/users/player/Saved Games/Example", isDirectory: true)
  let relative = "wine/drive_c/users/player/Saved Games/Example"
  try fm.createDirectory(at: game.appendingPathComponent("Slots"), withIntermediateDirectories: true)
  try Data("OLD".utf8).write(to: game.appendingPathComponent("Slots/save.dat"))
  try Data("KEEP".utf8).write(to: game.appendingPathComponent("extra.dat"))
  try fm.createDirectory(at: game.appendingPathComponent("empty"), withIntermediateDirectories: false)
  let outside = fixture.appendingPathComponent("outside", isDirectory: true)
  try fm.createDirectory(at: outside, withIntermediateDirectories: false)
  let sentinel = outside.appendingPathComponent("sentinel.dat")
  try Data("SENTINEL".utf8).write(to: sentinel)
  var checks: [String] = []
  func check(_ value: Bool, _ name: String) throws {
   guard value else { throw TestFailure.failed(name) }
   checks.append(name)
  }
  func rejects(_ name: String, _ body: () throws -> Void) throws {
   var rejected = false
   do { try body() } catch { rejected = true }
   try check(rejected, name)
  }
  func file(_ path: String, _ value: String) -> LocalSaveSnapshot.File {
   let bytes = Data(value.utf8)
   return LocalSaveSnapshot.File(path: path, data: bytes,
      sha256: SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined())
  }
  func archive(_ files: [LocalSaveSnapshot.File], version: Int = 1) -> LocalSaveSnapshot.Archive {
   LocalSaveSnapshot.Archive(version: version, id: UUID(), gameID: UUID(), title: "Example",
                             created: Date(), files: files)
  }
  func unchanged(_ value: Data, _ name: String) throws {
   try check(try Data(contentsOf: game.appendingPathComponent("Slots/save.dat")) == value, name)
  }
  func raw(_ value: LocalSaveSnapshot.Archive) throws -> Data {
   let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
   return try encoder.encode(value)
  }
  if CommandLine.arguments.count >= 3, CommandLine.arguments[2] == "swap-denied" {
   let initial = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative,
                                              gameID: UUID(), title: "Example")
   let control = game.deletingLastPathComponent().appendingPathComponent("stage-create-control")
   try fm.createDirectory(at: control, withIntermediateDirectories: false)
   try fm.removeItem(at: control)
   try check(true, "sandbox permits sibling stage creation")
   try rejects("actual rename-swap denied at filesystem boundary") {
    _ = try LocalSaveSnapshot.restore(archive([file("Slots/save.dat", "BAD")]), documents: docs,
     relativeDirectory: relative, expectedFingerprint: initial.fingerprint)
   }
   try unchanged(Data("OLD".utf8), "denied actual swap keeps original save")
   try check(try fm.contentsOfDirectory(atPath: docs.appendingPathComponent("iCloud Save Backups").path).count == 1,
             "denied actual swap keeps completed pre-replacement backup")
   try check(try fm.contentsOfDirectory(atPath: game.deletingLastPathComponent().path) == ["Example"],
             "denied actual swap cleans owned stage")
   print(String(decoding: try JSONSerialization.data(withJSONObject: ["assertions": checks.count, "checks": checks]), as: UTF8.self))
   return
  }
  try LocalSaveSnapshot.validateDirectory(documents: docs, relativeDirectory: relative)
  try check(true, "existing explicit game directory validates without reading")
  try check(LocalSaveSnapshot.pathKey("Straße/Save.dat") == LocalSaveSnapshot.pathKey("STRASSE/save.dat"),
            "UI overwrite key matches POSIX case folding including sharp s")
  let installed = docs.appendingPathComponent("wine/drive_c/ProgramData/InstalledGame/saves")
  try fm.createDirectory(at: installed, withIntermediateDirectories: true)
  try LocalSaveSnapshot.validateDirectory(documents: docs, relativeDirectory: "wine/drive_c/ProgramData/InstalledGame/saves")
  try check(true, "bound specific ProgramData game save directory allowed")
  let original = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative,
                                              gameID: UUID(), title: "Example")
  try check(original.files.count == 2, "production capture reads actual two files")
  try check(Set(original.files.map(\.path)) == ["Slots/save.dat", "extra.dat"], "capture relative paths")
  let encoded = try LocalSaveSnapshot.encode(original)
  let decoded = try LocalSaveSnapshot.decode(encoded)
  try check(decoded.fingerprint == original.fingerprint, "native encode/decode fingerprint roundtrip")
  let renamedMetadata = LocalSaveSnapshot.Archive(version: 1, id: UUID(), gameID: UUID(), title: "Other",
    created: Date(timeIntervalSince1970: 42), files: original.files.reversed())
  try check(renamedMetadata.fingerprint == original.fingerprint, "fingerprint ignores metadata and input ordering")
  try check(archive([file("Slots/save.dat", "NEW"), file("extra.dat", "KEEP")]).fingerprint != original.fingerprint,
            "fingerprint notices actual content difference")

  for path in ["", ".", "..", "../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b",
               "a:b", "nul\0tail", "line\nfeed", "control\u{7f}", "trailing.", "trailing ", "CON", "NUL.txt"] {
   let invalid = archive([file(path, "BAD")])
   try rejects("unsafe archive path \(path.debugDescription)") { _ = try LocalSaveSnapshot.decode(raw(invalid)) }
   try rejects("unsafe encode path \(path.debugDescription)") { _ = try LocalSaveSnapshot.encode(invalid) }
  }
  for invalid in [archive([file("slot.dat", "A"), file("SLOT.dat", "B")]),
                  archive([file("caf\u{e9}.dat", "A"), file("cafe\u{301}.dat", "B")]),
                  archive([file("parent", "A"), file("parent/child", "B")]),
                  archive([file("Folder/a", "A"), file("folder/b", "B")])] {
   try rejects("Windows or Unicode collision \(checks.count)") { _ = try LocalSaveSnapshot.decode(raw(invalid)) }
  }
  try rejects("unknown archive version") { _ = try LocalSaveSnapshot.decode(raw(archive([], version: 2))) }
  let badHash = LocalSaveSnapshot.File(path: "save.dat", data: Data("DATA".utf8), sha256: String(repeating: "0", count: 64))
  try rejects("corrupt SHA rejects before destination writes") { _ = try LocalSaveSnapshot.decode(raw(archive([badHash]))) }
  try rejects("malformed JSON") { _ = try LocalSaveSnapshot.decode(Data("{}".utf8)) }
  try rejects("bounded raw input") { _ = try LocalSaveSnapshot.decode(Data(count: 96 * 1024 * 1024 + 1)) }
  var paddedJSON = try raw(archive([]))
  paddedJSON.append(Data(repeating: 0x20, count: 96 * 1024 * 1024))
  try rejects("valid JSON still bounded before decode") { _ = try LocalSaveSnapshot.decode(paddedJSON) }
  paddedJSON.removeAll(keepingCapacity: false)
  let tooMany = (0...1024).map { file("file-\($0)", "") }
  try rejects("file count bound") { _ = try LocalSaveSnapshot.encode(archive(tooMany)) }
  let oversized = Data(count: 16 * 1024 * 1024 + 1)
  let largeFile = LocalSaveSnapshot.File(path: "large", data: oversized,
    sha256: SHA256.hash(data: oversized).map { String(format: "%02x", $0) }.joined())
  try rejects("per-file decoded bound") { _ = try LocalSaveSnapshot.encode(archive([largeFile])) }
  let chunk = Data(count: 16 * 1024 * 1024)
  let chunkHash = SHA256.hash(data: chunk).map { String(format: "%02x", $0) }.joined()
  try rejects("total decoded bound") {
   _ = try LocalSaveSnapshot.encode(archive((0..<5).map {
     LocalSaveSnapshot.File(path: "large-\($0)", data: chunk, sha256: chunkHash)
   }))
  }
  for path in ["", ".", "../outside", "/absolute", "wine/drive_c", "wine/drive_c/windows", "wine/drive_c/ProgramData",
               "wine/drive_c/users/player", "wine/drive_c/users/player/Saved Games",
               "wine/drive_c/users/player/AppData/Local", "epic", "steam", "iCloud Save Backups",
               relative + "/../Example", relative + "\0suffix"] {
   try rejects("unsafe configured root \(path.debugDescription)") {
    try LocalSaveSnapshot.validateDirectory(documents: docs, relativeDirectory: path)
   }
  }
  let linkedRoot = docs.appendingPathComponent("LinkedGame")
  try fm.createSymbolicLink(at: linkedRoot, withDestinationURL: game)
  try rejects("linked root") { _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: "LinkedGame", gameID: UUID(), title: "X") }
  let linkedParent = docs.appendingPathComponent("LinkedParent")
  try fm.createSymbolicLink(at: linkedParent, withDestinationURL: outside)
  try rejects("linked ancestor") { try LocalSaveSnapshot.validateDirectory(documents: docs, relativeDirectory: "LinkedParent/child") }
  let linkedDocs = fixture.appendingPathComponent("LinkedDocuments")
  try fm.createSymbolicLink(at: linkedDocs, withDestinationURL: docs)
  try rejects("linked Documents anchor") { try LocalSaveSnapshot.validateDirectory(documents: linkedDocs, relativeDirectory: relative) }
  let linkedFile = game.appendingPathComponent("link.dat")
  try fm.createSymbolicLink(at: linkedFile, withDestinationURL: sentinel)
  try rejects("source symlink") { _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X") }
  try fm.removeItem(at: linkedFile)
  let hardlink = game.appendingPathComponent("hard.dat")
  try fm.linkItem(at: sentinel, to: hardlink)
  try rejects("source hardlink") { _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X") }
  try fm.removeItem(at: hardlink)
  let fifo = game.appendingPathComponent("pipe")
  try check(mkfifo(fifo.path, 0o600) == 0, "real FIFO fixture created")
  try rejects("FIFO rejects without blocking") { _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X") }
  try fm.removeItem(at: fifo)
  for name in ["local.vdf", "loginusers.vdf", "ssfn123456", "Cookies", "Cookies-journal"] {
   try Data("PRIVATE".utf8).write(to: game.appendingPathComponent(name))
   try rejects("credential file \(name) rejected") { _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X") }
   try fm.removeItem(at: game.appendingPathComponent(name))
   try rejects("credential archive \(name) rejected") { _ = try LocalSaveSnapshot.encode(archive([file(name, "PRIVATE")])) }
  }
  let racingFile = game.appendingPathComponent("changing.dat")
  try Data(count: 8 * 1024 * 1024).write(to: racingFile)
  let writer = Process()
  writer.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
  writer.arguments = ["--write-loop", racingFile.path]
  writer.standardOutput = FileHandle.nullDevice; writer.standardError = FileHandle.nullDevice
  try writer.run()
  defer { stop(writer) }
  usleep(20_000)
  try check(writer.isRunning, "real independent writer active")
  try rejects("concurrent source writes reject changed snapshot") {
   _ = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X")
  }
  stop(writer)
  try fm.removeItem(at: racingFile)
  for swapParent in [false, true] {
   let moving = swapParent ? game.deletingLastPathComponent() : game
   let alternate = moving.deletingLastPathComponent().appendingPathComponent(".snapshot-race-alternate")
   try Data(count: 8 * 1024 * 1024).write(to: racingFile)
   let expectedRaceFingerprint = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative,
                                                              gameID: UUID(), title: "X").fingerprint
   try fm.copyItem(at: moving, to: alternate)
   let mover = Process()
   mover.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
   mover.arguments = ["--replace-loop", moving.path, alternate.path]
   mover.standardOutput = FileHandle.nullDevice; mover.standardError = FileHandle.nullDevice
   try mover.run()
   defer { stop(mover) }
   usleep(20_000)
   try check(mover.isRunning, "actual \(swapParent ? "parent" : "root") renamer active")
   var rejectedRename = false
   for _ in 0..<12 {
    do {
     let candidate = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "X")
     // A renamer can return the same unchanged tree before every identity check.
     // Such a complete stable snapshot is legal; never classify it as a failure.
     guard candidate.fingerprint == expectedRaceFingerprint else { throw TestFailure.failed("renamer produced mixed content") }
    } catch is LocalSaveSnapshot.Failure { rejectedRename = true }
    if rejectedRename { break }
   }
   try check(rejectedRename, "concurrent \(swapParent ? "parent" : "root") replacement rejects an invalid identity interleaving")
   stop(mover)
   try fm.removeItem(at: racingFile)
   try fm.removeItem(at: alternate)
  }

  let incoming = archive([file("slots/save.dat", "NEW"), file("new.dat", "ADDED")])
  let beforeCancelled = try fm.contentsOfDirectory(atPath: docs.path).sorted()
  _ = try LocalSaveSnapshot.decode(try LocalSaveSnapshot.encode(incoming))
  try unchanged(Data("OLD".utf8), "prepare/confirmation cancellation leaves original")
  try check(try fm.contentsOfDirectory(atPath: docs.path).sorted() == beforeCancelled,
            "prepare/confirmation cancellation creates no backups or stage")
  try rejects("stale confirmed fingerprint") {
   _ = try LocalSaveSnapshot.restore(incoming, documents: docs, relativeDirectory: relative,
                                    expectedFingerprint: String(repeating: "0", count: 64))
  }
  try unchanged(Data("OLD".utf8), "stale confirm preserves original")
  try check(try fm.contentsOfDirectory(atPath: docs.path).sorted() == beforeCancelled, "stale confirm creates no backup")
  let receipt = try LocalSaveSnapshot.restore(incoming, documents: docs, relativeDirectory: relative,
                                             expectedFingerprint: original.fingerprint)
  try check(receipt.changedFiles == 2, "restore counts changed/new files")
  try unchanged(Data("NEW".utf8), "atomic restore overwrites confirmed file")
  try check(try Data(contentsOf: game.appendingPathComponent("extra.dat")) == Data("KEEP".utf8), "merge retains extra local file")
  try check(try Data(contentsOf: game.appendingPathComponent("new.dat")) == Data("ADDED".utf8), "merge adds file")
  try check(fm.fileExists(atPath: game.appendingPathComponent("empty").path), "merge preserves empty directory")
  try check(!(try fm.contentsOfDirectory(atPath: game.path)).contains("slots")
             && (try fm.contentsOfDirectory(atPath: game.path)).contains("Slots"),
            "Windows equivalent directory preserves original casing")
  let backup = try LocalSaveSnapshot.decode(Data(contentsOf: receipt.backupURL))
  try check(backup.fingerprint == original.fingerprint, "backup is original exact contents before replacement")
  try check(receipt.backupURL.deletingLastPathComponent() == docs.appendingPathComponent("iCloud Save Backups"), "backup stored in declared local backup directory")
  try check(try fm.contentsOfDirectory(atPath: game.deletingLastPathComponent().path).allSatisfy { !$0.hasPrefix(".madeira-save-stage-") }, "successful restore cleans exact stage")
  let post = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "Example")
  let emptyIncoming = archive([])
  let noOpReceipt = try LocalSaveSnapshot.restore(emptyIncoming, documents: docs, relativeDirectory: relative,
    expectedFingerprint: post.fingerprint)
  try check(noOpReceipt.changedFiles == 0, "empty incoming archive merge is non-destructive")
  try check(try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "Example").fingerprint == post.fingerprint, "empty merge retains all local files")
  let emptyTarget = docs.appendingPathComponent("EmptyGame")
  try fm.createDirectory(at: emptyTarget, withIntermediateDirectories: false)
  let emptyLocal = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: "EmptyGame", gameID: UUID(), title: "Empty")
  try check(emptyLocal.files.isEmpty, "empty target capture valid")
  let firstReceipt = try LocalSaveSnapshot.restore(archive([file("first.dat", "FIRST")]), documents: docs,
    relativeDirectory: "EmptyGame", expectedFingerprint: emptyLocal.fingerprint)
  try check(try LocalSaveSnapshot.decode(Data(contentsOf: firstReceipt.backupURL)).files.isEmpty, "empty original receives valid empty backup")

  // Real filesystem denial after reads succeed: no test-only production switch.
  let protectedParent = docs.appendingPathComponent("Protected")
  let protectedGame = protectedParent.appendingPathComponent("Game")
  try fm.createDirectory(at: protectedGame, withIntermediateDirectories: true)
  try Data("KEEP".utf8).write(to: protectedGame.appendingPathComponent("save.dat"))
  let protectedArchive = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: "Protected/Game", gameID: UUID(), title: "P")
  try check(chmod(protectedParent.path, 0o500) == 0, "parent made non-writable by actual permissions")
  defer { _ = chmod(protectedParent.path, 0o700) }
  try rejects("filesystem stage/atomic swap failure") {
   _ = try LocalSaveSnapshot.restore(archive([file("save.dat", "BAD")]), documents: docs,
     relativeDirectory: "Protected/Game", expectedFingerprint: protectedArchive.fingerprint)
  }
  try check(try Data(contentsOf: protectedGame.appendingPathComponent("save.dat")) == Data("KEEP".utf8), "filesystem failure preserves original data")
  try check(chmod(protectedParent.path, 0o700) == 0, "parent permissions restored")
  let backupRoot = docs.appendingPathComponent("iCloud Save Backups")
  let storedBackupRoot = docs.appendingPathComponent("Backups-kept")
  try fm.moveItem(at: backupRoot, to: storedBackupRoot)
  try fm.createSymbolicLink(at: backupRoot, withDestinationURL: outside)
  let stable = try LocalSaveSnapshot.capture(documents: docs, relativeDirectory: relative, gameID: UUID(), title: "E")
  try rejects("linked backup root rejected without outside writes") {
   _ = try LocalSaveSnapshot.restore(incoming, documents: docs, relativeDirectory: relative, expectedFingerprint: stable.fingerprint)
  }
  try check(try fm.contentsOfDirectory(atPath: outside.path) == ["sentinel.dat"], "backup failure cannot write outside Documents")
  try check(try Data(contentsOf: sentinel) == Data("SENTINEL".utf8), "all rejected link/hardlink operations preserve sentinel")
  try fm.removeItem(at: backupRoot)
  try fm.moveItem(at: storedBackupRoot, to: backupRoot)
  let report: [String: Any] = ["assertions": checks.count, "checks": checks,
     "fixtureFilesystem": "APFS", "scope": "actual native core; no device or cloud provider"]
  print(String(decoding: try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), as: UTF8.self))
 }
}
'''

BASELINE = r'''
import Foundation
enum LocalSaveSnapshot {
 enum Failure: Error { case missing }
 struct File: Codable { let path: String; let data: Data; let sha256: String }
 struct Archive: Codable {
  let version: Int; let id: UUID; let gameID: UUID; let title: String; let created: Date; let files: [File]
  var fingerprint: String { "missing-feature-no-op" }
 }
 struct Receipt { let backupURL: URL; let changedFiles: Int }
 static func pathKey(_ path: String) -> String {
  path.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
 }
 static func validateDirectory(documents: URL, relativeDirectory: String) throws {}
 static func capture(documents: URL, relativeDirectory: String, gameID: UUID, title: String) throws -> Archive {
  Archive(version: 1, id: UUID(), gameID: gameID, title: title, created: Date(), files: [])
 }
 static func encode(_ archive: Archive) throws -> Data { try JSONEncoder().encode(archive) }
 static func decode(_ data: Data) throws -> Archive { try JSONDecoder().decode(Archive.self, from: data) }
 static func restore(_ archive: Archive, documents: URL, relativeDirectory: String, expectedFingerprint: String) throws -> Receipt {
  Receipt(backupURL: documents.appendingPathComponent("missing-backup"), changedFiles: 0)
 }
}
'''


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        REPORTS.mkdir(parents=True, exist_ok=True)
        cls.output = Path(tempfile.mkdtemp(prefix="run-", dir=REPORTS))
        cls.fixture_paths: list[str] = []

    def run_native(self, source: str, label: str, optimize: bool = False,
                   swap_denied: bool = False) -> subprocess.CompletedProcess[str]:
        work = self.output / label
        work.mkdir()
        native_source = work / "LocalSaveSnapshot.swift"
        native_source.write_text(source)
        main = work / "Main.swift"
        main.write_text(MAIN)
        binary = work / "snapshot-tests"
        command = ["xcrun", "swiftc", "-swift-version", "6", "-warnings-as-errors",
                   "-parse-as-library", "-O" if optimize else "-Onone",
                   str(native_source), str(main), "-o", str(binary)]
        compiled = execute(command, timeout=90)
        (work / "compile.txt").write_text(compiled.stdout + compiled.stderr)
        self.assertEqual(compiled.returncode, 0, "Native fixture must compile before runtime evidence: " + compiled.stderr)
        fixture = Path(tempfile.mkdtemp(prefix=".madeira-save-snapshot-test-", dir=Path.home()))
        self.fixture_paths.append(str(fixture))
        try:
            execution = [str(binary), str(fixture)]
            if swap_denied:
                game = fixture / "Documents/wine/drive_c/users/player/Saved Games/Example"
                profile = '(version 1) (allow default) (deny file-write-unlink (literal ' + json.dumps(str(game)) + '))'
                (work / "sandbox.sb").write_text(profile)
                execution = ["/usr/bin/sandbox-exec", "-p", profile, str(binary), str(fixture), "swap-denied"]
            completed = execute(execution, timeout=90)
            (work / "runtime.txt").write_text(completed.stdout + completed.stderr)
            if completed.returncode == 0:
                (work / "report.json").write_text(json.dumps(json.loads(completed.stdout), indent=2))
            return completed
        finally:
            # This exact directory was created above. A fixture test may have set
            # directory permissions; restore them before removing only this tree.
            for directory, _, _ in os.walk(fixture, followlinks=False):
                os.chmod(directory, 0o700)
            shutil.rmtree(fixture)
            self.assertFalse(fixture.exists(), "exact APFS fixture cleanup")

    def test_missing_capability_runtime_red(self) -> None:
        completed = self.run_native(BASELINE, "baseline-no-op")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("production capture reads actual two files", completed.stderr)

    def test_real_production_debug(self) -> None:
        completed = self.run_native(SOURCE.read_text(), "production-debug")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_real_production_optimized(self) -> None:
        completed = self.run_native(SOURCE.read_text(), "production-optimized", True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_real_swap_denial(self) -> None:
        for optimized in [False, True]:
            completed = self.run_native(SOURCE.read_text(), "swap-denied-" + str(optimized),
                                        optimized, swap_denied=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_compiled_runtime_mutations(self) -> None:
        source = SOURCE.read_text()
        mutations = [
            ("bad-hash", "file.sha256 == digest(file.data)", "true", "corrupt SHA rejects"),
            ("hardlink", "initial.st_nlink == 1", "initial.st_nlink >= 1", "source hardlink"),
            ("follow-links", [("O_NOFOLLOW", "0"), ("AT_SYMLINK_NOFOLLOW", "0")], None, "linked root"),
            ("stale-confirmation", "fingerprint(original.files) == expectedFingerprint", "true", "stale confirmed fingerprint"),
            ("discard-extra", "original.files.map { (key($0.path), $0) }", "[File]().map { (key($0.path), $0) }", "merge retains extra local file"),
            ("wrong-backup", "title: archive.title, created: Date(), files: original.files)",
             "title: archive.title, created: Date(), files: archive.files)", "backup is original exact contents"),
            ("input-bound", "guard data.count <= maxArchiveBytes else", "guard true else", "valid JSON still bounded"),
            ("file-count", "archive.files.count <= maxFiles", "true", "file count bound"),
            ("file-case-duplicates", "let fileKey = key(file.path)",
             "let fileKey = file.path.precomposedStringWithCanonicalMapping", "Windows or Unicode collision"),
            ("path-conflict", "files.isDisjoint(with: Set(directories.keys))", "true", "Windows or Unicode collision"),
        ]
        evidence = []
        for name, before, after, expected in mutations:
            changes = before if isinstance(before, list) else [(before, after)]
            mutated = source
            for old, new in changes:
                self.assertIn(old, mutated, name + " mutation must affect actual production source")
                mutated = mutated.replace(old, new)
            completed = self.run_native(mutated, "mutation-" + name)
            self.assertEqual(completed.returncode, 1, name + completed.stdout + completed.stderr)
            # Extra-file removal causes Data(contentsOf:) to fail before its check;
            # the concrete absent-file Cocoa error is the expected runtime evidence.
            if name == "discard-extra":
                self.assertIn("NSCocoaErrorDomain", completed.stderr)
            else:
                self.assertIn(expected, completed.stderr, name + completed.stderr)
            evidence.append({"name": name, "compiled": True, "runtimeRejected": True,
                             "expectedFailure": expected})
        (self.output / "mutation-summary.json").write_text(json.dumps(evidence, indent=2))

    def test_ios_sdk_typecheck(self) -> None:
        sdk_result = execute(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], timeout=20)
        self.assertEqual(sdk_result.returncode, 0, sdk_result.stderr)
        sdk = sdk_result.stdout.strip()
        completed = execute(["xcrun", "swiftc", "-swift-version", "6", "-warnings-as-errors",
                                    "-typecheck", "-target", "arm64-apple-ios17.0", "-sdk", sdk, str(SOURCE)],
                            timeout=90)
        (self.output / "ios-typecheck.txt").write_text(completed.stdout + completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_process_group_timeout_cleanup(self) -> None:
        record = self.output / "timeout-child.json"
        program = """import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid': child.pid, 'group': os.getpgid(child.pid)}))
time.sleep(60)
"""
        with self.assertRaises(subprocess.TimeoutExpired):
            execute([shutil.which("python3") or "python3", "-c", program, str(record)], timeout=0.5)
        identity = json.loads(record.read_text())
        observed = execute(["/bin/ps", "-p", str(identity["pid"]), "-o", "stat="], timeout=5)
        stopped = observed.returncode == 1 or observed.stdout.strip().startswith("Z")
        if not stopped:
            # Cleanup only this recorded, owned session if the test exposes a bug.
            try:
                os.killpg(identity["group"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertTrue(stopped, "timed-out native helpers must not outlive the owned session")
        (self.output / "timeout-cleanup.json").write_text(json.dumps({"child": identity, "stopped": stopped}))

    @classmethod
    def tearDownClass(cls) -> None:
        report = {
            "sourceSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest() if SOURCE.exists() else None,
            "noOpBaselineIsProduction": False,
            "fixtures": [{"path": p, "removed": not Path(p).exists()} for p in cls.fixture_paths],
            "scope": "host APFS core only; not iCloud or device interaction",
        }
        (cls.output / "summary.json").write_text(json.dumps(report, indent=2))
        print(f"Artifacts: {cls.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.baseline_only:
        suite = unittest.TestSuite([SnapshotTests("test_missing_capability_runtime_red")])
        outcome = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if outcome.wasSuccessful() else 1)
    unittest.main(argv=[__file__], verbosity=2)
