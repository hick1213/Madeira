#!/usr/bin/env python3
"""Execute the production game-work coordinator with native runtime boundaries.

No network, accounts, devices, user preferences or actual save files are used.
Compilation and reports stay on the external inspection volume; exact temporary
compiler directories are removed even when a child fails or times out.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/GameSessionWork.swift"
OUT = ROOT / "app/Build/Products/IPAInspection/game-session-work"

BOUNDARIES = r'''
import Foundation
import Combine
@MainActor final class LibraryModel {
    static let shared = LibraryModel()
    struct Entry { var id = UUID(); var steamAppID: Int? }
    var entries: [Entry] = []
    var current: UUID?
    var startingJIT: UUID?
    var error: String?
    var jitNotice: String?
    func pauseOptionalWork(_ paused: Bool) {}
    func upsertSteam(_ game: DockGame, title: String) {}
}
@MainActor final class SteamOwnedLibrary {
    static let shared = SteamOwnedLibrary()
    func sessionChanged(active: Bool) {}
    var hasInFlightDownload = false
    var hasActiveCloudSaveOperation = false
}
@MainActor final class EpicLibraryModel {
    static let shared = EpicLibraryModel()
    func sessionChanged(active: Bool) {}
    var hasInFlightDownload = false
}
@MainActor var nativeWine: Int32 = 0
@MainActor var nativeServer: Int32 = 0
@MainActor func wine_process_is_running() -> Int32 { nativeWine }
@MainActor func wineserver_is_running() -> Int32 { nativeServer }
@MainActor enum StikJITHelper {
    static var ready = false
    static var flaggedWithoutDebugger = false
    static let noDebuggerMessage = "Fixture missing debugger"
}
@MainActor final class LogStore {
    enum Level { case error }
    func log(_ value: String, level: Level? = nil) {}
}
enum JITStatus { case unknown, testing }
'''

JIT_HARNESS = r'''
@MainActor final class LaunchHarness {
    var launchAfterJIT: (() -> Void)?
    var launchAfterJITPreparation: UUID?
    let library = LibraryModel.shared
    let logStore = LogStore()
    var jitStatus = JITStatus.unknown
    var enableCalls = 0
    func enableJIT() { enableCalls += 1; jitStatus = .testing }
    __ACTUAL_JIT_METHODS__
    func exerciseBackground() async throws -> Int {
        let work = GameSessionWork.shared
        let token = try work.beginLaunchPreparation()
        var launches = 0, failures = 0
        guard backgroundReadyForLaunch(preparation: token, onFailure: { _ in failures += 1 }, then: { launches += 1 }) else {
            throw Failed(message: "idle background launch gate must continue synchronously")
        }
        EpicLibraryModel.shared.hasInFlightDownload = true
        guard !backgroundReadyForLaunch(preparation: token, onFailure: { _ in failures += 1 }, then: { launches += 1 }) else {
            throw Failed(message: "actual launch gate must not proceed while file work drains")
        }
        try await Task.sleep(for: .milliseconds(150))
        guard launches == 0 && failures == 0 else { throw Failed(message: "actual launch hook ran before drain") }
        EpicLibraryModel.shared.hasInFlightDownload = false
        try await Task.sleep(for: .milliseconds(150))
        guard launches == 1 && failures == 0 else { throw Failed(message: "actual launch hook must resume once after drain") }
        EpicLibraryModel.shared.hasInFlightDownload = true
        _ = backgroundReadyForLaunch(preparation: token, onFailure: { _ in failures += 1 }, then: { launches += 1 })
        work.completePreparation(token)
        let replacement = try work.beginLaunchPreparation()
        try await Task.sleep(for: .milliseconds(150))
        guard launches == 1 && failures == 1 && work.ownsPreparation(replacement) else {
            throw Failed(message: "stale actual launch hook must not launch or release replacement")
        }
        EpicLibraryModel.shared.hasInFlightDownload = false
        work.completePreparation(replacement)
        return 5
    }
    func exercise() throws -> Int {
        var count = 0
        func check(_ value: @autoclosure () -> Bool, _ text: String) throws {
            guard value() else { throw Failed(message: text) }; count += 1
        }
        let work = GameSessionWork.shared
        let token = try work.beginLaunchPreparation()
        var launches = 0
        let entry = UUID()
        try check(!jitReadyForLaunch(inLibrary: true, entry: entry, preparation: token, then: { launches += 1 }), "actual JIT hook waits for debugger")
        try check(enableCalls == 1 && library.startingJIT == entry && work.ownsPreparation(token), "actual JIT wait keeps preparation owned")
        launchAfterJITEnded(started: false)
        try check(launches == 0 && !work.pausesBackgroundWork && library.startingJIT == nil, "actual JIT failure releases pending preparation")
        let next = try work.beginLaunchPreparation()
        _ = jitReadyForLaunch(inLibrary: true, entry: entry, preparation: next, then: { launches += 1; nativeWine = 1 })
        StikJITHelper.ready = true
        launchAfterJITEnded(started: true)
        try check(launches == 1 && work.ownsPreparation(next) && launchAfterJIT == nil, "actual successful JIT callback launches once with same preparation")
        work.completePreparation(next)
        try check(work.pausesBackgroundWork, "detach after actual callback does not resume live native game")
        nativeWine = 0; work.reconcile()
        let cancelled = try work.beginLaunchPreparation()
        StikJITHelper.ready = false; jitStatus = .unknown
        _ = jitReadyForLaunch(inLibrary: true, preparation: cancelled, then: { launches += 1 })
        work.completePreparation(cancelled)
        let replacement = try work.beginLaunchPreparation()
        StikJITHelper.ready = true
        launchAfterJITEnded(started: true)
        try check(launches == 1 && work.ownsPreparation(replacement), "actual stale JIT callback cannot start or release replacement")
        work.completePreparation(replacement)
        try check(!work.pausesBackgroundWork, "actual JIT integration ends idle")
        return count
    }
}
'''

STEAM_HARNESS = r'''
import Combine
import CryptoKit
enum SteamLog { static func event(_ value: String) {} }
struct SteamDownloadProgress: Equatable { var sourceNotice: String? }
enum SteamSignIn {
    static let accountName: String? = "Fixture account"
    static func message(_ error: Error) -> String { "Fixture failure" }
}
enum SteamError: Error { case appInfoNotFound(UInt32), logonDenied(Int) }
struct DockGame {
    var id: Int; var name: String; var installDir: String; var library: String
    var installed: Bool; var customExecutables: Bool
}
enum SteamInstallPaths {
    static let libraryRelative = "steamapps"
    static func common(drive: URL) -> URL { drive.appendingPathComponent("common") }
}
struct FixtureInstallInfo { var name = "Fixture" }
struct FixtureApp { var installableOnWindows = true; var name = "Fixture" }
struct SteamOwnedGame { var name: String; init(_ app: FixtureApp) { name = app.name } }
struct SteamPlaytime: Codable {
    var minutes = 1
    static func parse(_ value: Data) throws -> [Int: SteamPlaytime] { [42: SteamPlaytime()] }
}
@MainActor final class FixtureFetcher {
    let catalogueGate = DownloadGate()
    func fetchOwnedApps() async throws -> [FixtureApp] { await catalogueGate.wait(); return [FixtureApp()] }
    func fetchInstallInfo(appID: UInt32) async throws -> FixtureInstallInfo? { FixtureInstallInfo() }
    func ownedDepotIDs() async throws -> Set<UInt32> { [] }
}
actor DownloadGate {
    var entered = false; var opened = false; var waiter: CheckedContinuation<Void, Never>?
    func wait() async { entered = true; if !opened { await withCheckedContinuation { waiter = $0 } } }
    func open() { opened = true; waiter?.resume(); waiter = nil }
    func hasEntered() -> Bool { entered }
}
@MainActor final class FixtureDownloader {
    let gate = DownloadGate()
    var calls = 0
    func install(_ info: FixtureInstallInfo, steamApps: URL,
                 ownedDepots: () async -> Set<UInt32>?, progress: (SteamDownloadProgress) -> Void) async throws -> URL {
        calls += 1
        if calls == 1 { await gate.wait(); try Task.checkCancellation() }
        return steamApps.appendingPathComponent("fixture")
    }
}
@MainActor final class SteamDownloadBackground {
    static let shared = SteamDownloadBackground()
    enum Outcome { case paused, completed, failed(String) }
    func downloadStarted(appID: Int, name: String) {}
    func progress(_ value: SteamDownloadProgress) {}
    func downloadEnded(appID: Int, name: String, outcome: Outcome, queueEmpty: Bool) {}
}
@MainActor final class SteamGamesModel {
    static let shared = SteamGamesModel()
    func refresh() {}
}
__ACTUAL_STEAM_SETUP_QUEUE__
__ACTUAL_STEAM_GATE__
@MainActor final class SteamQueueHarness {
    static let enabled = true
    static var drive: URL { URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true) }
    static var steamApps: URL { drive.appendingPathComponent("steamapps") }
    static var supportFolder: URL { drive }
    static var playtimeURL: URL { drive.appendingPathComponent("synthetic-playtime.json") }
    var signedIn = true
    var refreshing = false
    var owned: [SteamOwnedGame] = []
    var playtime: [Int: SteamPlaytime] = [:]
    var libraryUpdated: Date?
    var cachedAccount: String?
    var cacheWrites = 0
    var playtimeGate: DownloadGate?
    var error: String?
    var inSession = false
    var queue: [Int] = []
    var active: (id: Int, task: Task<Void, Never>)?
    var resumeAfterSession = Set<Int>()
    var setupChoices: [String: Bool] = [:]
    var installSetups = SteamInstallSetupQueue()
    let fetcher = FixtureFetcher()
    let downloader = FixtureDownloader()
    let gate = SteamConnectionGate(close: {}, reopen: {})
    var downloads: [Int: Download] = [:]
    func invalidateCloudWork() {}
    func restoreIncompleteDownloads() {}
    func saveInstallSetups() {}
    func writeCache() { cacheWrites += 1 }
    func auditCloudSaves() async {}
    func requestOwnedGamesPlaytime() async throws -> Data {
        if let playtimeGate { await playtimeGate.wait() }; return Data()
    }
    func startTimer() {}
    func game(_ id: Int) -> DockGame? { nil }
    func handleSessionError(_ error: Error, context: String, report: Bool = true) {}
    static func reason(_ error: Error) -> String { "Fixture error" }
    __ACTUAL_STEAM_QUEUE__
    static func exercise() async throws -> Int {
        let resumed = SteamQueueHarness()
        resumed.install(42)
        while !(await resumed.downloader.gate.hasEntered()) { await Task.yield() }
        let oldTask = resumed.active!.task
        resumed.sessionChanged(active: true)
        resumed.sessionChanged(active: false)
        await resumed.downloader.gate.open()
        await oldTask.value; await resumed.active?.task.value
        guard resumed.downloader.calls == 2 else { throw Failed(message: "fast session exit must resume Steam active job after cancelled task drains") }
        let manual = SteamQueueHarness()
        manual.install(43)
        while !(await manual.downloader.gate.hasEntered()) { await Task.yield() }
        let manualTask = manual.active!.task
        manual.sessionChanged(active: true); manual.pause(43); manual.sessionChanged(active: false)
        await manual.downloader.gate.open()
        await manualTask.value
        guard manual.downloader.calls == 1 && manual.downloads[43]?.state == .paused else {
            throw Failed(message: "manual Steam pause must not restart after cancellation drain")
        }
        let catalog = SteamQueueHarness()
        let refresh = Task { @MainActor in await catalog.refreshLibrary() }
        while !(await catalog.fetcher.catalogueGate.hasEntered()) { await Task.yield() }
        catalog.sessionChanged(active: true)
        await catalog.fetcher.catalogueGate.open(); await refresh.value
        guard catalog.owned.isEmpty && catalog.cacheWrites == 0 else {
            throw Failed(message: "Steam catalogue response arriving during game cannot publish or write cache")
        }
        catalog.sessionChanged(active: false)
        let playtime = SteamQueueHarness(), playtimeGate = DownloadGate()
        playtime.playtimeGate = playtimeGate
        let playtimeRefresh = Task { @MainActor in await playtime.refreshPlaytime() }
        while !(await playtimeGate.hasEntered()) { await Task.yield() }
        playtime.sessionChanged(active: true)
        await playtimeGate.open(); await playtimeRefresh.value
        guard playtime.playtime.isEmpty else { throw Failed(message: "Steam playtime response cannot publish during game") }
        playtime.sessionChanged(active: false)
        return 4
    }
}
'''

HARNESS = r'''
import Foundation
import Darwin
struct Failed: Error { let message: String }
@main struct Run {
    @MainActor static func main() async {
        do { try await run() }
        catch let error as Failed { print("FAIL: " + error.message); exit(1) }
        catch { print("FAIL: unexpected " + String(describing: error)); exit(1) }
    }
    @MainActor static func run() async throws {
        var assertions = 0
        func check(_ value: @autoclosure () -> Bool, _ text: String) throws {
            guard value() else { throw Failed(message: text) }; assertions += 1
        }
        var library = false, wine = false, server = false
        var transitions: [Bool] = []
        let work = GameSessionWork(librarySession: { library }, nativeSession: { wine || server },
                                   changed: { transitions.append($0) })
        try check(!work.pausesBackgroundWork && !work.isMonitoring, "idle coordinator has no timer")
        let first = try work.beginLaunchPreparation()
        try check(work.pausesBackgroundWork && work.isMonitoring && work.ownsPreparation(first), "preparation pauses before native starts")
        try check(transitions == [true], "one synchronous pause notification")
        do { _ = try work.beginSaveOperation(); throw Failed(message: "save accepted during preparation") }
        catch GameSessionWork.Failure.gameRunning { assertions += 1 }
        do { _ = try work.beginLaunchPreparation(); throw Failed(message: "second preparation accepted") }
        catch GameSessionWork.Failure.sessionAlreadyRunning { assertions += 1 }
        work.completePreparation(UUID())
        try check(work.ownsPreparation(first) && transitions == [true], "wrong preparation does not release current launch")
        library = true; wine = true; work.reconcile(); work.completePreparation(first)
        try check(work.pausesBackgroundWork && work.isMonitoring && !work.ownsPreparation(first), "detach completion keeps running session paused")
        do { _ = try work.beginSaveOperation(); throw Failed(message: "save accepted during game") }
        catch GameSessionWork.Failure.gameRunning { assertions += 1 }
        wine = false; library = false; server = true; work.reconcile()
        try check(work.pausesBackgroundWork, "server remains protected after guest exit")
        server = false; work.reconcile()
        try check(!work.pausesBackgroundWork && !work.isMonitoring && transitions == [true, false], "all stopped resumes exactly once and stops timer")
        let legacy = try work.beginLaunchPreparation()
        wine = true; work.completePreparation(legacy)
        try check(work.pausesBackgroundWork && work.isMonitoring, "legacy without Library.current remains paused")
        wine = false
        try await Task.sleep(for: .milliseconds(700))
        try check(!work.pausesBackgroundWork && !work.isMonitoring, "real shared-style timer detects legacy native exit")
        let failed = try work.beginLaunchPreparation()
        work.completePreparation(failed)
        try check(!work.pausesBackgroundWork && !work.isMonitoring, "fast launch failure releases preparation")
        let newer = try work.beginLaunchPreparation()
        work.completePreparation(failed)
        try check(work.ownsPreparation(newer), "stale failure cannot release next launch")
        work.completePreparation(newer)
        let save = try work.beginSaveOperation()
        try check(work.pausesBackgroundWork && !work.isMonitoring, "save pauses transfers without idle polling")
        do { _ = try work.beginSaveOperation(); throw Failed(message: "concurrent save accepted") }
        catch GameSessionWork.Failure.saveAlreadyRunning { assertions += 1 }
        do { _ = try work.beginLaunchPreparation(); throw Failed(message: "launch accepted during save") }
        catch GameSessionWork.Failure.savesSyncing { assertions += 1 }
        work.endSaveOperation(UUID())
        try check(work.pausesBackgroundWork, "wrong save token cannot release operation")
        work.endSaveOperation(save)
        try check(!work.pausesBackgroundWork && !work.isMonitoring, "save completion restores idle work")
        work.endSaveOperation(save); work.reconcile()
        try check(transitions == [true, false, true, false, true, false, true, false, true, false], "idempotent exits do not resume twice")
        server = true
        do { _ = try work.beginSaveOperation(); throw Failed(message: "save accepted while untracked native session runs") }
        catch GameSessionWork.Failure.gameRunning { assertions += 1 }
        try check(work.pausesBackgroundWork && work.isMonitoring, "native-only reconciliation protects existing session")
        server = false; work.reconcile()
        assertions += try LaunchHarness().exercise()
        SteamOwnedLibrary.shared.hasActiveCloudSaveOperation = true
        var cloudBlocked = false
        do {
            let invalid = try GameSessionWork.shared.beginSaveOperation()
            GameSessionWork.shared.endSaveOperation(invalid)
        } catch { cloudBlocked = true }
        try check(cloudBlocked, "save acquisition refuses existing Steam Cloud file operation")
        SteamOwnedLibrary.shared.hasActiveCloudSaveOperation = false
        let shared = GameSessionWork.shared
        let upload = try shared.beginSaveOperation()
        SteamOwnedLibrary.shared.hasInFlightDownload = true
        EpicLibraryModel.shared.hasInFlightDownload = true
        var ready = false
        let waiting = Task { @MainActor in try await shared.waitForSaveOperation(upload); ready = true }
        try await Task.sleep(for: .milliseconds(150))
        try check(!ready && shared.isSynchronizingSaves, "save awaits actual transfer task boundaries after cancellation")
        SteamOwnedLibrary.shared.hasInFlightDownload = false
        try await Task.sleep(for: .milliseconds(150))
        try check(!ready, "save still waits for second provider")
        EpicLibraryModel.shared.hasInFlightDownload = false
        try await waiting.value
        try check(ready && shared.pausesBackgroundWork, "drained save remains exclusive until caller releases")
        shared.endSaveOperation(upload)
        do { try await shared.waitForSaveOperation(upload); throw Failed(message: "released save token accepted") }
        catch GameSessionWork.Failure.saveOperationEnded { assertions += 1 }
        let cancelledSave = try shared.beginSaveOperation()
        EpicLibraryModel.shared.hasInFlightDownload = true
        let cancellation = Task { @MainActor in try await shared.waitForSaveOperation(cancelledSave) }
        try await Task.sleep(for: .milliseconds(100)); cancellation.cancel()
        do { try await cancellation.value; throw Failed(message: "cancelled drain wait completed") }
        catch is CancellationError { assertions += 1 }
        try check(shared.isSynchronizingSaves, "cancelled wait cannot release ownership behind caller")
        EpicLibraryModel.shared.hasInFlightDownload = false
        shared.endSaveOperation(cancelledSave)
        let launch = try shared.beginLaunchPreparation()
        SteamOwnedLibrary.shared.hasInFlightDownload = true
        EpicLibraryModel.shared.hasInFlightDownload = true
        SteamOwnedLibrary.shared.hasActiveCloudSaveOperation = true
        var launched = false
        let launchWait = Task { @MainActor in try await shared.waitForLaunchPreparation(launch); launched = true }
        try await Task.sleep(for: .milliseconds(150))
        try check(!launched && !shared.backgroundWorkIsDrained, "launch waits for cancelled transfers and cloud writes")
        SteamOwnedLibrary.shared.hasInFlightDownload = false
        EpicLibraryModel.shared.hasInFlightDownload = false
        try await Task.sleep(for: .milliseconds(150))
        try check(!launched, "launch still waits for cloud save file operation")
        SteamOwnedLibrary.shared.hasActiveCloudSaveOperation = false
        try await launchWait.value
        try check(launched && shared.ownsPreparation(launch) && shared.pausesBackgroundWork,
                  "drained launch remains owned and paused until native startup")
        shared.completePreparation(launch)
        do { try await shared.waitForLaunchPreparation(launch); throw Failed(message: "released launch accepted") }
        catch GameSessionWork.Failure.launchPreparationEnded { assertions += 1 }
        let cancelledLaunch = try shared.beginLaunchPreparation()
        EpicLibraryModel.shared.hasInFlightDownload = true
        let launchCancellation = Task { @MainActor in try await shared.waitForLaunchPreparation(cancelledLaunch) }
        try await Task.sleep(for: .milliseconds(100)); launchCancellation.cancel()
        do { try await launchCancellation.value; throw Failed(message: "cancelled launch drain completed") }
        catch is CancellationError { assertions += 1 }
        try check(shared.ownsPreparation(cancelledLaunch), "cancelled launch waiter cannot release ownership")
        shared.completePreparation(cancelledLaunch)
        let replacementLaunch = try shared.beginLaunchPreparation()
        do { try await shared.waitForLaunchPreparation(cancelledLaunch); throw Failed(message: "stale launch accepted") }
        catch GameSessionWork.Failure.launchPreparationEnded { assertions += 1 }
        try check(shared.ownsPreparation(replacementLaunch), "stale launch cannot disturb replacement")
        EpicLibraryModel.shared.hasInFlightDownload = false
        nativeServer = 1
        do { try await shared.waitForLaunchPreparation(replacementLaunch); throw Failed(message: "native session started during drain accepted") }
        catch GameSessionWork.Failure.sessionAlreadyRunning { assertions += 1 }
        nativeServer = 0
        shared.completePreparation(replacementLaunch)
        assertions += try await LaunchHarness().exerciseBackground()
        assertions += try await SteamQueueHarness.exercise()
        print("PASS coordinator and actual JIT hooks: \(assertions) production assertions")
    }
}
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    options = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"optimized": options.optimize, "python_optimized": not __debug__, "source_sha256": None, "mutations": [],
              "boundary": "Real coordinator and Foundation timer; synthetic native flags and transition endpoints",
              "not_executed": ["iPad lifecycle", "actual game/download CPU or I/O", "OS Game Mode"]}
    scratch = None
    code = 1
    suffix = ("python-optimized-" if not __debug__ else "") + ("optimized" if options.optimize else "debug")
    try:
        with tempfile.TemporaryDirectory(prefix="coordinator-", dir=OUT) as name:
            scratch = Path(name)
            production = SOURCE.read_bytes()
            report["source_sha256"] = hashlib.sha256(production).hexdigest()
            copied = scratch / SOURCE.name
            copied.write_bytes(production)
            boundary = scratch / "Boundary.swift"
            boundary.write_text(BOUNDARIES)
            spec = importlib.util.spec_from_file_location("performance_extractor", ROOT / "tests/host/check-performance-integration.py")
            if spec is None or spec.loader is None:
                raise OSError("Could not load bounded Swift definition extractor")
            extractor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(extractor)
            content = (ROOT / "app/Madeira/ContentView.swift").read_text()
            report["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
            jit = "\n".join(extractor.function(content, method) for method in
                            ("private func jitReadyForLaunch(", "private func launchAfterJITEnded(",
                             "private func backgroundReadyForLaunch("))
            owned = (ROOT / "app/Madeira/SteamOwnedLibrary.swift").read_text()
            report["steam_owned_sha256"] = hashlib.sha256(owned.encode()).hexdigest()
            queue = "\n".join(extractor.function(owned, method) for method in
                              ("struct Download:", "func sessionChanged(active", "func install(_ appID:",
                               "func pause(_ appID:", "private func pump(", "private func run(_ appID:",
                               "func refreshLibrary(", "func refreshPlaytime(",
                               "private func setupPreferenceKey(", "private static func accountKey("))
            steam = STEAM_HARNESS.replace("__ACTUAL_STEAM_GATE__", extractor.function(owned, "@MainActor final class SteamConnectionGate"))
            steam = steam.replace("__ACTUAL_STEAM_SETUP_QUEUE__", extractor.function(owned, "struct SteamInstallSetupQueue:"))
            steam = steam.replace("__ACTUAL_STEAM_QUEUE__", queue)
            harness = scratch / "Run.swift"
            original_harness = HARNESS + JIT_HARNESS.replace("__ACTUAL_JIT_METHODS__", jit) + steam
            harness.write_text(original_harness)
            binary = scratch / "check"
            command = ["xcrun", "swiftc", "-j", "1", "-swift-version", "6", "-strict-concurrency=complete",
                       "-warnings-as-errors", "-parse-as-library", "-module-cache-path", str(scratch / "module-cache"),
                       str(copied), str(boundary), str(harness), "-o", str(binary)]
            if options.optimize:
                command.insert(2, "-O")
            compiled = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
            (OUT / f"coordinator-compile-{suffix}.txt").write_text(compiled.stdout + compiled.stderr)
            report["compiled"] = compiled.returncode == 0
            if compiled.returncode:
                print(compiled.stdout + compiled.stderr, end="")
                return compiled.returncode
            fixture = scratch / "synthetic-drive"
            fixture.mkdir()
            result = subprocess.run([str(binary), str(fixture)], text=True, capture_output=True, timeout=15, check=False,
                                    env=dict(os.environ, SWIFT_BACKTRACE="enable=no"))
            (OUT / f"coordinator-run-{suffix}.txt").write_text(result.stdout + result.stderr)
            report["output"] = result.stdout + result.stderr
            print(report["output"], end="")
            code = result.returncode
            if options.mutations and code == 0:
                for label, before, after in (
                    ("detach-loses-native", "preparation != nil || librarySession() || nativeSession()", "preparation != nil || librarySession()"),
                    ("wrong-preparation", "guard preparation == token else { return }", "/* token identity removed */"),
                    ("wrong-save", "guard saveOperation == token else { return }", "/* token identity removed */"),
                    ("save-launch-race", "guard saveOperation == nil else { throw Failure.savesSyncing }", "/* save exclusion removed */"),
                    ("cloud-save-race", "guard !backgroundWork().cloudSaves else { throw Failure.backgroundBusy }", "/* cloud write exclusion removed */"),
                    ("download-drain-race", "if !background.downloads { return }", "if background.downloads || !background.downloads { return }"),
                    ("launch-drain-race", "if backgroundWorkIsDrained { return }", "if preparation == token { return }"),
                ):
                    if production.decode().count(before) != 1:
                        raise OSError("Mutation anchor moved: " + label)
                    copied.write_text(production.decode().replace(before, after, 1))
                    compiled = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
                    (OUT / f"mutation-{label}-compile-{suffix}.txt").write_text(compiled.stdout + compiled.stderr)
                    if compiled.returncode:
                        code = 1
                        report["mutations"].append({"label": label, "compiled": False})
                        continue
                    mutation = subprocess.run([str(binary), str(fixture)], text=True, capture_output=True, timeout=15, check=False,
                                              env=dict(os.environ, SWIFT_BACKTRACE="enable=no"))
                    output = mutation.stdout + mutation.stderr
                    rejected = mutation.returncode != 0 and "FAIL:" in mutation.stdout
                    report["mutations"].append({"label": label, "compiled": True, "runtime_rejected": rejected, "output": output})
                    (OUT / f"mutation-{label}-run-{suffix}.txt").write_text(output)
                    if not rejected:
                        code = 1
                    print(("PASS" if rejected else "FAIL") + " compiled mutation rejected at runtime: " + label)
                copied.write_bytes(production)
                for label, before, after in (
                    ("steam-fast-exit", "if active?.id == id { resumeAfterSession.insert(id) }\n                else { install(id) }", "install(id)"),
                    ("steam-optional-publish", "guard !Task.isCancelled, !inSession, signedIn, GameSessionWork.shared.workEpoch == epoch else { return }", "/* optional publication guard removed */"),
                ):
                    if before not in original_harness:
                        raise OSError("Steam mutation anchor moved: " + label)
                    harness.write_text(original_harness.replace(before, after))
                    compiled = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
                    (OUT / f"mutation-{label}-compile-{suffix}.txt").write_text(compiled.stdout + compiled.stderr)
                    if compiled.returncode:
                        code = 1
                        report["mutations"].append({"label": label, "compiled": False})
                        continue
                    mutation = subprocess.run([str(binary), str(fixture)], text=True, capture_output=True, timeout=15, check=False,
                                              env=dict(os.environ, SWIFT_BACKTRACE="enable=no"))
                    output = mutation.stdout + mutation.stderr
                    rejected = mutation.returncode != 0 and "FAIL:" in mutation.stdout
                    report["mutations"].append({"label": label, "compiled": True, "runtime_rejected": rejected, "output": output})
                    (OUT / f"mutation-{label}-run-{suffix}.txt").write_text(output)
                    if not rejected:
                        code = 1
                    print(("PASS" if rejected else "FAIL") + " compiled mutation rejected at runtime: " + label)
            return code
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        code = 1
        report["error"] = str(error)
        print("FAIL: " + str(error))
        return 1
    finally:
        report["scratch_removed"] = scratch is not None and not scratch.exists()
        report["exit"] = code
        (OUT / f"coordinator-report-{suffix}.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
