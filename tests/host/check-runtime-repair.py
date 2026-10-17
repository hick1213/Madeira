#!/usr/bin/env python3
"""Offline behavioral fixtures for official .NET selection and bound repair receipts."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'app/Build/Products/IPAInspection/runtime-repair-tests'


class RuntimeRepairTests(unittest.TestCase):
    def test_official_catalog_and_bound_receipts(self):
        harness = r'''
import Foundation
import CryptoKit
import Darwin
@main struct Main {
 static func main() async throws {
  func check(_ value: @autoclosure () throws -> Bool, _ message: String = "repair fixture") throws {
   if try !value() { throw NSError(domain: message, code: 1) }
  }
  func rejected(_ body: () throws -> Void) throws {
   var failed = false; do { try body() } catch { failed = true }; try check(failed, "unsafe operation accepted")
  }
  func stage(_ text: String) { FileHandle.standardError.write(Data((text + "\n").utf8)) }
  let requirement = RuntimePreflight.Requirement(family: .desktop, version: .init(major: 6, minor: 0, patch: 0), architecture: .x64, rollForward: .latestPatch, applyPatches: true)
  try check(try RuntimeRepair.metadataURL(for: requirement).absoluteString == "https://builds.dotnet.microsoft.com/dotnet/release-metadata/6.0/releases.json")
  for url in ["http://builds.dotnet.microsoft.com/a", "https://evil.example/a", "https://user:secret@builds.dotnet.microsoft.com/a", "https://builds.dotnet.microsoft.com:8443/a", "https://builds.dotnet.microsoft.com/a#x"] {
   try check(!RuntimeRepair.allows(URL(string: url)!))
  }
  try check(RuntimeRepair.allows(URL(string: "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe")!))
  let hash = String(repeating: "a", count: 128)
  func metadata(_ url: String = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe", digest: String? = nil, rid: String = "win-x64") throws -> Data {
   try JSONSerialization.data(withJSONObject: ["releases": [["windowsdesktop": ["version": "6.0.36", "files": [["name": "windowsdesktop-runtime-win-x64.exe", "rid": rid, "url": url, "hash": digest ?? hash]]]]]])
  }
  let package = try RuntimeRepair.resolve(data: metadata(), requirement: requirement)
  try check(package.version.description == "6.0.36" && package.sha512 == hash)
  try rejected { _ = try RuntimeRepair.resolve(data: metadata("https://evil.example/runtime.exe"), requirement: requirement) }
  try rejected { _ = try RuntimeRepair.resolve(data: metadata(digest: "bad"), requirement: requirement) }
  try rejected { _ = try RuntimeRepair.resolve(data: metadata(rid: "win-x86"), requirement: requirement) }
  try rejected { _ = try RuntimeRepair.resolve(data: Data(repeating: 32, count: RuntimeRepair.metadataLimit + 1), requirement: requirement) }
  try rejected { _ = try RuntimeRepair.resolve(data: metadata("https://builds.dotnet.microsoft.com/a.msi"), requirement: requirement) }
  try rejected { _ = try RuntimeRepair.resolve(data: metadata("https://builds.dotnet.microsoft.com/dotnet/Sdk/6.0.428/dotnet-sdk-6.0.428-win-x64.exe"), requirement: requirement) }
  for (family, section, prefix) in [(RuntimePreflight.Family.netCore, "runtime", "dotnet-runtime"), (.aspnet, "aspnetcore-runtime", "aspnetcore-runtime")] {
   let requested = RuntimePreflight.Requirement(family: family, version: .init(major: 6, minor: 0, patch: 0), architecture: .x86, rollForward: .latestPatch, applyPatches: true)
   let catalog = try JSONSerialization.data(withJSONObject: ["releases": [[section: ["version": "6.0.36", "files": [["name": "\(prefix)-win-x86.exe", "rid": "win-x86", "url": "https://builds.dotnet.microsoft.com/dotnet/Runtime/6.0.36/\(prefix)-6.0.36-win-x86.exe", "hash": hash]]]]]])
   try check(try RuntimeRepair.resolve(data: catalog, requirement: requested).fileName == "\(prefix)-6.0.36-win-x86.exe")
  }
  let framework = RuntimePreflight.Requirement(family: .framework, version: .init(major: 4, minor: 8, patch: 0), architecture: .x64, rollForward: .latestPatch, applyPatches: true)
  try rejected { _ = try RuntimeRepair.metadataURL(for: framework) }
  let fm = FileManager.default
  // The repository may be ExFAT; private POSIX file identity fixtures require internal APFS.
  let root = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("runtime-repair-" + UUID().uuidString)
  try fm.createDirectory(at: root, withIntermediateDirectories: false)
  defer { try? fm.removeItem(at: root) }
  let drive = root.appendingPathComponent("drive_c")
  try fm.createDirectory(at: drive, withIntermediateDirectories: false)
  let payload = Data(repeating: 0xa7, count: 1024 * 1024 + 17) // Cross the streaming hash chunk boundary.
  let digest = SHA512.hash(data: payload).map { String(format: "%02x", $0) }.joined()
  let fixturePackage = RuntimeRepair.Package(requirement: requirement, version: package.version, url: package.url, sha512: digest, fileName: package.fileName)
  stage("repair fixture: trusted anchor boundaries")
  let anchor = try RuntimeRepair.fixtureOpen(root, root: root)
  var anchorInfo = stat()
  try check(fstat(anchor, &anchorInfo) == 0 && anchorInfo.st_mode & S_IFMT == S_IFDIR)
  close(anchor)
  func expectUnsafePath(_ body: () throws -> Void) throws {
   do { try body(); throw NSError(domain: "path boundary accepted", code: 1) }
   catch RuntimeRepair.RepairError.unsafePath {}
  }
  try expectUnsafePath { _ = try RuntimeRepair.fixtureOpen(URL(fileURLWithPath: root.path + "-sibling"), root: root) }
  let linkedPrefix = root.appendingPathComponent("linked-prefix")
  try fm.createSymbolicLink(at: linkedPrefix, withDestinationURL: drive)
  try expectUnsafePath { _ = try RuntimeRepair.fixtureOpen(linkedPrefix, root: root) }
  try expectUnsafePath { _ = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: linkedPrefix, identity: "imported:fixture") }
  try check(try RuntimeRepair.fixturePath(URL(fileURLWithPath: "/var/mobile/Containers/Data/fixture")) == "/private/var/mobile/Containers/Data/fixture")
  do {
   _ = try RuntimeRepair.fixtureOpen(root.appendingPathComponent("missing"), root: root)
   throw NSError(domain: "missing directory accepted", code: 1)
  } catch let RuntimeRepair.RepairError.fileAccess(step, code) {
   try check(step == .openDirectory && code == ENOENT)
   let message = RuntimeRepair.RepairError.fileAccess(step: step, code: code).localizedDescription
   try check(message.contains("step=open-directory") && message.contains("errno=2") && !message.contains(root.path))
  }
  stage("repair fixture: external installer anchor")
  let external = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
  try expectUnsafePath { _ = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: external, identity: "imported:fixture") }
  let redistributables = external.appendingPathComponent("redist")
  try fm.createDirectory(at: redistributables, withIntermediateDirectories: false)
  let localCandidate = redistributables.appendingPathComponent(fixturePackage.fileName)
  try payload.write(to: localCandidate)
  let local = try RuntimeRepair.fixtureLocal(package: fixturePackage, root: external)
  try check(local != nil, "explicit external game root could not find installer")
  if let local { close(local) }
  let retainedRedist = root.appendingPathComponent("retained-redist")
  try fm.moveItem(at: redistributables, to: retainedRedist)
  try fm.createSymbolicLink(at: redistributables, withDestinationURL: retainedRedist)
  try check(try RuntimeRepair.fixtureLocal(package: fixturePackage, root: external) == nil, "installer lookup followed descendant link")
  try fm.removeItem(at: redistributables)
  try fm.createDirectory(at: redistributables, withIntermediateDirectories: false)
  try fm.createSymbolicLink(at: localCandidate, withDestinationURL: retainedRedist.appendingPathComponent(fixturePackage.fileName))
  try check(try RuntimeRepair.fixtureLocal(package: fixturePackage, root: external) == nil, "installer lookup followed file link")
  // Match LibraryModel.drive: the directory already exists, its URL carries a
  // directory slash, and Foundation resolves the protected container spelling.
  let productionDrive = root.appendingPathComponent("drive_c", isDirectory: true).resolvingSymlinksInPath()
  stage("repair fixture: create plan with production directory URL")
  let plan = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: productionDrive, identity: "epic:account:game")
  let installerHandle = try FileHandle(forWritingTo: plan.installerURL(at: 0))
  try installerHandle.write(contentsOf: payload); try installerHandle.close()
  try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: plan.installerURL(at: 0).path)
  stage("repair fixture: validate production directory URL")
  try RuntimeRepair.validate(plan: plan, identity: "epic:account:game", drive: productionDrive)
  // Equivalent non-directory URL spelling must bind to the same prepared plan.
  try RuntimeRepair.validate(plan: plan, identity: "epic:account:game", drive: drive)
  try rejected { try RuntimeRepair.validate(plan: plan, identity: "steam:game", drive: drive) }
  let script = try String(contentsOf: plan.scriptURL, encoding: .utf8)
  try check(script.contains("/install /quiet /norestart"))
  try check(!script.contains("epic:account") && !script.contains("madeira-games") && !script.contains(" -f"))
  let scriptLines = script.components(separatedBy: "\r\n")
  let bootstrap = #"start "" /b "C:\windows\system32\services.exe""#
  let shutdown = #""C:\windows\system32\wineboot.exe" -k -s"#
  try check(scriptLines.filter { $0 == bootstrap }.count == 1)
  try check(scriptLines.filter { $0 == shutdown }.count == 1)
  let beginIndex = scriptLines.firstIndex { $0.contains(" echo BEGIN ") }!
  let bootstrapIndex = scriptLines.firstIndex(of: bootstrap)!
  let installerIndex = scriptLines.firstIndex { $0.contains("/install /quiet /norestart") }!
  let exitIndex = scriptLines.firstIndex { $0.contains(" echo EXIT ") }!
  let endIndex = scriptLines.firstIndex { $0.contains(" echo END ") }!
  let shutdownIndex = scriptLines.firstIndex(of: shutdown)!
  try check(beginIndex < bootstrapIndex && bootstrapIndex < installerIndex)
  try check(installerIndex < exitIndex && exitIndex < endIndex && endIndex < shutdownIndex)
  try check(scriptLines[shutdownIndex + 1] == "exit /b 0")
  func receipt(_ exit: Int = 0, _ nonce: String? = nil, _ end: Bool = true) -> Data {
   let id = nonce ?? plan.nonce
   return Data(("BEGIN \(id) 1\nSTART \(id) 0\nEXIT \(id) 0 \(exit)\n" + (end ? "END \(id) 1\n" : "")).utf8)
  }
  try check(try RuntimeRepair.parseReceipt(receipt(), plan: plan) == .completed(restartRequired: false))
  try check(try RuntimeRepair.parseReceipt(receipt(3010), plan: plan) == .completed(restartRequired: true))
  try check(try RuntimeRepair.parseReceipt(receipt(1641), plan: plan) == .completed(restartRequired: true))
  try check(try RuntimeRepair.parseReceipt(receipt(1603), plan: plan) == .failed(index: 0, exitCode: 1603))
  try check(try RuntimeRepair.parseReceipt(receipt(0, nil, false), plan: plan) == .interrupted)
  try rejected { _ = try RuntimeRepair.parseReceipt(receipt(0, UUID().uuidString), plan: plan) }
  try rejected { _ = try RuntimeRepair.parseReceipt(receipt() + receipt(), plan: plan) }
  try rejected { _ = try RuntimeRepair.parseReceipt(Data(String(data: receipt(), encoding: .utf8)!.replacingOccurrences(of: "EXIT \(plan.nonce) 0 0", with: "EXIT \(plan.nonce) 1 0").utf8), plan: plan) }
  try rejected { _ = try RuntimeRepair.parseReceipt(Data(String(data: receipt(), encoding: .utf8)!.replacingOccurrences(of: "END \(plan.nonce) 1", with: "END \(plan.nonce) 2").utf8), plan: plan) }
  func writeReceipt(_ data: Data) throws {
   let handle = try FileHandle(forWritingTo: plan.resultURL); defer { try? handle.close() }
   try handle.truncate(atOffset: 0); try handle.write(contentsOf: data)
  }
  stage("repair fixture: read bound receipt")
  try writeReceipt(receipt())
  try check(try RuntimeRepair.result(for: plan, identity: "epic:account:game", drive: productionDrive) == .completed(restartRequired: false))
  try rejected { try RuntimeRepair.validate(plan: plan, identity: "epic:account:game", drive: drive) }
  try writeReceipt(Data())
  let damaged = try FileHandle(forWritingTo: plan.installerURL(at: 0))
  try damaged.truncate(atOffset: 0); try damaged.write(contentsOf: payload + Data([0])); try damaged.close()
  do {
   try RuntimeRepair.validate(plan: plan, identity: "epic:account:game", drive: drive)
   throw NSError(domain: "same inode corrupted payload accepted", code: 1)
  } catch RuntimeRepair.RepairError.integrity {} // Must reach the SHA512 gate.
  stage("repair fixture: retry and cancellation")
  let retry = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: drive, identity: "epic:account:game")
  try check(retry.nonce != plan.nonce && retry.directoryPath != plan.directoryPath)
  let linkedDrive = root.appendingPathComponent("linked-drive")
  try fm.createDirectory(at: linkedDrive, withIntermediateDirectories: false)
  try fm.createSymbolicLink(at: linkedDrive.appendingPathComponent(".madeira-runtime-repairs"), withDestinationURL: root)
  try rejected { _ = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: linkedDrive, identity: "imported:fixture") }
  for _ in 0..<2 {
   let cancelled = Task {
    withUnsafeCurrentTask { $0?.cancel() }
    return try await RuntimeRepair.prepare(requirements: [requirement], gameRoot: root, drive: drive, identity: "epic:account:game", progress: { _ in })
   }
   do { _ = try await cancelled.value; throw NSError(domain: "cancelled repair returned a plan", code: 1) }
   catch is CancellationError {} // Repeating proves cancellation releases the single preparation gate.
  }
  stage("repair fixture: cleanup only unlaunched owned files")
  let cleanup = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: drive, identity: "epic:account:game")
  // Partial installer bytes must be reclaimable after cancellation; the shared verified cache survives.
  if !fm.fileExists(atPath: cleanup.installerURL(at: 0).path) { try Data().write(to: cleanup.installerURL(at: 0)) }
  let partial = try FileHandle(forWritingTo: cleanup.installerURL(at: 0)); try partial.write(contentsOf: payload); try partial.close()
  try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cleanup.installerURL(at: 0).path)
  let cache = URL(fileURLWithPath: cleanup.directoryPath).deletingLastPathComponent().appendingPathComponent("cache")
  try fm.createDirectory(at: cache, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
  let cached = cache.appendingPathComponent(digest + ".exe"); try payload.write(to: cached)
  let cleanupTask = Task {
   withUnsafeCurrentTask { $0?.cancel() }
   try RuntimeRepair.cleanupUnlaunched(plan: cleanup, identity: "epic:account:game", drive: productionDrive)
  }
  try await cleanupTask.value
  try check(!fm.fileExists(atPath: cleanup.directoryPath) && fm.fileExists(atPath: cached.path))
  func fresh() throws -> RuntimeRepair.Plan {
   let plan = try RuntimeRepair.makePlan(packages: [fixturePackage], drive: drive, identity: "epic:account:game")
   let writer = try FileHandle(forWritingTo: plan.installerURL(at: 0))
   try writer.write(contentsOf: payload); try writer.close()
   try RuntimeRepair.validate(plan: plan, identity: "epic:account:game", drive: drive)
   try check(try RuntimeRepair.result(for: plan, identity: "epic:account:game", drive: drive) == .interrupted)
   return plan
  }
  let foreign = try fresh(), foreignURL = URL(fileURLWithPath: foreign.directoryPath).appendingPathComponent("keep.txt")
  try Data("KEEP".utf8).write(to: foreignURL)
  try rejected { try RuntimeRepair.cleanupUnlaunched(plan: foreign, identity: "epic:account:game", drive: drive) }
  try check(fm.fileExists(atPath: foreign.scriptURL.path) && fm.fileExists(atPath: foreignURL.path))
  let started = try fresh(), startedHandle = try FileHandle(forWritingTo: started.resultURL)
  try startedHandle.write(contentsOf: Data("BEGIN \(started.nonce) 1\n".utf8)); try startedHandle.close()
  try rejected { try RuntimeRepair.cleanupUnlaunched(plan: started, identity: "epic:account:game", drive: drive) }
  try check(fm.fileExists(atPath: started.scriptURL.path))
  for name in ["repair.cmd", "receipt.txt", "plan.json", "000.exe"] {
   let replaced = try fresh(), file = URL(fileURLWithPath: replaced.directoryPath).appendingPathComponent(name)
   if !fm.fileExists(atPath: file.path) { try Data().write(to: file) }
   let bytes = try Data(contentsOf: file)
   let kept = root.appendingPathComponent(UUID().uuidString)
   try fm.moveItem(at: file, to: kept) // Keep the original inode allocated.
   try bytes.write(to: file); try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
   try rejected { try RuntimeRepair.validate(plan: replaced, identity: "epic:account:game", drive: drive) }
   if name != "000.exe" {
    try rejected { _ = try RuntimeRepair.result(for: replaced, identity: "epic:account:game", drive: drive) }
   }
   try rejected { try RuntimeRepair.cleanupUnlaunched(plan: replaced, identity: "epic:account:game", drive: drive) }
   try check(fm.fileExists(atPath: file.path) && fm.fileExists(atPath: replaced.scriptURL.path))
  }
  let replacedDirectory = try fresh(), originalDirectory = URL(fileURLWithPath: replacedDirectory.directoryPath)
  let retained = root.appendingPathComponent("retained-plan")
  try fm.moveItem(at: originalDirectory, to: retained)
  try fm.copyItem(at: retained, to: originalDirectory)
  try rejected { try RuntimeRepair.validate(plan: replacedDirectory, identity: "epic:account:game", drive: drive) }
  try rejected { _ = try RuntimeRepair.result(for: replacedDirectory, identity: "epic:account:game", drive: drive) }
  try rejected { try RuntimeRepair.cleanupUnlaunched(plan: replacedDirectory, identity: "epic:account:game", drive: drive) }
  try check(fm.fileExists(atPath: replacedDirectory.scriptURL.path))
  print("PASS official metadata, host restrictions, architecture, streaming hash and bound receipt cases")
 }
}
'''
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='scratch-', dir=ARTIFACTS) as scratch:
            scratch = Path(scratch)
            source = scratch / 'main.swift'
            source.write_text(harness)
            # Same-file extension grants fixture access to private boundaries without
            # rewriting their production bodies or exposing a shipping test API.
            repair_source = scratch / 'RuntimeRepair.swift'
            repair_source.write_text((ROOT / 'app/Madeira/RuntimeRepair.swift').read_text() + r'''
extension RuntimeRepair {
 static func fixtureOpen(_ target: URL, root: URL) throws -> Int32 {
  try openDirectory(target, trustedRoot: root)
 }
 static func fixtureLocal(package: Package, root: URL) throws -> Int32? {
  try localInstaller(package: package, root: root)
 }
 static func fixturePath(_ target: URL) throws -> String { try directoryPath(target).path }
}
''')
            binary = scratch / 'test'
            result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1',
                                     str(ROOT / 'app/Madeira/RuntimePreflight.swift'),
                                     str(repair_source), str(source), '-o', str(binary)],
                                    capture_output=True, text=True, timeout=120)
            (ARTIFACTS / 'compile.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tempfile.TemporaryDirectory(prefix='.madeira-runtime-repair-', dir=Path.home()) as internal, \
                    tempfile.TemporaryDirectory(prefix='madeira-external-installer-', dir='/private/tmp') as external:
                # dyld needs '/'; deny an ancestor above the permitted home instead.
                # The helper must open its trusted home anchor directly.
                result = subprocess.run(['/usr/bin/sandbox-exec', '-p',
                                         '(version 1)(allow default)(deny file-read-data (literal '
                                         + json.dumps(str(Path.home().parent)) + '))',
                                         str(binary), internal, external], capture_output=True, text=True, timeout=30)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
