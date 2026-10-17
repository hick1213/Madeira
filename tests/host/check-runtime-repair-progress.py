#!/usr/bin/env python3
"""Exercise bounded live installer receipts without treating progress as success."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'issue/epic-retry-20261015/bg3-runtime-repair/live-progress'
HARNESS = r'''
import Foundation
import CryptoKit
import Darwin

@main struct Main {
 static func main() throws {
  func check(_ value: Bool, _ message: String) throws {
   if !value { throw NSError(domain: message, code: 1) }
  }
  func invalid(_ data: Data, plan: RuntimeRepair.Plan, _ message: String) throws {
   var rejected = false
   do { _ = try RuntimeRepair.parseLiveProgress(data, plan: plan) }
   catch RuntimeRepair.RepairError.invalidReceipt { rejected = true }
   try check(rejected, message)
  }
  func unsafe(_ body: () throws -> Void, _ message: String) throws {
   var rejected = false
   do { try body() } catch RuntimeRepair.RepairError.unsafePath { rejected = true }
   try check(rejected, message)
  }
  let requirement = RuntimePreflight.Requirement(family: .desktop, version: .init(major: 6, minor: 0, patch: 0), architecture: .x64)
  let package = RuntimeRepair.Package(requirement: requirement, version: .init(major: 6, minor: 0, patch: 36),
    url: URL(string: "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe")!,
    sha512: String(repeating: "f", count: 128), fileName: "windowsdesktop-runtime-6.0.36-win-x64.exe")
  let nonce = "a9231acd-9877-47e1-846d-84d31284842e"
  let plan = RuntimeRepair.Plan(identityHash: String(repeating: "a", count: 64), nonce: nonce,
    drivePath: "/fixture/drive_c", directoryPath: "/fixture/repair", packages: [package, package, package],
    driveDevice: 1, driveInode: 2, rootInode: 3, directoryInode: 4, scriptInode: 5,
    receiptInode: 6, installerInodes: [7, 8, 9], manifestInode: 10)
  typealias Progress = RuntimeRepair.LiveProgress
  let firstFailure = Progress.Failure(index: 1, exitCode: 1603)
  let lines = ["BEGIN \(nonce) 3", "START \(nonce) 0", "EXIT \(nonce) 0 3010",
               "START \(nonce) 1", "EXIT \(nonce) 1 1603", "START \(nonce) 2",
               "EXIT \(nonce) 2 5", "END \(nonce) 3"]
  let checkpoints: [Progress] = [
    .init(), .init(stage: .installing(index: 0)),
    .init(stage: .installing(index: 0), completed: 1),
    .init(stage: .installing(index: 1), completed: 1),
    .init(stage: .installing(index: 1), completed: 2, firstFailure: firstFailure),
    .init(stage: .installing(index: 2), completed: 2, firstFailure: firstFailure),
    .init(stage: .installing(index: 2), completed: 3, firstFailure: firstFailure),
    .init(stage: .finishing, completed: 3, firstFailure: firstFailure)
  ]
  let stream = Data((lines.joined(separator: "\r\n") + "\r\n").utf8)
  var boundary = 0, milestones: [(Int, Progress)] = []
  for (index, line) in lines.enumerated() {
   boundary += line.utf8.count + 2
   milestones.append((boundary, checkpoints[index]))
  }
  // Literal expected states are attached only to completed fixture lines. Every
  // intermediate byte, including a final CR without LF, retains the prior state.
  for length in 0...stream.count {
   let expected = milestones.last { $0.0 <= length }?.1 ?? Progress()
   let observed = try RuntimeRepair.parseLiveProgress(Data(stream.prefix(length)), plan: plan)
   try check(observed == expected, "partial live receipt changed state before a complete valid line at byte \(length)")
  }
  let finished = checkpoints.last!
  try check(try RuntimeRepair.parseLiveProgress(Data((lines.joined(separator: "\n") + "\n").utf8), plan: plan) == finished,
            "LF-only complete receipts preserve the same bounded progress")
  for tail in [Data("unterminated garbage".utf8), Data([0xf0, 0x9f]), Data([13])] {
   try check(try RuntimeRepair.parseLiveProgress(stream + tail, plan: plan) == finished,
             "an unfinished trailing line is ignored even after END or during partial UTF8/CRLF")
  }
  for text in ["", "B", "BEGIN wrong nonce", "BEGIN \(nonce) 3\r"] {
   try check(try RuntimeRepair.parseLiveProgress(Data(text.utf8), plan: plan) == Progress(),
             "empty or incomplete BEGIN waits without claiming an installer started")
  }
  for codes in [[0, 3010, 1641], [5, 1603, 3010], [Int(UInt32.max), 0, 0]] {
   var receipt = "BEGIN \(nonce) 3\n"
   for index in 0..<3 { receipt += "START \(nonce) \(index)\nEXIT \(nonce) \(index) \(codes[index])\n" }
   receipt += "END \(nonce) 3\n"
   let progress = try RuntimeRepair.parseLiveProgress(Data(receipt.utf8), plan: plan)
   let failure: Progress.Failure? = codes[0] == 0 ? nil : .init(index: 0, exitCode: codes[0])
   try check(progress == .init(stage: .finishing, completed: 3, firstFailure: failure),
             "restart codes are not failures and the first failure wins across all completed installers")
  }
  let begin = "BEGIN \(nonce) 3\n", start = "START \(nonce) 0\n"
  for text in [
   "BEGIN other 3\n", "BEGIN \(nonce) 03\n", "BEGIN \(nonce) 2\n", "\n", "garbage\n",
   "START \(nonce) 0\n", begin + begin, begin + "START \(nonce) 1\n",
   begin + "EXIT \(nonce) 0 0\n", begin + start + start,
   begin + start + "EXIT other 0 0\n", begin + start + "EXIT \(nonce) 1 0\n",
   begin + "END \(nonce) 3\n", begin + start + "END \(nonce) 3\n",
   begin + start + "EXIT \(nonce) 0 0\nEXIT \(nonce) 0 0\n",
   lines.dropLast().joined(separator: "\n") + "\nEND \(nonce) 2\n",
   String(decoding: stream, as: UTF8.self) + "END \(nonce) 3\n",
   String(decoding: stream, as: UTF8.self) + "garbage\n",
   String(decoding: stream, as: UTF8.self) + "\n", "BEGIN \(nonce) 3\r\r\n"
  ] {
   try invalid(Data(text.utf8), plan: plan, "complete wrong nonce, order, count or trailing garbage must be rejected")
  }
  for code in ["-1", "+1", "01", "4294967296", "", "1 extra", " 1", "1 "] {
   try invalid(Data((begin + start + "EXIT \(nonce) 0 \(code)\n").utf8), plan: plan,
               "live installer exit codes must use canonical UInt32 text")
  }
  for bytes in [Data([0xff, 10]), Data([0, 10]), Data("é\n".utf8), Data((begin + "ST\rART \(nonce) 0\n").utf8)] {
   try invalid(bytes, plan: plan, "completed receipt lines reject invalid UTF8, non-ASCII and embedded controls")
  }
  let atLimit = stream + Data(repeating: 32, count: 16 * 1024 - stream.count)
  try check(try RuntimeRepair.parseLiveProgress(atLimit, plan: plan) == finished, "a bounded unfinished tail may reach exactly 16 KiB")
  try invalid(atLimit + Data([32]), plan: plan, "live receipt parsing rejects input above 16 KiB")
  for count in [0, 9] {
   var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(plan)) as! [String: Any]
   let encodedPackage = (object["packages"] as! [[String: Any]])[0]
   object["packages"] = Array(repeating: encodedPackage, count: count)
   let invalidPlan = try JSONDecoder().decode(RuntimeRepair.Plan.self, from: JSONSerialization.data(withJSONObject: object))
   try invalid(Data(), plan: invalidPlan, "standalone live parsing requires one through eight bound packages")
  }

  // Use actual private plans on internal APFS. Empty installer files deliberately
  // cannot pass SHA512 validation: progress polling must not scan/hash installers.
  let fm = FileManager.default, root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
  let drive = root.appendingPathComponent("drive_c", isDirectory: true)
  try fm.createDirectory(at: drive, withIntermediateDirectories: false)
  let identity = "runtime-progress-fixture"
  let diskPlan = try RuntimeRepair.makePlan(packages: [package, package, package], drive: drive, identity: identity)
  let directory = URL(fileURLWithPath: diskPlan.directoryPath, isDirectory: true)
  let originalNames = try fm.contentsOfDirectory(atPath: directory.path).sorted()
  try check(try RuntimeRepair.liveProgress(plan: diskPlan, identity: identity, drive: drive) == Progress(),
            "an actual empty bound receipt waits without requiring installer hash success")
  func writeReceipt(_ data: Data) throws {
   let handle = try FileHandle(forWritingTo: diskPlan.resultURL)
   defer { try? handle.close() }
   try handle.truncate(atOffset: 0); try handle.write(contentsOf: data); try handle.synchronize()
  }
  let diskReceipt = Data("BEGIN \(diskPlan.nonce) 3\r\nSTART \(diskPlan.nonce) 0\r\n".utf8)
  try writeReceipt(diskReceipt)
  try check(try RuntimeRepair.liveProgress(plan: diskPlan, identity: identity, drive: drive) == .init(stage: .installing(index: 0)),
            "actual nonce-bound live receipt reports the currently started installer")
  try check(try Data(contentsOf: diskPlan.resultURL) == diskReceipt && fm.contentsOfDirectory(atPath: directory.path).sorted() == originalNames,
            "progress polling does not modify receipts or add task files")
  try unsafe({ _ = try RuntimeRepair.liveProgress(plan: diskPlan, identity: "other identity", drive: drive) },
             "live progress remains bound to its original identity")
  let retained = directory.appendingPathComponent("retained-receipt.txt")
  try fm.moveItem(at: diskPlan.resultURL, to: retained)
  try check(fm.createFile(atPath: diskPlan.resultURL.path, contents: diskReceipt, attributes: [.posixPermissions: 0o600]),
            "replacement fixture receipt is created")
  try unsafe({ _ = try RuntimeRepair.liveProgress(plan: diskPlan, identity: identity, drive: drive) },
             "a replaced live receipt inode is refused even with identical nonce and contents")
  try fm.removeItem(at: diskPlan.resultURL); try fm.moveItem(at: retained, to: diskPlan.resultURL)
  try fm.moveItem(at: diskPlan.resultURL, to: retained)
  try fm.createSymbolicLink(at: diskPlan.resultURL, withDestinationURL: retained)
  try unsafe({ _ = try RuntimeRepair.liveProgress(plan: diskPlan, identity: identity, drive: drive) },
             "live progress refuses a symlink receipt")
  try fm.removeItem(at: diskPlan.resultURL); try fm.moveItem(at: retained, to: diskPlan.resultURL)
  try writeReceipt(Data(repeating: 32, count: 16 * 1024 + 1))
  try unsafe({ _ = try RuntimeRepair.liveProgress(plan: diskPlan, identity: identity, drive: drive) },
             "the actual bound live receipt is limited before unbounded reading")
  print("PASS every-byte partial receipts, strict order/nonce/codes, bounded read-only progress and inode binding")
 }
}
'''


class RuntimeRepairProgressTests(unittest.TestCase):
    def test_production_live_receipt_progress(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cache = Path.home() / 'Library/Caches/Madeira'
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='runtime-progress-', dir=cache) as filesystem_fixture, \
                tempfile.TemporaryDirectory(prefix='progress-compile-', dir=ARTIFACTS) as directory:
            directory = Path(directory)
            swift = directory / 'Main.swift'
            binary = directory / 'checks'
            swift.write_text(HARNESS)
            compiled = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1',
                str(ROOT / 'app/Madeira/RuntimePreflight.swift'),
                str(ROOT / 'app/Madeira/RuntimeRepair.swift'), str(swift), '-o', str(binary)],
                capture_output=True, text=True, timeout=180)
            (ARTIFACTS / 'compile.txt').write_text(compiled.stdout + compiled.stderr)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary), filesystem_fixture], capture_output=True, text=True, timeout=30)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
