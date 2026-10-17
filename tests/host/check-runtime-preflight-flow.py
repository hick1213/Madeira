#!/usr/bin/env python3
"""Execute the real preflight coordinator with controlled asynchronous boundaries."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'issue/epic-retry-20261014/runtime-preflight/flow'
HARNESS = r'''
import Foundation
import Combine

@MainActor final class Gate {
 var entered = false
 var continuation: CheckedContinuation<Void, Never>?
 func wait() async { entered = true; await withCheckedContinuation { continuation = $0 } }
 func open() { continuation?.resume(); continuation = nil }
}
@main struct Main {
 @MainActor static func main() async throws {
  func check(_ value: Bool, _ message: String) throws { if !value { throw NSError(domain: message, code: 1) } }
  func settle(_ label: String = "flow", _ predicate: @escaping () -> Bool) async throws {
   for _ in 0..<2000 { if predicate() { return }; try await Task.sleep(nanoseconds: 1_000_000) }
   throw NSError(domain: "\(label) did not settle", code: 1)
  }
  let root = URL(fileURLWithPath: "/fixture")
  let ctx = RuntimePreflightController.Context(id: UUID(), title: "Fixture", identity: "epic:account:game", executable: root.appendingPathComponent("Game.exe"), gameRoot: root, drive: root, prefix: root)
  let requirement = RuntimePreflight.Requirement(family: .desktop, version: .init(major: 6, minor: 0, patch: 0), architecture: .x64)
  func report(_ state: RuntimePreflight.State) -> RuntimePreflight.Report {
   .init(findings: [.init(id: "desktop", requirement: requirement, state: state, message: "fixture")])
  }
  let plan = RuntimeRepair.Plan(identityHash: "fixture", nonce: "nonce", drivePath: root.path, directoryPath: root.path, packages: [], driveDevice: 0, driveInode: 0, rootInode: 0, directoryInode: 0, scriptInode: 0, receiptInode: 0, installerInodes: [], manifestInode: 0)
  var scanned = report(.missing), receipt = RuntimeRepair.Result.completed(restartRequired: false)
  var identity = true, ownership = true, proceeded = 0, cancelled = 0, installs = 0, validationFails = false
  var scanGate: Gate?, prepareGate: Gate?, cleanupGate: Gate?
  var cleaned = 0, scans = 0, preparations = 0
  let operations = RuntimePreflightController.Operations(scan: { _ in
   scans += 1
   if let scanGate { await scanGate.wait() }; return scanned
  }, prepare: { _, _, progress in
   preparations += 1
   if let prepareGate { await prepareGate.wait() }; progress("fixture downloaded"); return plan
  }, validate: { _, _ in if validationFails { throw NSError(domain: "invalid plan", code: 1) } }, result: { _, _ in receipt }, cleanup: { _, _ in
   cleaned += 1; if let cleanupGate { await cleanupGate.wait() }
  })
  var diagnostics: [String] = []
  var model = RuntimePreflightController(operations: operations, diagnostic: { diagnostics.append($0) })
  func begin(_ context: RuntimePreflightController.Context? = nil) { model.begin(context: context ?? ctx, valid: { ownership && identity }, identityValid: { identity }, proceed: { proceeded += 1 }, cancel: { cancelled += 1 }, install: { _ in installs += 1 }) }
  begin(); try await settle { model.phase == .needsRepair }
  try check(model.canRepair && model.canContinue && proceeded == 0, "missing dependency waits for explicit action")
  model.cancel(); try await settle { cancelled == 1 }
  try check(proceeded == 0, "cancel never launches game")
  // Cancellation must retain the lease until the worker has actually drained.
  prepareGate = Gate(); cleanupGate = Gate(); begin(); try await settle { model.phase == .needsRepair }; model.repair()
  try await settle { prepareGate!.entered }; model.cancel(); await Task.yield()
  try check(cancelled == 1, "prepare cancellation cannot release external lease early")
  prepareGate!.open(); try await settle { cleanupGate!.entered }
  try check(cancelled == 1, "cleanup must drain before external lease release")
  cleanupGate!.open(); try await settle { cancelled == 2 }; prepareGate = nil; cleanupGate = nil
  try check(cleaned == 1, "successfully prepared stale plan must be cleaned")
  try check(installs == 0, "stale preparation completion cannot launch installer")
  scanGate = Gate(); begin(); try await settle { scanGate!.entered }; identity = false; scanGate!.open()
  try await settle { cancelled == 3 }; scanGate = nil; identity = true
  try check(proceeded == 0, "account change rejects scan result")
  begin(); try await settle { model.phase == .needsRepair }; validationFails = true; model.repair()
  try await settle { model.phase == .failed }; try check(installs == 0 && cleaned == 2, "invalid plan never starts Wine and is cleaned")
  try check(diagnostics.contains { $0.contains("phase=failed") && $0.contains("修复准备失败") }, "repair preparation failure is logged without depending on a visible sheet")
  try check(!diagnostics.joined().contains(ctx.identity), "diagnostics omit provider identity")
  validationFails = false; model.cancel(); try await settle { cancelled == 4 }
  // A completed exit receipt is insufficient while the runtime is still missing.
  begin(); try await settle { model.phase == .needsRepair }; model.repair(); try await settle { installs == 1 }
  model.installerStarted(); model.cancel(); await Task.yield()
  try check(cancelled == 4 && !model.isPresented, "installer dismissal retains source lease until Wine stops")
  ownership = false; await model.installationEnded()
  try check(model.phase == .failed && model.requiresRestart && proceeded == 0, "exit zero needs fresh satisfied scan and restart")
  try check(!model.canContinue && !model.canRepair, "consumed session blocks launch and repeat installer")
  model.cancel(); try await settle { cancelled == 5 }; ownership = true
  model = RuntimePreflightController(operations: operations)
  // Bad/incomplete receipt remains failure, including apparent satisfied files.
  begin(); try await settle { model.phase == .needsRepair }; model.repair(); try await settle { installs == 2 }
  model.installerStarted(); receipt = .interrupted; scanned = report(.satisfied); await model.installationEnded()
  try check(model.phase == .failed && model.requiresRestart && proceeded == 0, "incomplete receipt cannot become repaired")
  model.cancel(); try await settle { cancelled == 6 }; scanned = report(.missing); receipt = .completed(restartRequired: true)
  model = RuntimePreflightController(operations: operations)
  begin(); try await settle { model.phase == .needsRepair }; model.repair(); try await settle { installs == 3 }
  model.installerStarted(); scanned = report(.satisfied); await model.installationEnded()
  try check(model.phase == .finished && model.requiresRestart && proceeded == 0, "repair requires restart and never auto-launches game")
  model.cancel(); try await settle { cancelled == 7 }
  // Dismissing completion is not equivalent to restarting the app.
  begin(); try await settle { cancelled == 8 }
  try check(model.requiresRestart && !model.canContinue && !model.canRepair && installs == 3 && proceeded == 0,
            "new request in same app still requires restart")
  model = RuntimePreflightController(operations: operations)
  begin(); try await settle { proceeded == 1 }
  try check(!model.isPresented, "initial satisfied scan continues automatically")
  model = RuntimePreflightController(operations: operations)
  var inspection = ctx; inspection.inspectionOnly = true; begin(inspection)
  try await settle { model.phase == .finished }
  try check(model.isPresented && model.canContinue && proceeded == 1, "manual inspection keeps clear report visible")
  model.launchAnyway(); await Task.yield()
  try check(model.isPresented && model.canContinue && proceeded == 1,
            "inspection cannot turn its unchecked Cloud state into a game launch")
  model.cancel(); try await settle { cancelled == 9 }
  model = RuntimePreflightController(operations: operations); scanned = report(.missing)
  begin(); try await settle { model.phase == .needsRepair }; model.repair(); try await settle { installs == 4 }
  model.installationFailedToStart("fixture declined before Wine", sessionConsumed: false)
  model.cancel(); try await settle { cancelled == 10 }
  try check(cleaned == 3, "unlaunched ready plan is cleaned when failed launch is cancelled")
  prepareGate = Gate(); begin(); try await settle { model.phase == .needsRepair }; model.repair()
  try await settle { prepareGate!.entered }; identity = false; prepareGate!.open()
  try await settle { cancelled == 11 }; prepareGate = nil; identity = true
  try check(cleaned == 4 && installs == 4, "account change after successful prepare cleans stale plan")
  // JIT cancellation can end launch ownership before any Wine session starts.
  model = RuntimePreflightController(operations: operations)
  begin(); try await settle { model.phase == .needsRepair }; model.repair(); try await settle { installs == 5 }
  ownership = false
  model.installationFailedToStart("JIT preparation cancelled", sessionConsumed: false)
  try check(!model.requiresRestart && !model.canContinue && !model.canRepair,
            "ended preparation cannot offer continue or repair before a Wine session is consumed")
  let priorScans = scans, priorPreparations = preparations
  model.recheck(); model.repair(); model.launchAnyway(); await Task.yield()
  try check(model.phase == .failed && scans == priorScans && preparations == priorPreparations && proceeded == 1,
            "stale retry actions cannot scan, prepare, or launch")
  model.cancel(); try await settle { cancelled == 12 }; ownership = true
  try check(cleaned == 5, "stale preparation can still cancel and clean its unlaunched plan")
  // Explicit install-and-setup consent may consume one installer, never a game launch.
  var setupContext = ctx; setupContext.inspectionOnly = true; setupContext.repairAutomatically = true
  var setupReport = report(.missing), setupEvents: [String] = []
  var setupIdentity = true, setupOwnership = true, setupForeground = true, setupPrepareFails = false
  var setupInstalls = 0, setupProceeded = 0, setupCancelled = 0, setupCleaned = 0
  var setupScanGate: Gate?, setupPrepareGate: Gate?, setupValidateGate: Gate?, setupCleanupGate: Gate?
  let setupOperations = RuntimePreflightController.Operations(scan: { _ in
   setupEvents.append("scan"); if let setupScanGate { await setupScanGate.wait() }; return setupReport
  }, prepare: { requirements, _, _ in
   try check(requirements == [requirement], "setup prepares only the detected supported missing requirement")
   setupEvents.append("prepare"); if let setupPrepareGate { await setupPrepareGate.wait() }
   if setupPrepareFails { throw NSError(domain: "preparation failed", code: 1) }; return plan
  }, validate: { _, _ in
   setupEvents.append("validate"); if let setupValidateGate { await setupValidateGate.wait() }
  }, result: { _, _ in .completed(restartRequired: false) }, cleanup: { _, _ in
   setupCleaned += 1; if let setupCleanupGate { await setupCleanupGate.wait() }
  })
  var setup = RuntimePreflightController(operations: setupOperations)
  func beginSetup(_ context: RuntimePreflightController.Context? = nil) {
   setup.begin(context: context ?? setupContext, valid: { setupOwnership && setupIdentity && setupForeground }, identityValid: { setupIdentity },
               proceed: { setupProceeded += 1 }, cancel: { setupCancelled += 1 },
               install: { _ in setupEvents.append("install"); setupInstalls += 1 })
  }
  beginSetup(); try await settle("automatic setup installer") { setupInstalls == 1 }
  try check(setupEvents == ["scan", "prepare", "validate", "install"] && setupProceeded == 0,
            "explicit setup consent runs scan, prepare and validation before exactly one installer")
  setup.installerStarted(); setupReport = report(.satisfied); await setup.installationEnded()
  try check(setup.phase == .finished && setup.requiresRestart && setupInstalls == 1 && setupProceeded == 0,
            "automatic setup verifies once and requires restart without launching a game")
  setup.cancel(); try await settle("automatic setup completion dismissal") { setupCancelled == 1 }
  beginSetup(); try await settle("consumed setup session") { setupCancelled == 2 }
  try check(setupInstalls == 1, "automatic setup cannot reuse a consumed Wine session")
  // Satisfied, unknown and unsupported results must not invent an installer request.
  for state in [RuntimePreflight.State.satisfied, .unknown, .unsupported] {
   setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupReport = report(state)
   beginSetup(); try await settle("setup without supported missing dependency") { setup.canContinue }
   try check(setupEvents == ["scan"] && setupInstalls == 1 && setupProceeded == 0,
             "setup without a supported missing dependency starts neither installer nor game")
   let previousCancelled = setupCancelled; setup.cancel(); try await settle { setupCancelled == previousCancelled + 1 }
  }
  // An opt-in on a play context is ignored; the existing manual repair gate remains.
  setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupReport = report(.missing)
  var playContext = setupContext; playContext.inspectionOnly = false
  beginSetup(playContext); try await settle("play context manual repair gate") { setup.canRepair }
  try check(setupEvents == ["scan"] && setupInstalls == 1, "automatic repair requires an inspection-only setup context")
  var previousCancelled = setupCancelled; setup.cancel(); try await settle { setupCancelled == previousCancelled + 1 }
  // A failed automatic preparation stays failed, and recheck never retries installation.
  setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupPrepareFails = true
  beginSetup(); try await settle("failed automatic preparation") { setup.phase == .failed && setup.canContinue }
  try check(setupEvents == ["scan", "prepare"] && setupInstalls == 1, "failed automatic preparation stops without retry")
  setupPrepareFails = false; setup.recheck(); try await settle("manual recheck after automatic failure") { setup.canRepair }
  try check(setupEvents == ["scan", "prepare", "scan"] && setupInstalls == 1,
            "rechecking failed automatic setup does not create an installer loop")
  previousCancelled = setupCancelled; setup.cancel(); try await settle { setupCancelled == previousCancelled + 1 }
  // Account change during the initial scan must stop before preparing any runtime.
  setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupScanGate = Gate()
  beginSetup(); try await settle("setup initial scan gate") { setupScanGate!.entered }; setupIdentity = false; setupScanGate!.open()
  previousCancelled = setupCancelled; try await settle("stale setup scan cancellation") { setupCancelled == previousCancelled + 1 }
  try check(setupEvents == ["scan"] && setupInstalls == 1, "stale setup scan cannot prepare or install")
  setupScanGate = nil; setupIdentity = true
  // Cancellation after preparation keeps ownership until the stale plan cleanup drains.
  setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupPrepareGate = Gate(); setupCleanupGate = Gate()
  beginSetup(); try await settle("setup preparation gate") { setupPrepareGate!.entered }; previousCancelled = setupCancelled
  setup.cancel(); setupPrepareGate!.open(); try await settle("setup cancellation cleanup gate") { setupCleanupGate!.entered }
  try check(setupCancelled == previousCancelled && setupInstalls == 1, "automatic setup cancellation retains its source during cleanup")
  setupCleanupGate!.open(); try await settle("setup cancellation cleanup drain") { setupCancelled == previousCancelled + 1 }
  try check(setupCleaned == 1 && setupEvents == ["scan", "prepare"], "cancelled automatic preparation is cleaned without validation or installation")
  setupPrepareGate = nil; setupCleanupGate = nil
  // Identity and ownership are checked again after validation, before the install callback.
  setup = RuntimePreflightController(operations: setupOperations); setupEvents = []; setupValidateGate = Gate()
  beginSetup(); try await settle("setup validation gate") { setupValidateGate!.entered }; setupOwnership = false; setupValidateGate!.open()
  previousCancelled = setupCancelled; try await settle("stale setup validation cancellation") { setupCancelled == previousCancelled + 1 }
  try check(setupInstalls == 1 && setupCleaned == 2 && setupProceeded == 0,
            "ownership change after setup validation cleans the plan without starting an installer")
  setupValidateGate = nil; setupOwnership = true
  // Install-and-setup validity also includes being foreground before Wine starts.
  for stage in ["scan", "prepare", "validate"] {
   setup = RuntimePreflightController(operations: setupOperations); setupEvents = []
   setupScanGate = stage == "scan" ? Gate() : nil
   setupPrepareGate = stage == "prepare" ? Gate() : nil
   setupValidateGate = stage == "validate" ? Gate() : nil
   setupCleanupGate = stage == "scan" ? nil : Gate()
   let gate = (setupScanGate ?? setupPrepareGate ?? setupValidateGate)!
   beginSetup(); try await settle("foreground setup \(stage) gate") { gate.entered }
   previousCancelled = setupCancelled; let previousCleaned = setupCleaned
   setupForeground = false; gate.open()
   if let cleanup = setupCleanupGate {
    try await settle("background setup \(stage) cleanup gate") { cleanup.entered }
    try check(setupCancelled == previousCancelled && setupInstalls == 1,
              "background setup must drain its prepared plan before releasing ownership")
    cleanup.open()
   }
   try await settle("background setup \(stage) release") { setupCancelled == previousCancelled + 1 }
   let expectedEvents = stage == "scan" ? ["scan"] : stage == "prepare" ? ["scan", "prepare"] : ["scan", "prepare", "validate"]
   try check(setupEvents == expectedEvents && setupInstalls == 1 && setupProceeded == 0,
             "background transition during setup \(stage) must not start an installer or game")
   try check(setupCleaned == previousCleaned + (stage == "scan" ? 0 : 1),
             "background setup cleans only the plan that was actually prepared")
   setupForeground = true; setupScanGate = nil; setupPrepareGate = nil; setupValidateGate = nil; setupCleanupGate = nil
  }
  print("PASS cancellation drain, identity, stale callbacks, invalid plan, missing-after-zero, receipt and restart gates")
  print("PASS opt-in setup, one installer, no game launch, no retry loop and stale setup cancellation")
 }
}
'''


class RuntimePreflightFlowTests(unittest.TestCase):
    def test_production_flow(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='flow-', dir=ARTIFACTS) as directory:
            directory = Path(directory)
            source = directory / 'Main.swift'
            source.write_text(HARNESS)
            binary = directory / 'flow'
            compile_result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1',
                str(ROOT / 'app/Madeira/RuntimePreflight.swift'),
                str(ROOT / 'app/Madeira/RuntimeRepair.swift'),
                str(ROOT / 'app/Madeira/RuntimePreflightController.swift'), str(source), '-o', str(binary)],
                capture_output=True, text=True, timeout=180)
            (ARTIFACTS / 'compile.txt').write_text(compile_result.stdout + compile_result.stderr)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
