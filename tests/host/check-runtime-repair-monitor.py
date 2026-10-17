#!/usr/bin/env python3
"""Exercise the actual native repair monitor with gated, draining receipt reads."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'app/Build/Products/IPAInspection/runtime-repair-monitor-tests'
HARNESS = r'''
import Foundation
import Combine

@MainActor final class ReadGate {
 var entered = false
 var drained = false
 var continuation: CheckedContinuation<Void, Never>?
 func wait() async { entered = true; await withCheckedContinuation { continuation = $0 }; drained = true }
 func open() { continuation?.resume(); continuation = nil }
}

@main struct Main {
 @MainActor static func main() async throws {
  func check(_ value: @autoclosure () -> Bool, _ text: String) throws {
   if !value() { throw NSError(domain: text, code: 1) }
  }
  func settle(_ text: String, _ value: () -> Bool) async throws {
   for _ in 0..<6000 { if value() { return }; try await Task.sleep(nanoseconds: 1_000_000) }
   throw NSError(domain: "timeout: " + text, code: 1)
  }
  let root = URL(fileURLWithPath: "/fixture")
  let context = RuntimePreflightController.Context(id: UUID(), title: "Fixture", identity: "PRIVATE_IDENTITY",
   executable: root.appendingPathComponent("game.exe"), gameRoot: root, drive: root, prefix: root)
  let wanted = RuntimePreflight.Requirement(family: .desktop, version: .init(major: 6, minor: 0, patch: 0), architecture: .x64)
  let second = RuntimePreflight.Requirement(family: .netCore, version: .init(major: 8, minor: 0, patch: 0), architecture: .x64)
  let packages = [
   RuntimeRepair.Package(requirement: wanted, version: .init(major: 6, minor: 0, patch: 36), url: URL(string: "https://builds.dotnet.microsoft.com/fixture.exe")!, sha512: String(repeating: "a", count: 128), fileName: "fixture.exe"),
   RuntimeRepair.Package(requirement: second, version: .init(major: 8, minor: 0, patch: 10), url: URL(string: "https://builds.dotnet.microsoft.com/second.exe")!, sha512: String(repeating: "b", count: 128), fileName: "second.exe")
  ]
  let plan = RuntimeRepair.Plan(identityHash: "fixture", nonce: UUID().uuidString.lowercased(), drivePath: root.path,
   directoryPath: root.path, packages: packages, driveDevice: 0, driveInode: 0, rootInode: 0, directoryInode: 0,
   scriptInode: 0, receiptInode: 0, installerInodes: [0, 0], manifestInode: 0)
  func report(_ state: RuntimePreflight.State) -> RuntimePreflight.Report {
   .init(findings: [.init(id: "runtime", requirement: wanted, state: state, message: "fixture")])
  }
  var live = RuntimeRepair.LiveProgress(), readFails = false
  var gate: ReadGate?, resultGate: ReadGate?
  var reads = 0, results = 0, scans = 0, releases = 0, installs = 0
  var identity = true, ownership = true, scanned = report(.missing)
  var diagnostics: [String] = []
  func makeModel(polling: Bool = true) -> RuntimePreflightController {
   let reader: ((RuntimeRepair.Plan, RuntimePreflightController.Context) async throws -> RuntimeRepair.LiveProgress)? = polling ? { _, _ in
    reads += 1
    let captured = live, failure = readFails, held = gate
    if let held { await held.wait() }
    if failure { throw NSError(domain: "PRIVATE_PATH_SHOULD_NOT_APPEAR", code: 99) }
    return captured
   } : nil
   return RuntimePreflightController(operations: .init(scan: { _ in scans += 1; return scanned },
    prepare: { _, _, _ in plan }, validate: { _, _ in }, result: { _, _ in
     results += 1; if let resultGate { await resultGate.wait() }; return .completed(restartRequired: false)
    }, cleanup: { _, _ in }, progress: reader), diagnostic: { diagnostics.append($0) })
  }
  func start(_ model: RuntimePreflightController) async throws {
   let prior = installs
   model.begin(context: context, valid: { identity && ownership }, identityValid: { identity },
    proceed: {}, cancel: { releases += 1 }, install: { _ in installs += 1 })
   try await settle("missing runtime") { model.phase == .needsRepair }
   model.repair()
   try await settle("installer callback") { installs == prior + 1 }
   model.isPresented = false // The launch owner hides the earlier preparation sheet.
   model.installerStarted()
  }

  // Real installer startup opens the native sheet and owns only one reader.
  var model = makeModel()
  gate = ReadGate(); let initialGate = gate!
  try await start(model)
  try check(model.isPresented, "installer startup must present native progress")
  try check(model.installationStartedAt != nil && model.installerCount == 2, "start date and package count are real")
  let startedAt = model.installationStartedAt
  try await settle("initial receipt read") { initialGate.entered }
  let firstReads = reads
  model.installerStarted()
  try check(model.installationStartedAt == startedAt && reads == firstReads, "duplicate startup cannot reset elapsed time or duplicate polling")
  model.cancel()
  try check(!model.isPresented && model.phase == .installing && releases == 0, "dismissal hides progress without releasing running Wine")
  gate = nil; initialGate.open()
  let priorReads = reads
  try await settle("polling survives sheet dismissal") { reads > priorReads }
  live = .init(stage: .installing(index: 0), completed: 0)
  try await settle("selected runtime version") { model.installationProgress == live }
  try check(model.message.contains("6.0.36") && !model.message.contains("6.0.0"), "display selected installer version rather than requested minimum")
  try check(!model.message.contains("%"), "receipt stages do not invent a percentage")
  live = .init(stage: .installing(index: 1), completed: 1, firstFailure: .init(index: 0, exitCode: 1603))
  try await settle("second package and first failure") { model.installationProgress == live }
  try check(model.message.contains("8.0.10") && model.message.contains("1603"), "stage identifies actual selected package and recorded failure")
  live = .init(stage: .finishing, completed: 2)
  try await settle("receipt END") { model.installationProgress == live }
  try check(model.phase == .installing && results == 0 && scans == 1 && releases == 0,
   "END cannot complete repair before Wine stops, result verification and fresh scan")
  let stableDiagnostics = diagnostics.count
  try await Task.sleep(nanoseconds: 1_150_000_000)
  try check(diagnostics.count == stableDiagnostics, "unchanged receipt status must not republish messages each tick")

  // Ending Wine must cancel and drain an in-flight read before result/scan/release.
  gate = ReadGate(); let stopGate = gate!
  try await settle("held final reader") { stopGate.entered }
  scanned = report(.satisfied)
  let ended = Task { await model.installationEnded() }
  try await settle("verifying after stop") { model.phase == .verifying }
  try check(results == 0 && releases == 0, "post-stop result must wait for the receipt read to drain")
  gate = nil; stopGate.open(); await ended.value
  try check(stopGate.drained && results == 1 && scans == 2 && releases == 1 && model.phase == .finished,
   "drained monitor allows authoritative result and fresh satisfied scan")
  let stoppedReads = reads
  try await Task.sleep(nanoseconds: 1_100_000_000)
  try check(reads == stoppedReads, "finished monitor never keeps polling")

  // A transient read failure stays an installing session, omits private errors,
  // and recovers when the bound receipt can be read again.
  scanned = report(.missing); live = .init(); readFails = true
  model = makeModel(); try await start(model)
  try await settle("safe unavailable status") { model.message.contains("进度暂时") }
  try check(model.phase == .installing && releases == 1, "reader failure does not terminate or release installer")
  try check(!model.message.contains("PRIVATE_") && !diagnostics.joined().contains("PRIVATE_"), "monitor diagnostics omit private errors and identity")
  readFails = false; live = .init(stage: .installing(index: 0), completed: 0)
  try await settle("reader recovery") { model.installationProgress == live && model.message.contains("6.0.36") }
  scanned = report(.satisfied); await model.installationEnded()
  try check(model.phase == .finished && releases == 2, "recovered session still requires authoritative verification")

  // An identity change rejects a late completion without releasing active Wine.
  scanned = report(.missing); live = .init(stage: .installing(index: 1), completed: 1)
  model = makeModel(); gate = ReadGate(); let identityGate = gate!
  try await start(model); try await settle("identity read gate") { identityGate.entered }
  identity = false; gate = nil; identityGate.open()
  try await settle("identity reader drained") { identityGate.drained }
  await Task.yield()
  try check(model.installationProgress == .init() && model.phase == .installing && releases == 2,
   "late status from changed identity cannot update progress or release active installer")
  let priorResults = results
  await model.installationEnded()
  try check(model.phase == .failed && results == priorResults && releases == 3, "changed identity blocks result and releases only after Wine stops")
  identity = true

  // Failed startup also drains a non-cancellable reader before releasing its lease.
  live = .init(stage: .installing(index: 0), completed: 0)
  model = makeModel(); gate = ReadGate(); let failureGate = gate!
  try await start(model); try await settle("failed-start gate") { failureGate.entered }
  model.installationFailedToStart("fixture startup failed", sessionConsumed: true)
  try check(model.phase == .failed && releases == 3, "startup failure cannot release an in-flight reader")
  gate = nil; failureGate.open()
  try await settle("failed-start drain release") { releases == 4 }
  try check(model.installationProgress == .init(), "late failed-start completion cannot publish progress")

  // Cancellation during verification drains the reader before external release
  // and never starts result verification after cancellation.
  model = makeModel(); gate = ReadGate(); let cancelGate = gate!
  try await start(model); try await settle("cancel verification reader") { cancelGate.entered }
  let cancelledEnd = Task { await model.installationEnded() }
  try await settle("cancel verification phase") { model.phase == .verifying }
  model.cancel(); await Task.yield()
  try check(releases == 4, "verifying cancellation retains lease while read is blocked")
  let beforeCancelledResult = results
  gate = nil; cancelGate.open(); await cancelledEnd.value
  try await settle("verification cancellation released") { releases == 5 }
  try check(results == beforeCancelledResult && model.installationProgress == .init(), "cancelled stop cannot verify or publish a late progress result")

  // Cancellation after result reading begins must drain that operation as well.
  model = makeModel(); live = .init(); try await start(model)
  resultGate = ReadGate(); let heldResult = resultGate!
  let resultEnd = Task { await model.installationEnded() }
  try await settle("post-stop result gate") { heldResult.entered }
  model.cancel(); await Task.yield()
  try check(releases == 5, "verification result read also retains external lease")
  resultGate = nil; heldResult.open(); await resultEnd.value
  try await settle("result cancellation released") { releases == 6 }

  // Existing injected operations with no reader keep their original behavior.
  scanned = report(.missing); model = makeModel(polling: false)
  let noReaderReads = reads
  try await start(model)
  try check(model.isPresented && model.installationStartedAt != nil && reads == noReaderReads, "optional reader preserves legacy fixtures without polling")
  scanned = report(.satisfied); await model.installationEnded()
  try check(model.phase == .finished && releases == 7, "no-reader session still verifies normally")
  print("PASS native progress stages, actual versions, dismissal, safe recovery, identity and drained cancellation")
 }
}
'''


class RuntimeRepairMonitorTests(unittest.TestCase):
    def test_progress_uses_session_window(self):
        source = (ROOT / 'app/Madeira/ContentView.swift').read_text()
        controls = source[source.index('final class ControlsWindow:'):source.index('enum TouchControlsHost {')]
        self.assertIn('RuntimeInstallerPresentation.shared.model', controls)
        self.assertLess(controls.index('RuntimeInstallerPresentation.shared.model'), controls.index('if m.editing'))
        self.assertIn('repair.isPresented', controls)
        self.assertIn('RuntimeInstallerPresentation.reopenRect', controls)
        overlay = source[source.index('struct TouchControlsOverlay:'):]
        self.assertIn('RuntimeInstallerProgressOverlay(model: model)', overlay)
        self.assertLess(overlay.index('RuntimeInstallerProgressOverlay(model: model)'), overlay.index('LibraryHUD()'))
        self.assertIn('RuntimeInstallerProgressOverlay(model: model)\n                } else if landscape {', overlay)
        self.assertIn('repair.model == nil && !m.editing && !library.blocksGameplayTouch', overlay)
        self.assertIn('.gesture(scalePinch, including: repair.model != nil ? .subviews :', overlay)
        self.assertEqual(source.count('topInset: TouchControlsHost.topInset'), 2)
        self.assertIn('runtimePreflight.isPresented && runtimeInstallerEntry == nil', source)
        self.assertIn('RuntimeInstallerPresentation.shared.model = runtimePreflight', source)
        self.assertIn('RuntimeInstallerPresentation.shared.model = nil', source)

    def test_actual_controller_monitor(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='monitor-', dir=ARTIFACTS) as directory:
            directory = Path(directory)
            source = directory / 'Main.swift'
            source.write_text(HARNESS)
            binary = directory / 'monitor'
            result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1',
                str(ROOT / 'app/Madeira/RuntimePreflight.swift'),
                str(ROOT / 'app/Madeira/RuntimeRepair.swift'),
                str(ROOT / 'app/Madeira/RuntimePreflightController.swift'), str(source), '-o', str(binary)],
                capture_output=True, text=True, timeout=180)
            (ARTIFACTS / 'compile.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=45)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
