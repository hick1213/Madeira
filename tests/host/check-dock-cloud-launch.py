#!/usr/bin/env python3
"""Exercise production Steam launch prefixes and their Cloud permissions.

The original methods run through Cloud gating and stop before launch preparation
begins. Steam options and session state are isolated boundaries; no account,
game, Wine process, device, or network service is accessed.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'issue/epic-retry-20261015/bg3-runtime-repair/dock-cloud'


def declaration(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index('{', start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f'Unclosed declaration: {marker}')


HARNESS = r'''
import Foundation
import Darwin

struct DockGame {
 let id: Int
 let name: String
 let library: String
 let installDir: String
}
struct LibraryEntry {
 let title: String
 var externalLibraryID: UUID? = nil
 var steamAppID: Int? = nil
 var epicAppName: String? = nil
 var epicAccountKey: String? = nil
 var desktop: Bool? = nil
 var startsSteamGameDirectly = true
 var steamProgram: String? = "Fixture.exe"
 var steamProgramSource: String? = "fixture"
 var launchArguments = ""
 var steamWorkingWindowsPath: String? = nil
}
enum LogLevel { case error }
@MainActor final class LogStore {
 static let shared = LogStore()
 var lines: [String] = []
 func log(_ message: String, level: LogLevel? = nil) { lines.append(message) }
}
@MainActor final class LibraryModel {
 struct CloudNotice { let appID: Int }
 static var sessionsThisRun = 0
 static let restartMessage = "restart required"
 var enabled = true
 var cloudBypass: Int?
 var cloudRetry: (() -> Void)?
 var cloudNotice: CloudNotice?
 var current: UUID?
 var error: String?
 var restartNotice: String?
}
@MainActor final class GameSessionWork {
 enum Failure: LocalizedError {
  case savesSyncing
  var errorDescription: String? { "Saves are synchronizing." }
 }
 static let shared = GameSessionWork()
 var isSynchronizingSaves = false
 var preparations: Set<UUID> = []
 var completed: [UUID] = []
 func ownsPreparation(_ id: UUID) -> Bool { preparations.contains(id) }
 func completePreparation(_ id: UUID) { completed.append(id); preparations.remove(id) }
}
struct LaunchOption: Sendable { let launchIndex: UInt32; let program: String }
@MainActor final class SteamOwnedLibrary {
 enum CloudHold: Equatable { case syncing, stale }
 static let shared = SteamOwnedLibrary()
 var hold: CloudHold? = .syncing
 var holdQueries = 0
 var optionQueries = 0
 func cloudHold(_ appID: Int) -> CloudHold? { holdQueries += 1; return hold }
 func launchOptions(appID: Int) async -> [LaunchOption]? {
  optionQueries += 1
  await Task.yield()
  return [LaunchOption(launchIndex: 0, program: "Fixture.exe")]
 }
}
enum SteamDirectStart {
 static func choose(_ options: [LaunchOption], installFolder: URL) -> LaunchOption? { options.first }
}
@MainActor enum MadeiraDock {
 static let drive = URL(fileURLWithPath: "/fixture/drive_c")
 static var installedGames: [DockGame] = []
 static func games(drive: URL) -> [DockGame] { installedGames }
}
@MainActor final class MadeiraDockModel {
 static let shared = MadeiraDockModel()
 let compactPool = false
}
@MainActor final class EpicLaunchLogPrivacy {
 static let shared = EpicLaunchLogPrivacy()
 func set(_ value: String?) {}
}
enum MadeiraConfig { static func flag(_ name: String) -> Bool { true } }
func wine_process_is_running() -> Int32 { 0 }
func wineserver_is_running() -> Int32 { 0 }
func wine_epic_clear_launch() {}

@MainActor final class RunnerState {
 var prepared = 0
 var preparedOptions: [UInt32] = []
 var preparedIDs: [UUID?] = []
 var preparedInspections: [Bool] = []
 var directInspections: [Bool] = []
 var cloudWaits = 0
}
@MainActor struct Runner {
 let library = LibraryModel()
 let logStore = LogStore.shared
 let state = RunnerState()
 var prepared: Int { state.prepared }
 var preparedOptions: [UInt32] { state.preparedOptions }
 var preparedIDs: [UUID?] { state.preparedIDs }
 var preparedInspections: [Bool] { state.preparedInspections }
 var directInspections: [Bool] { state.directInspections }
 func cloudWait(_ appID: Int) { state.cloudWaits += 1 }
 func cloudNotice(_ appID: Int, name: String, hold: SteamOwnedLibrary.CloudHold) -> LibraryModel.CloudNotice {
  LibraryModel.CloudNotice(appID: appID)
 }
__CLOUD_CLEAR__
__LIBRARY_PREFIX__
        state.directInspections.append(inspectionOnly)
    }
__DOCK_PREFIX__
        state.prepared += 1
        state.preparedOptions.append(launchOption)
        state.preparedIDs.append(suppliedPreparation)
        state.preparedInspections.append(inspectionOnly)
    }
 func invoke(_ game: DockGame, preparation: UUID? = nil, launchOption: UInt32? = nil,
             runtimeChecked: Bool = false, inspectionOnly: Bool = false) {
  startDock(game, compactPool: false, preparation: preparation, launchOption: launchOption,
            runtimeChecked: runtimeChecked, inspectionOnly: inspectionOnly)
 }
 func invokeLibrary(_ entry: LibraryEntry, inspectionOnly: Bool) {
  launchLibraryEntry(entry, inspectionOnly: inspectionOnly)
 }
}

@main struct Main {
 @MainActor static func main() async {
  var failures = 0
  func check(_ value: Bool, _ message: String) {
   if !value { failures += 1; print("FAIL: \(message)") }
  }
  func settle(_ label: String, _ predicate: () -> Bool) async {
   for _ in 0..<2000 {
    if predicate() { return }
    try? await Task.sleep(nanoseconds: 1_000_000)
   }
   check(false, "\(label) did not settle")
  }
  let game = DockGame(id: 42, name: "Fixture", library: "steamapps", installDir: "Fixture")
  let steam = SteamOwnedLibrary.shared, work = GameSessionWork.shared
  MadeiraDock.installedGames = [game]
  var directEntry = LibraryEntry(title: game.name)
  directEntry.steamAppID = game.id
  var dockEntry = directEntry
  dockEntry.startsSteamGameDirectly = false
  // A runtime inspection is not a Play request. Neither Steam start route may
  // query Cloud, begin a Cloud retry/wait, or consume an unrelated Play permit.
  // The Dock route also resolves its options asynchronously and recurses.
  for hold in [SteamOwnedLibrary.CloudHold.stale, .syncing] {
   for bypass in [Optional<Int>.none, game.id] {
    steam.hold = hold
    steam.holdQueries = 0
    let direct = Runner()
    direct.library.cloudBypass = bypass
    direct.invokeLibrary(directEntry, inspectionOnly: true)
    let directLabel = "direct inspection with \(hold), bypass=\(bypass != nil)"
    check(direct.directInspections == [true], "\(directLabel) reaches the preflight boundary with inspection intent")
    check(steam.holdQueries == 0 && direct.library.cloudRetry == nil && direct.library.cloudNotice == nil && direct.state.cloudWaits == 0,
          "\(directLabel) leaves Cloud untouched")
    check(direct.library.cloudBypass == bypass, "\(directLabel) preserves a queued Play permission")

    steam.holdQueries = 0
    let optionsBeforeInspection = steam.optionQueries
    let dock = Runner()
    dock.library.cloudBypass = bypass
    dock.invokeLibrary(dockEntry, inspectionOnly: true)
    await settle("Dock inspection option resolution") { dock.prepared > 0 || dock.library.cloudRetry != nil }
    let dockLabel = "Dock inspection with \(hold), bypass=\(bypass != nil)"
    check(dock.prepared == 1 && dock.preparedInspections == [true] && dock.preparedOptions == [0] && dock.directInspections.isEmpty,
          "\(dockLabel) reaches the preflight boundary with inspection intent after option recursion")
    check(steam.optionQueries == optionsBeforeInspection + 1,
          "\(dockLabel) resolves launch options once")
    check(steam.holdQueries == 0 && dock.library.cloudRetry == nil && dock.library.cloudNotice == nil && dock.state.cloudWaits == 0,
          "\(dockLabel) leaves Cloud untouched")
    check(dock.library.cloudBypass == bypass, "\(dockLabel) preserves a queued Play permission")
   }
  }
  // Direct Play retains the Cloud hold, including stale-check wait behavior.
  for hold in [SteamOwnedLibrary.CloudHold.stale, .syncing] {
   steam.hold = hold
   steam.holdQueries = 0
   let direct = Runner()
   direct.invokeLibrary(directEntry, inspectionOnly: false)
   check(direct.directInspections.isEmpty && steam.holdQueries == 1 && direct.library.cloudRetry != nil,
         "direct Play with \(hold) remains held without explicit permission")
   check(direct.state.cloudWaits == (hold == .stale ? 1 : 0) && (direct.library.cloudNotice != nil) == (hold == .syncing),
         "direct Play with \(hold) retains its existing Cloud wait or notice")
  }
  steam.hold = .syncing
  steam.holdQueries = 0
  let directPlay = Runner()
  directPlay.library.cloudBypass = game.id
  directPlay.invokeLibrary(directEntry, inspectionOnly: false)
  check(directPlay.directInspections == [false] && directPlay.library.cloudBypass == nil && steam.holdQueries == 0,
        "direct Play consumes its explicit one-use permission and reaches preflight once")
  directPlay.invokeLibrary(directEntry, inspectionOnly: false)
  check(directPlay.directInspections == [false] && steam.holdQueries == 1 && directPlay.library.cloudNotice?.appID == game.id,
        "a later direct Play request cannot reuse the consumed Cloud permission")
  steam.holdQueries = 0
  steam.optionQueries = 0
  // Launch-option discovery recurses before preparation owns the launch. The
  // original, explicit one-use permission must survive that discovery step.
  let initial = Runner()
  initial.library.cloudBypass = game.id
  initial.invoke(game)
  await settle("launch option resolution") { initial.prepared > 0 || initial.library.cloudNotice != nil }
  check(initial.prepared == 1 && initial.preparedOptions == [0] && initial.preparedIDs == [nil],
        "one Cloud bypass must reach preparation exactly once after asynchronous launch-option resolution")
  check(steam.optionQueries == 1 && steam.holdQueries == 0 && initial.library.cloudNotice == nil,
        "launch-option recursion must not ask Cloud again after the user's bypass")
  check(initial.library.cloudBypass == nil, "the fresh launch consumes its Cloud permission once")
  // The permission must not leak into a later, independent request.
  let beforeFreshAttempt = initial.prepared
  initial.library.cloudNotice = nil
  initial.invoke(game, launchOption: 0)
  check(initial.prepared == beforeFreshAttempt && initial.library.cloudNotice?.appID == game.id && steam.holdQueries == 1,
        "a fresh launch without another Cloud permission remains held")
  // Runtime and JIT callbacks already carry preparation ownership. They must
  // not consume any new permit or re-enter the Cloud warning for that launch.
  steam.holdQueries = 0
  let continuation = Runner(), preparation = UUID()
  work.preparations.insert(preparation)
  continuation.invoke(game, preparation: preparation, launchOption: 0)
  continuation.invoke(game, preparation: preparation, launchOption: 0, runtimeChecked: true)
  check(continuation.prepared == 2 && continuation.preparedIDs == [preparation, preparation] && steam.holdQueries == 0,
        "owned runtime and JIT continuations skip a second Cloud permission check")
  continuation.library.cloudBypass = game.id
  continuation.invoke(game, preparation: preparation, launchOption: 0, runtimeChecked: true)
  check(continuation.prepared == 3 && continuation.library.cloudBypass == game.id && steam.holdQueries == 0,
        "an owned continuation must preserve any newly queued one-use Cloud permission")
  // Existing ownership validation remains mandatory even when Cloud is bypassed.
  let stale = Runner()
  stale.library.cloudBypass = game.id
  stale.invoke(game, preparation: UUID(), launchOption: 0)
  check(stale.prepared == 0 && stale.library.cloudBypass == game.id && steam.holdQueries == 0,
        "an unowned preparation never reaches launch work or consumes Cloud permission")
  // Active save operations are a stronger guard than an explicit bypass and
  // apply both to a fresh launch and to an owned continuation.
  work.isSynchronizingSaves = true
  let optionsBeforeSync = steam.optionQueries
  for inspectionOnly in [false, true] {
   for supplied in [Optional<UUID>.none, preparation] {
    let syncing = Runner()
    syncing.library.cloudBypass = game.id
    syncing.invoke(game, preparation: supplied, inspectionOnly: inspectionOnly)
    check(syncing.prepared == 0 && syncing.library.cloudBypass == game.id && syncing.library.error != nil,
          "save synchronization must refuse fresh and owned Dock launches or inspections before consuming a bypass")
   }
   for entry in [directEntry, dockEntry] {
    let syncing = Runner()
    syncing.library.cloudBypass = game.id
    syncing.invokeLibrary(entry, inspectionOnly: inspectionOnly)
    check(syncing.prepared == 0 && syncing.directInspections.isEmpty && syncing.library.cloudBypass == game.id && syncing.library.error != nil,
          "save synchronization must refuse both library Steam routes for Play and inspection")
   }
  }
  check(steam.optionQueries == optionsBeforeSync && steam.holdQueries == 0,
        "save synchronization blocks before asynchronous options or Cloud lookup")
  work.isSynchronizingSaves = false
  print("Steam Cloud launch and inspection checks: \(failures) failure(s)")
  exit(failures == 0 ? 0 : 1)
 }
}
'''


class DockCloudLaunchTests(unittest.TestCase):
    def test_inspection_skips_cloud_and_play_permissions_remain_one_use(self):
        source = (ROOT / 'app/Madeira/ContentView.swift').read_text()
        cloud_clear = declaration(source, '    private func cloudClear(')
        start = source.index('    private func startDock(')
        boundary = source.index('        let preparation: UUID', start)
        library_start = source.index('    private func launchLibraryEntry(_ entry: LibraryEntry, inspectionOnly: Bool) {')
        library_boundary = source.index('        guard wine_process_is_running() == 0, wineserver_is_running() == 0, library.current == nil else {', library_start)
        # All bodies are copied verbatim. The sentinel replaces only work after
        # the tested boundary; no production condition or recursion is rewritten.
        harness = (HARNESS.replace('__CLOUD_CLEAR__', cloud_clear)
                   .replace('__LIBRARY_PREFIX__', source[library_start:library_boundary])
                   .replace('__DOCK_PREFIX__', source[start:boundary]))
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='dock-cloud-', dir=ARTIFACTS) as directory:
            directory = Path(directory)
            swift = directory / 'Main.swift'
            binary = directory / 'checks'
            swift.write_text(harness)
            compiled = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-j', '1', str(swift), '-o', str(binary)],
                                      capture_output=True, text=True, timeout=120)
            (ARTIFACTS / 'compile.txt').write_text(compiled.stdout + compiled.stderr)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            (ARTIFACTS / 'run.txt').write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
