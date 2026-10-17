#!/usr/bin/env python3
"""Run production Epic storage against real sandbox and APFS boundaries."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'app/Build/Products/IPAInspection/epic-record-tests'
CONTAINER_ARTIFACTS = ROOT / 'app/Build/Products/IPAInspection/epic-container-fix-tests'

SANDBOX_MAIN = r'''
import Foundation
import Darwin
@main struct Main {
 static func main() {
  let documents = URL(fileURLWithPath: CommandLine.arguments[1])
  let container = URL(fileURLWithPath: CommandLine.arguments[3])
  var report: [String: Any] = [:]
  let root = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
  report["rootOpenDenied"] = root < 0
  if root >= 0 { close(root) }
  let anchor = Darwin.open(container.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
  report["containerOpenAllowed"] = anchor >= 0
  if anchor >= 0 { close(anchor) }
  let ancestor = Darwin.open(container.deletingLastPathComponent().path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
  report["ancestorOpenDenied"] = ancestor < 0
  report["ancestorErrno"] = errno
  if ancestor >= 0 { close(ancestor) }
  let outside = Darwin.open(CommandLine.arguments[2], O_RDONLY | O_CLOEXEC)
  report["outsideReadDenied"] = outside < 0
  report["outsideErrno"] = errno
  if outside >= 0 { close(outside) }
  do {
   let control = documents.appendingPathComponent("control.json")
   try Data("CONTROL".utf8).write(to: control)
   report["controlWriteRead"] = try Data(contentsOf: control) == Data("CONTROL".utf8)
  } catch { report["controlError"] = String(describing: error) }
  do {
   let store = try __STORE_CALL__
   try store.write(Data("RECORD".utf8), name: "downloads.json")
   report["productionReadWrite"] = try store.read(name: "downloads.json") == Data("RECORD".utf8)
  } catch {
   report["productionError"] = String(describing: error)
   report["productionErrno"] = errno
  }
  do {
   let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
   print(String(decoding: data, as: UTF8.self))
  } catch { fputs("Could not serialize test evidence\n", stderr); exit(44) }
  let controlsPassed = report["controlWriteRead"] as? Bool == true
   && report["containerOpenAllowed"] as? Bool == true
   && report["ancestorOpenDenied"] as? Bool == true
   && report["outsideReadDenied"] as? Bool == true
  exit(controlsPassed ? (report["productionReadWrite"] as? Bool == true ? 0 : 42) : 43)
 }
}
'''

SECURITY_MAIN = r'''
import Foundation
import Darwin
enum TestFailure: Error { case failed(String) }
@main struct Main {
 static func main() {
  do { try run() }
  catch { print("FAIL \(error)"); exit(1) }
 }
 static func run() throws {
  let fixture = URL(fileURLWithPath: CommandLine.arguments[1])
  let fm = FileManager.default
  let container = fixture.appendingPathComponent("AppContainer")
  let documents = container.appendingPathComponent("Documents")
  let outside = fixture.appendingPathComponent("outside")
  try fm.createDirectory(at: documents, withIntermediateDirectories: true)
  try fm.createDirectory(at: outside.appendingPathComponent("Documents"), withIntermediateDirectories: true)
  let sentinel = outside.appendingPathComponent("keep.txt")
  let kept = Data("KEEP".utf8)
  try kept.write(to: sentinel)
  var checks: [String] = []
  func check(_ condition: Bool, _ name: String) throws {
   guard condition else { throw TestFailure.failed(name) }
   checks.append(name)
  }
  func rejected(_ name: String, _ body: () throws -> Void) throws {
   var rejected = false
   do { try body() } catch { rejected = true }
   try check(rejected, name)
  }
  func unsafe(_ name: String, at target: URL, anchor: URL? = nil) throws {
   var rejected = false
   do {
    let fd = try EpicDirectory.openDirectory(at: target, trustedContainer: anchor ?? container, create: true)
    close(fd)
   } catch EpicDirectory.Failure.unsafePath { rejected = true }
   catch { throw TestFailure.failed(name + " rejected only for filesystem failure: \(error)") }
   try check(rejected, name)
  }
  func signature(_ path: URL) throws -> String {
   var info = stat()
   guard lstat(path.path, &info) == 0 else { throw TestFailure.failed("stat sentinel") }
   return "\(info.st_dev):\(info.st_ino):\(info.st_nlink):\(info.st_size):\(info.st_mtimespec.tv_sec):\(info.st_mtimespec.tv_nsec)"
  }
  let store = try EpicRecordStore(documents: documents, trustedContainer: container)
  try store.write(Data("first".utf8), name: "downloads.json")
  try check(try store.read(name: "downloads.json") == Data("first".utf8), "owned write/read")
  try store.write(Data("second".utf8), name: "downloads.json")
  try check(try store.read(name: "downloads.json") == Data("second".utf8), "owned atomic replacement")
  try check(try store.read(name: "missing.json") == nil, "missing record")
  var recordInfo = stat(), epicInfo = stat()
  try check(lstat(documents.appendingPathComponent("epic/downloads.json").path, &recordInfo) == 0 && recordInfo.st_mode & 0o777 == 0o600, "record mode 0600")
  try check(lstat(documents.appendingPathComponent("epic").path, &epicInfo) == 0 && epicInfo.st_mode & 0o777 == 0o700, "epic directory mode 0700")
  for name in ["", ".", "..", "../keep.txt", "a/b", "bad\0name", "bad name"] {
   try rejected("invalid record name \(name.debugDescription)") { try store.write(Data(), name: name) }
  }
  let linkedRecord = documents.appendingPathComponent("epic/link.json")
  try fm.createSymbolicLink(at: linkedRecord, withDestinationURL: sentinel)
  try rejected("linked record read") { _ = try store.read(name: "link.json") }
  try rejected("linked record write") { try store.write(Data("BAD".utf8), name: "link.json") }
  try check(try Data(contentsOf: sentinel) == kept, "linked record sentinel preserved")
  let hardlink = documents.appendingPathComponent("epic/hard.json")
  try fm.linkItem(at: sentinel, to: hardlink)
  let beforeSentinel = try signature(sentinel), beforeHardlink = try signature(hardlink)
  try rejected("hardlinked record read") { _ = try store.read(name: "hard.json") }
  try rejected("hardlinked record write") { try store.write(Data("BAD".utf8), name: "hard.json") }
  try check(try Data(contentsOf: sentinel) == kept, "hardlink sentinel data preserved")
  try check(try Data(contentsOf: hardlink) == kept, "hardlink record data preserved")
  try check(try signature(sentinel) == beforeSentinel, "hardlink sentinel inode/link metadata preserved")
  try check(try signature(hardlink) == beforeHardlink, "hardlink record inode/link metadata preserved")
  let linkedDocuments = container.appendingPathComponent("LinkedDocuments")
  try fm.createSymbolicLink(at: linkedDocuments, withDestinationURL: outside.appendingPathComponent("Documents"))
  try rejected("linked Documents rejected") { _ = try EpicRecordStore(documents: linkedDocuments, trustedContainer: container) }
  let linkedParent = container.appendingPathComponent("LinkedParent")
  try fm.createSymbolicLink(at: linkedParent, withDestinationURL: outside)
  try rejected("linked writable ancestor rejected") { _ = try EpicRecordStore(documents: linkedParent.appendingPathComponent("Documents"), trustedContainer: container) }
  let other = container.appendingPathComponent("OtherDocuments")
  try fm.createDirectory(at: other, withIntermediateDirectories: false)
  try fm.createSymbolicLink(at: other.appendingPathComponent("epic"), withDestinationURL: outside)
  try rejected("linked epic root rejected") { _ = try EpicRecordStore(documents: other, trustedContainer: container) }
  let linkedContainer = fixture.appendingPathComponent("LinkedContainer")
  try fm.createSymbolicLink(at: linkedContainer, withDestinationURL: container)
  try rejected("linked trusted container leaf rejected") { _ = try EpicRecordStore(documents: linkedContainer.appendingPathComponent("Documents"), trustedContainer: linkedContainer) }
  let prefixSibling = fixture.appendingPathComponent("AppContainer-other")
  try fm.createDirectory(at: prefixSibling.appendingPathComponent("Documents"), withIntermediateDirectories: true)
  try unsafe("container component prefix rejected", at: prefixSibling.appendingPathComponent("Documents"))
  try unsafe("outside Documents rejected", at: outside.appendingPathComponent("Documents"))
  let directoryBefore = try fm.contentsOfDirectory(atPath: container.path).sorted()
  let dot = URL(string: container.absoluteString + "/./dot-child")!
  let parent = URL(string: container.absoluteString + "/../escape-child")!
  try unsafe("raw dot path rejected before create", at: dot)
  try unsafe("raw parent traversal rejected before create", at: parent)
  let actualNUL = URL(fileURLWithPath: container.path + "/nul\0tail", isDirectory: true)
  let encodedNUL = URL(string: container.absoluteString + "/encoded%00tail")!
  try check(actualNUL.path(percentEncoded: false).contains("\0"), "actual NUL fixture really contains NUL")
  try check(encodedNUL.path(percentEncoded: false).contains("\0"), "encoded NUL fixture really contains NUL")
  try unsafe("actual NUL rejected before create", at: actualNUL)
  try unsafe("encoded NUL rejected before create", at: encodedNUL)
  try unsafe("NUL trusted anchor rejected before open", at: documents, anchor: actualNUL)
  try check(try fm.contentsOfDirectory(atPath: container.path).sorted() == directoryBefore, "rejected create paths left container unchanged")
  try check(!fm.fileExists(atPath: fixture.appendingPathComponent("escape-child").path), "rejected traversal left outside unchanged")
  let literal = URL(fileURLWithPath: container.path + "/literal%00tail", isDirectory: true)
  try check(!literal.path(percentEncoded: false).contains("\0"), "literal percent00 fixture has no NUL")
  let literalFD = try EpicDirectory.openDirectory(at: literal, trustedContainer: container, create: true)
  close(literalFD)
  try check(fm.fileExists(atPath: container.appendingPathComponent("literal%00tail").path), "literal percent00 directory creation allowed")
  try check(!fm.fileExists(atPath: container.appendingPathComponent("literal").path), "literal percent00 did not create truncated directory")
  let originalEpic = documents.appendingPathComponent("epic")
  let savedEpic = documents.appendingPathComponent("epic-saved")
  try fm.moveItem(at: originalEpic, to: savedEpic)
  try fm.createDirectory(at: originalEpic, withIntermediateDirectories: false)
  try rejected("replacement epic directory read") { _ = try store.read(name: "downloads.json") }
  try rejected("replacement epic directory write") { try store.write(Data("BAD".utf8), name: "downloads.json") }
  try check(try Data(contentsOf: savedEpic.appendingPathComponent("downloads.json")) == Data("second".utf8), "old pinned directory data preserved")
  try check(try fm.contentsOfDirectory(atPath: originalEpic.path).isEmpty, "replacement directory untouched")
  try fm.removeItem(at: originalEpic)
  try fm.createSymbolicLink(at: originalEpic, withDestinationURL: outside)
  try rejected("replacement epic link read") { _ = try store.read(name: "downloads.json") }
  try rejected("replacement epic link write") { try store.write(Data("BAD".utf8), name: "downloads.json") }
  try check(try Data(contentsOf: sentinel) == kept, "replacement link sentinel preserved")
  try fm.removeItem(at: originalEpic)
  try fm.moveItem(at: savedEpic, to: originalEpic)
  try store.write(Data("restored".utf8), name: "downloads.json")
  try check(try store.read(name: "downloads.json") == Data("restored".utf8), "restored owned directory remains usable")
  try check(try fm.contentsOfDirectory(atPath: originalEpic.path).allSatisfy { !$0.hasPrefix(".tmp-") }, "atomic write scratch cleaned")
  try check(try fm.contentsOfDirectory(atPath: outside.path).sorted() == ["Documents", "keep.txt"], "all outside directory entries preserved")
  let report: [String: Any] = ["checks": checks, "assertions": checks.count]
  let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
  print(String(decoding: data, as: UTF8.self))
 }
}
'''


class RecordTests(unittest.TestCase):
    def compile_production(self, build, main_text, artifacts, label, optimized=False, record_source=None):
        sources = [ROOT / 'app/Madeira/EpicDirectory.swift', record_source or ROOT / 'app/Madeira/EpicRecords.swift']
        snapshots = []
        hashes = {}
        for source in sources:
            data = source.read_bytes()
            snapshot = build / source.name
            snapshot.write_bytes(data)
            snapshots.append(str(snapshot))
            hashes[source.name] = hashlib.sha256(data).hexdigest()
        main = build / 'main.swift'
        main.write_text(main_text)
        binary = build / 'test'
        command = ['xcrun', 'swiftc', '-swift-version', '6', '-strict-concurrency=complete', '-parse-as-library']
        if optimized:
            command.append('-O')
        result = subprocess.run(command + snapshots + [str(main), '-o', str(binary)], capture_output=True, text=True, timeout=90)
        (artifacts / (label + '-compile.txt')).write_text(result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        return binary, hashes

    def run_sandbox_case(self, record_source=None, label='green'):
        """Keep loader/rule failures distinct from the production failure.

        macOS cannot deny read-data on root: its loader aborts before main even
        for /bin/echo. /private/tmp and the container's parent are denied instead;
        the supplied trusted container itself is permitted for the new anchor FD.
        """
        sandbox = shutil.which('sandbox-exec')
        self.assertIsNotNone(sandbox, 'This regression needs the macOS sandbox runner')
        CONTAINER_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        report = {}
        fixture_path = None
        try:
            with tempfile.TemporaryDirectory(prefix='compile-', dir=CONTAINER_ARTIFACTS) as build, tempfile.TemporaryDirectory(prefix='madeira-apfs-epic-sandbox-', dir='/private/tmp') as fixture:
                build = Path(build)
                fixture_path = Path(fixture)
                container = fixture_path / 'AppContainer'
                documents = container / 'Documents'
                documents.mkdir(parents=True)
                outside = fixture_path / 'outside.txt'
                outside.write_text('KEEP')
                call = 'EpicRecordStore(documents: documents)' if record_source else 'EpicRecordStore(documents: documents, trustedContainer: container)'
                binary, hashes = self.compile_production(build, SANDBOX_MAIN.replace('__STORE_CALL__', call), CONTAINER_ARTIFACTS, label, record_source=record_source)
                report['sourceSHA256'] = hashes
                profile = (
                    '(version 1) (allow default) '
                    '(deny file-read-data (literal "/private/tmp") (subpath '
                    + json.dumps(str(fixture_path)) + ')) '
                    '(allow file-read-data (subpath ' + json.dumps(str(container)) + '))'
                )
                (CONTAINER_ARTIFACTS / (label + '-profile.sb')).write_text(profile)
                run = subprocess.run([sandbox, '-p', profile, str(binary), str(documents), str(outside), str(container)], capture_output=True, text=True, timeout=20)
                report['childExit'] = run.returncode
                (CONTAINER_ARTIFACTS / (label + '-run.txt')).write_text(run.stdout + run.stderr)
                self.assertTrue(run.stdout.strip(), 'Sandbox child did not reach main: ' + run.stderr)
                child = json.loads(run.stdout)
                report['child'] = child
                self.assertTrue(child.get('controlWriteRead'), child)
                self.assertTrue(child.get('containerOpenAllowed'), child)
                self.assertTrue(child.get('ancestorOpenDenied'), child)
                self.assertEqual(child.get('ancestorErrno'), 1, child)
                self.assertTrue(child.get('outsideReadDenied'), child)
                self.assertEqual(child.get('outsideErrno'), 1, child)
        finally:
            report['scratchRemoved'] = fixture_path is None or not fixture_path.exists()
            (CONTAINER_ARTIFACTS / (label + '-result.json')).write_text(json.dumps(report, indent=2))
        return report

    def test_documents_remain_usable_when_ancestors_cannot_be_opened(self):
        report = self.run_sandbox_case()
        self.assertTrue(report['child'].get('productionReadWrite'), report)
        self.assertEqual(report['childExit'], 0, report)
        self.assertTrue(report['scratchRemoved'], report)

    def test_owned_directory_and_atomic_records(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for optimized in [False, True]:
            with self.subTest(optimized=optimized):
                label = 'optimized' if optimized else 'debug'
                report = {}
                fixture_path = None
                try:
                    # Compiles/logs stay external; only real APFS fixtures live internally.
                    with tempfile.TemporaryDirectory(prefix='compile-', dir=ARTIFACTS) as build, tempfile.TemporaryDirectory(prefix='madeira-apfs-epic-records-', dir='/private/tmp') as fixture:
                        fixture_path = Path(fixture)
                        binary, hashes = self.compile_production(Path(build), SECURITY_MAIN, ARTIFACTS, label, optimized=optimized)
                        report['sourceSHA256'] = hashes
                        run = subprocess.run([str(binary), fixture], capture_output=True, text=True, timeout=20)
                        report['childExit'] = run.returncode
                        (ARTIFACTS / (label + '-run.txt')).write_text(run.stdout + run.stderr)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        child = json.loads(run.stdout)
                        report['child'] = child
                        self.assertGreaterEqual(child['assertions'], 45, child)
                finally:
                    report['scratchRemoved'] = fixture_path is None or not fixture_path.exists()
                    (ARTIFACTS / (label + '-result.json')).write_text(json.dumps(report, indent=2))
                self.assertTrue(report['scratchRemoved'], report)


if __name__ == '__main__':
    unittest.main()
