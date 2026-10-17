#!/usr/bin/env python3
"""Run the original runtime-installer UI routing prefix with device path aliases.

Only the synchronous path-to-installer-profile boundary is exercised. Synthetic iOS
paths are never created or opened, and no Wine, UI, device, or network runs.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'issue/epic-retry-20261015/bg3-runtime-repair/routing'
HARNESS = r'''
import Foundation
import CryptoKit
import Darwin

enum LibraryModel {
 static var drive = URL(fileURLWithPath: "/unused-runtime-routing-fixture")
}
struct LibraryEntry {
 var title: String
 var relativePath: String
 var bits: Int
 var launchMode: String?
 var config: String?
}
enum RuntimePreflightController {
 struct Context {
  let drive: URL
  let identity: String
 }
}
final class PreflightSpy {
 var isPresented = true
 var failures: [String] = []
 var consumed: [Bool] = []
 var cancellations = 0
 func installationFailedToStart(_ message: String, sessionConsumed: Bool) {
  failures.append(message); consumed.append(sessionConsumed)
 }
 func cancel() { cancellations += 1 }
}
final class Runner {
 let runtimePreflight = PreflightSpy()
 var capturedRelative: String?
 var capturedConfig: String?
 var capturedProfile: LibraryEntry?
__PRODUCTION_PREFIX__
        capturedRelative = relative
        capturedConfig = profile.config
        capturedProfile = profile
    }
 func invoke(_ plan: RuntimeRepair.Plan, valid: Bool = true) {
  startRuntimeRepair(plan, context: .init(drive: LibraryModel.drive, identity: "routing-fixture"),
                     preparation: UUID(), valid: { valid })
 }
}

@main struct Main {
 static func main() {
  var failures = 0
  func check(_ value: Bool, _ message: String) {
   if !value { failures += 1; print("FAIL: \(message)") }
  }
  func environment(_ config: String?) -> [String: String] {
   var values: [String: String] = [:]
   for line in (config ?? "").split(whereSeparator: \.isNewline) {
    let fields = line.split(separator: "=", maxSplits: 1)
    guard fields.count == 2 else { continue }
    let key = String(fields[0]).trimmingCharacters(in: .whitespaces)
    guard key.hasPrefix("env.") else { continue }
    values[String(key.dropFirst(4))] = String(fields[1]).trimmingCharacters(in: .whitespaces)
   }
   return values
  }
  let alias = "/var/mobile/Containers/Data/Application/31E2FC32-4730-4DF3-9FC6-FAC035A9219E/Documents/wine/drive_c"
  let canonical = "/private" + alias
  let hash = SHA256.hash(data: Data("routing-fixture".utf8)).map { String(format: "%02x", $0) }.joined()
  let nonce = "a9231acd-9877-47e1-846d-84d31284842e"
  let relativeDirectory = ".madeira-runtime-repairs/" + hash + "-" + nonce
  let expectedRelative = relativeDirectory + "/repair.cmd"
  let requirement = RuntimePreflight.Requirement(family: .desktop, version: .init(major: 6, minor: 0, patch: 0), architecture: .x64)
  let package = RuntimeRepair.Package(requirement: requirement, version: .init(major: 6, minor: 0, patch: 36),
    url: URL(string: "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe")!,
    sha512: String(repeating: "f", count: 128), fileName: "windowsdesktop-runtime-6.0.36-win-x64.exe")
  func plan(drive: String? = nil, directory: String? = nil) -> RuntimeRepair.Plan {
   RuntimeRepair.Plan(identityHash: hash, nonce: nonce, drivePath: drive ?? canonical,
    directoryPath: directory ?? canonical + "/" + relativeDirectory, packages: [package],
    driveDevice: 1, driveInode: 2, rootInode: 3, directoryInode: 4, scriptInode: 5,
    receiptInode: 6, installerInodes: [7], manifestInode: 8)
  }
  // Like LibraryModel.drive on an iPad, these nonexistent paths may retain the
  // protected /var spelling even after Foundation's filesystem resolution.
  for path in [canonical, alias] {
   for isDirectory in [false, true] {
    LibraryModel.drive = URL(fileURLWithPath: path, isDirectory: isDirectory)
    let runner = Runner()
    runner.invoke(plan())
    check(runner.capturedRelative == expectedRelative && runner.runtimePreflight.failures.isEmpty,
          "equivalent \(path.hasPrefix("/private/") ? "private" : "var") drive spelling, directory=\(isDirectory), must route the prepared installer")
    let profile = runner.capturedProfile
    check(profile?.relativePath == expectedRelative && profile?.bits == 64 && profile?.launchMode == "desktop"
            && profile?.title == "安装运行库" && !runner.runtimePreflight.isPresented,
          "the original installer profile is captured as a 64-bit desktop entry before Wine starts")
    let installerEnvironment: [String: String] = environment(runner.capturedConfig)
    check(installerEnvironment["MADEIRA_GDI_SHARED_SECTION"] == "1",
          "the installer profile enables the shared GDI table before entering Wine")
    check(installerEnvironment["MADEIRA_MADSYNC_SESSION"] == "0"
            && installerEnvironment["MADEIRA_WAIT_CHILDREN"] == "0",
          "the installer profile preserves its disabled madsync and child-wait settings")
   }
  }
  LibraryModel.drive = URL(fileURLWithPath: canonical + "-other", isDirectory: true)
  let wrongCurrentDrive = Runner()
  wrongCurrentDrive.invoke(plan())
  check(wrongCurrentDrive.capturedRelative == nil && wrongCurrentDrive.runtimePreflight.consumed == [false],
        "a genuinely different current Wine drive must be rejected before an installer is created")
  LibraryModel.drive = URL(fileURLWithPath: canonical, isDirectory: true)
  for outside in [canonical + "-sibling/" + relativeDirectory,
                  "/private/var/mobile/Containers/Data/Application/OTHER/Documents/" + relativeDirectory] {
   let runner = Runner()
   runner.invoke(plan(directory: outside))
   check(runner.capturedRelative == nil && runner.runtimePreflight.consumed == [false],
         "a sibling or outside repair script must not be routed into the current Wine drive")
  }
  let wrongPlanDrive = Runner()
  wrongPlanDrive.invoke(plan(drive: canonical + "-sibling"))
  check(wrongPlanDrive.capturedRelative == nil && wrongPlanDrive.runtimePreflight.consumed == [false],
        "the plan's declared drive must match the active drive as well as its script location")
  let invalidRequest = Runner()
  invalidRequest.invoke(plan(), valid: false)
  check(invalidRequest.capturedRelative == nil && invalidRequest.runtimePreflight.consumed == [false]
          && invalidRequest.runtimePreflight.cancellations == 1,
        "an invalid launch request remains cancelled before path routing")
  print("Runtime repair UI routing checks: \(failures) failure(s)")
  exit(failures == 0 ? 0 : 1)
 }
}
'''


class RuntimeRepairRoutingTests(unittest.TestCase):
    def test_production_installer_prefix_accepts_only_the_same_drive(self):
        source = (ROOT / 'app/Madeira/ContentView.swift').read_text()
        start = source.index('    private func startRuntimeRepair(')
        boundary = source.index('        let start: () -> Void', start)
        # Preserve every production guard, path operation and installer profile.
        # Capture the profile before the asynchronous Wine launch boundary.
        harness = HARNESS.replace('__PRODUCTION_PREFIX__', source[start:boundary])
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='repair-routing-', dir=ARTIFACTS) as directory:
            directory = Path(directory)
            swift = directory / 'Main.swift'
            binary = directory / 'checks'
            swift.write_text(harness)
            compiled = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1',
                str(ROOT / 'app/Madeira/RuntimePreflight.swift'),
                str(ROOT / 'app/Madeira/RuntimeRepair.swift'), str(swift), '-o', str(binary)],
                capture_output=True, text=True, timeout=180)
            (ARTIFACTS / 'compile.txt').write_text(compiled.stdout + compiled.stderr)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
