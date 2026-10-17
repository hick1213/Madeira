#!/usr/bin/env python3
"""Run production Steam Cloud quit/session methods against isolated service boundaries.

No account, prefix, save, or Steam server is touched. The service boundary supplies
literal comparison results; the production quit orchestration, plan and account
gate decide whether the app may close or resume its queued downloads.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app/Madeira"
SWIFTC = os.environ.get("SWIFTC") or shutil.which("swiftc")


def declaration(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f"Unclosed declaration: {marker}")


def run_check(source: str, label: str) -> int:
    if not SWIFTC:
        raise RuntimeError("swiftc is required")
    products = ROOT / "app/Build/Products"
    products.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="CloudLifecycleTests-", dir=products) as folder:
        swift = Path(folder) / "main.swift"
        executable = Path(folder) / "checks"
        swift.write_text(source)
        subprocess.run([SWIFTC, "-parse-as-library", str(swift), "-o", str(executable)], check=True, timeout=120)
        result = subprocess.run([str(executable)], check=False, timeout=180)
    print(f"{label}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
    return result.returncode


def identity_members(owned: str) -> str:
    return "\n".join(
        declaration(owned, marker)
        for marker in [
            "private struct CloudIdentity:",
            "private func invalidateCloudWork(",
            "private func resetCloudState(",
            "private func cloudIdentity(",
            "private func cloudIdentityIsCurrent(",
        ]
        if marker in owned
    )


def main() -> int:
    owned = (APP / "SteamOwnedLibrary.swift").read_text()
    cloud = (APP / "SteamCloud.swift").read_text()
    models = "\n".join([
        declaration(cloud, "struct SteamCloudEntry:"),
        # Audit comparison itself is covered by check-steam-cloud.py. These
        # fields are the literal service boundary's data, not reimplemented decisions.
        """struct SteamCloudAudit: Equatable, Sendable {
            var entries: [SteamCloudEntry] = []
            func count(_ kind: SteamCloudEntry.Kind) -> Int { entries.filter { $0.kind == kind }.count }
        }""",
        declaration(cloud, "struct SteamCloudPlan"),
        declaration(cloud, "struct SteamCloudState:"),
        declaration(owned, "@MainActor final class SteamConnectionGate"),
    ])
    if "--ui" in sys.argv:
        games = (APP / "SteamGames.swift").read_text()
        ui = r'''
import AppKit
import SwiftUI
import Foundation
''' + models + r'''
@MainActor final class SteamOwnedLibrary: ObservableObject {
    static let shared = SteamOwnedLibrary()
    static let cloudEnabled = true
    @Published var signedIn = true
    @Published var cloud: [Int: SteamCloudState] = [:]
    var started = 0
    var completed = 0
    var cancelled = 0
    func syncCloud(_ id: Int) async {
        started += 1
        cloud[id] = SteamCloudState(phase: .checking)
        do {
            try await Task.sleep(nanoseconds: 500_000_000)
            cloud[id] = SteamCloudState(phase: .ready, checked: Date())
            completed += 1
        } catch {
            cancelled += 1
            cloud[id] = SteamCloudState(phase: .failed("cancelled"))
        }
    }
    func resolveCloud(_ id: Int, useCloud: Bool) async {}
}
''' + declaration(games, "struct SteamCloudSection:") + r'''
@main struct LifecycleUI {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 420),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "Madeira Cloud lifecycle test"
        window.contentView = NSHostingView(rootView: Form { SteamCloudSection(appID: 1) })
        window.orderFront(nil)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            let steam = SteamOwnedLibrary.shared
            let initial = steam.completed == 1 && steam.cancelled == 0 && steam.cloud[1]?.phase == .ready
            print((initial ? "PASS: " : "FAIL: ") + "production CloudSection finishes its first check after rows change")
            window.contentView = NSHostingView(rootView: EmptyView())
            try? await Task.sleep(nanoseconds: 100_000_000)
            window.contentView = NSHostingView(rootView: Form { SteamCloudSection(appID: 1) })
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            let reopened = steam.completed == 2 && steam.cancelled == 0 && steam.cloud[1]?.phase == .ready
            print((reopened ? "PASS: " : "FAIL: ") + "production CloudSection checks again when a ready page is reopened")
            window.close()
            exit(initial && reopened ? 0 : 1)
        }
        app.run()
    }
}
'''
        return run_check(ui, "Native SwiftUI CloudSection lifecycle")
    helpers = "\n".join(
        declaration(owned, marker)
        for marker in ["private static func cloudCancelled"]
        if marker in owned
    )
    if "--exit" in sys.argv:
        games = (APP / "SteamGames.swift").read_text()
        quit_row = r'''
import SwiftUI
import Foundation
import Darwin
var exits = 0
func exit(_ status: Int) { exits += 1 }
enum SteamLog { static func event(_ message: String) {} }
final class LogStore { static let shared = LogStore(); func log(_ message: String) {} }
@MainActor final class SteamOwnedLibrary: ObservableObject {
    static let shared = SteamOwnedLibrary()
    static let cloudQuitEnabled = true
    @Published var signedIn = true
''' + declaration(owned, "enum CloudQuit:") + r'''
    @Published var cloudQuit: CloudQuit?
    var cloudQuitGeneration: UUID? = UUID()
    var cancelUpload = false
    var startedUploads = 0
    func uploadForQuit(_ id: Int, replaceCloud: Bool) async {
        startedUploads += 1
        cloudQuit = .done(1)
        if cancelUpload { withUnsafeCurrentTask { $0?.cancel() } }
    }
}
''' + declaration(games, "struct SteamCloudQuitRow:") + r'''
extension SteamCloudQuitRow { func invoke() { run(replaceCloud: false) } }
@main struct ExitChecks {
    @MainActor static func main() async {
        let row = SteamCloudQuitRow(appID: 1)
        let steam = SteamOwnedLibrary.shared
        var failures = 0
        func check(_ condition: Bool, _ label: String) {
            print((condition ? "PASS: " : "FAIL: ") + label)
            if !condition { failures += 1 }
        }
        row.invoke()
        steam.cloudQuitGeneration = UUID()
        steam.cloudQuit = nil
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        check(exits == 0 && steam.startedUploads == 0,
              "a queued old row checks its original generation before starting any upload")
        exits = 0
        row.invoke()
        try? await Task.sleep(nanoseconds: 50_000_000)
        steam.cloudQuit = nil // sessionChanged(false) or the next game clears this
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        check(exits == 0, "a replaced session never exits from the old row's delayed completion")
        exits = 0
        row.invoke()
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        check(exits == 1, "a still-confirmed upload closes once after its result is shown")
        exits = 0
        row.invoke()
        try? await Task.sleep(nanoseconds: 50_000_000)
        steam.cloudQuitGeneration = UUID()
        steam.cloudQuit = .done(1)
        try? await Task.sleep(nanoseconds: 1_400_000_000)
        check(exits == 0, "same-count completion from a new session cannot satisfy an old exit task")
        exits = 0
        steam.cancelUpload = true
        row.invoke()
        try? await Task.sleep(nanoseconds: 100_000_000)
        check(exits == 0, "cancellation during quit upload never skips the wait and exits")
        Darwin.exit(failures == 0 ? 0 : 1)
    }
}
'''
        return run_check(quit_row, "Cloud quit row final exit guard")
    if "--cancel" in sys.argv:
        cancellation = r'''
import Foundation
import CryptoKit
import Combine
enum SteamLog { static func event(_ message: String) {} }
enum SteamSignIn { static var accountName: String? = "synthetic"; static func message(_ error: Error) -> String { error.localizedDescription } }
struct SteamAppInfo {
    struct SaveFile { var root: String }
    var saveFiles: [SaveFile] = []
    var rootOverrides: [String] = []
}
struct SteamCloudPaths {}
struct SteamCloudListing {}
''' + models.replace("var entries: [SteamCloudEntry] = []", """var entries: [SteamCloudEntry] = []
            var cloudFiles = 0
            var summary: String { "empty" }
            static func run(listing: SteamCloudListing, saveFiles: [SteamAppInfo.SaveFile], paths: SteamCloudPaths) -> SteamCloudAudit { SteamCloudAudit() }
            static func findElsewhere(cloudPath: String, drive: URL) -> String? { nil }
        """) + r'''
@MainActor final class CheckHarness {
    static let cloudEnabled = true
    static let cloudNoFolders = "no folders"
    static let drive = URL(fileURLWithPath: "/unused-cloud-test")
    var signedIn = true
    var cloudBlocked = false
    var cloudAccount = CheckHarness.accountKey(SteamSignIn.accountName)
    var cloudGeneration = UUID()
    var cloudBaseline: [String: [String: String]]?
    var cloudBaselineAccount: String?
    var cloudTakeover = false
    var cloudAudited = false
    var cloudWaitingFor: Int?
''' + declaration(owned, "enum CloudQuit:") + r'''
    var cloudQuit: CloudQuit?
    var cloud: [Int: SteamCloudState] = [:]
    var boundaryError: Error?
    static func reason(_ error: Error) -> String { "test" }
    private func cloudContext(_ id: Int, identity: CloudIdentity) async throws -> (info: SteamAppInfo, paths: SteamCloudPaths)? {
        if let boundaryError { throw boundaryError }
        await Task.yield()
        return (SteamAppInfo(), SteamCloudPaths())
    }
    private func cloudListing(_ id: Int, identity: CloudIdentity) async throws -> SteamCloudListing { SteamCloudListing() }
    func baseline(_ id: Int) -> [String: String] { [:] }
    private func recordBaseline(_ id: Int, settled: [String: String], identity: CloudIdentity) {}
''' + helpers + "\n" + declaration(owned, "private static func accountKey(") + "\n" + identity_members(owned) + "\n" + declaration(owned, "func checkCloud(") + r'''
}
@main struct CancellationChecks {
    @MainActor static func main() async {
        var failures = 0
        func check(_ condition: Bool, _ label: String) {
            print((condition ? "PASS: " : "FAIL: ") + label)
            if !condition { failures += 1 }
        }
        for error: Error in [CancellationError(), URLError(.cancelled)] {
            let initial = CheckHarness()
            initial.boundaryError = error
            _ = await initial.checkCloud(1)
            check(initial.cloud[1] == nil, "cancelled first audit remains retryable, not a displayed failure")
            let previous = CheckHarness()
            previous.boundaryError = error
            previous.cloud[1] = SteamCloudState(phase: .ready, checked: Date())
            _ = await previous.checkCloud(1)
            check(previous.cloud[1]?.phase == .ready && previous.cloud[1]?.checked == nil,
                  "cancelled recheck preserves the earlier comparison but requires a fresh check")
        }
        let cancelled = CheckHarness()
        let task = Task { _ = await cancelled.checkCloud(1) }
        task.cancel()
        await task.value
        check(cancelled.cloud[1] == nil, "an already-cancelled task never records a successful new audit")
        let failed = CheckHarness()
        failed.boundaryError = URLError(.timedOut)
        _ = await failed.checkCloud(1)
        if case .failed = failed.cloud[1]?.phase { check(true, "real network failures remain visible") }
        else { check(false, "real network failures remain visible") }
        let retry = CheckHarness()
        retry.cloud[1] = SteamCloudState(phase: .ready, checked: nil, problem: "interrupted transfer")
        _ = await retry.checkCloud(1)
        check(retry.cloud[1]?.phase == .ready && retry.cloud[1]?.problem == nil,
              "a successful recheck clears a previous interrupted transfer problem")
        exit(failures == 0 ? 0 : 1)
    }
}
'''
        return run_check(cancellation, "Cloud audit cancellation")
    source = r'''
import Foundation
import CryptoKit
import Combine
enum SteamLog { static func event(_ message: String) {} }
enum SteamSignIn { static var accountName: String? = "synthetic" }
''' + models + r'''
@MainActor final class QuitHarness {
    static var cloudQuitEnabled = true
    static var enabled = true
    var signedIn = true
    var cloudBusy = Set<Int>()
    var cloud: [Int: SteamCloudState] = [:]
    var cloudAudited = true
    var cloudTakeover = false
    var cloudAccount = QuitHarness.accountKey(SteamSignIn.accountName)
    var cloudGeneration = UUID()
    var cloudBaseline: [String: [String: String]]?
    var cloudBaselineAccount: String?
    var cloudWaitingFor: Int?
    var cloudBlocked: Bool { inSession && !cloudTakeover }
    var inSession = true
    var resumeAfterSession = Set<Int>()
    struct Download { enum State { case active, paused }; var state: State }
    var downloads: [Int: Download] = [:]
    var queue: [Int] = []
    var active: (id: Int, task: Task<Void, Never>)?
    var resumed: [Int] = []
    var reopenCount = 0
    lazy var gate = SteamConnectionGate(close: {}, reopen: { self.reopenCount += 1 })
    var looks: [[SteamCloudEntry]] = []
    var checkFailure = false
    var baseline: [String: String] = [:]
    var uploaded: [SteamCloudEntry] = []
    func install(_ id: Int) { resumed.append(id) }
    func refreshPlaytime() async {}
    func checkCloud(_ id: Int) async -> SteamCloudPlan? {
        if checkFailure { return nil }
        let entries = looks.count > 1 ? looks.removeFirst() : (looks.first ?? [])
        var state = cloud[id] ?? SteamCloudState()
        state.audit = SteamCloudAudit(entries: entries)
        state.phase = .ready
        state.checked = Date()
        cloud[id] = state
        return SteamCloudPlan.make(audit: state.audit, baseline: baseline)
    }
    func upload(_ id: Int, _ wanted: [SteamCloudEntry]) async -> [String: Data] {
        uploaded = wanted
        cloud[id]?.lastUpload = wanted.count
        return Dictionary(wanted.map { ($0.key, $0.localSHA) }, uniquingKeysWith: { a, _ in a })
    }
''' + declaration(owned, "enum CloudQuit:") + "\n" + r'''
    var cloudQuit: CloudQuit?
''' + helpers + "\n" + declaration(owned, "private static func accountKey(") + "\n" + identity_members(owned) + "\n" + declaration(owned, "func uploadForQuit(") + "\n" + declaration(owned, "func sessionChanged(active") + r'''
}

var failures = 0
func check(_ condition: Bool, _ label: String) {
    print((condition ? "PASS: " : "FAIL: ") + label)
    if !condition { failures += 1 }
}
func entry(_ kind: SteamCloudEntry.Kind, local: UInt8 = 1, remote: UInt8 = 1, path: String = "save.dat") -> SteamCloudEntry {
    var value = SteamCloudEntry(path: path, kind: kind)
    if kind != .cloudOnly { value.localSHA = Data(repeating: local, count: 20) }
    if kind != .localOnly { value.cloudSHA = Data(repeating: remote, count: 20) }
    return value
}
@main struct Checks {
    @MainActor static func main() async {
        let changed = entry(.localOnly)
        let same = entry(.same)

        let accepted = QuitHarness()
        accepted.looks = [[changed], [changed], [same]]
        await accepted.uploadForQuit(1)
        check(accepted.cloudQuit == .done(1), "confirmed identical upload permits quit")

        let absent = QuitHarness()
        absent.looks = [[changed], [changed], []]
        await absent.uploadForQuit(1)
        if case .failed = absent.cloudQuit { check(true, "missing file after accepted upload keeps Madeira open") }
        else { check(false, "missing file after accepted upload keeps Madeira open") }

        let cloudMoved = QuitHarness()
        cloudMoved.baseline = ["save.dat": SteamCloudPlan.hex(same.localSHA)]
        cloudMoved.looks = [[entry(.differ, local: 2)], [entry(.differ, local: 2)], [entry(.differ, local: 1, remote: 3)]]
        await cloudMoved.uploadForQuit(1)
        if case .failed = cloudMoved.cloudQuit { check(true, "remote-only change after upload is not falsely confirmed") }
        else { check(false, "remote-only change after upload is not falsely confirmed") }

        let wrongHash = QuitHarness()
        wrongHash.looks = [[changed], [changed], [entry(.same, local: 2, remote: 2)]]
        await wrongHash.uploadForQuit(1)
        if case .failed = wrongHash.cloudQuit { check(true, "a different identical file is not confirmation of the bytes actually uploaded") }
        else { check(false, "a different identical file is not confirmation of the bytes actually uploaded") }

        let unstable = QuitHarness()
        unstable.looks = (1...5).map { [entry(.localOnly, local: UInt8($0))] }
        await unstable.uploadForQuit(1)
        if case .failed = unstable.cloudQuit {
            check(unstable.uploaded.isEmpty, "continuously changing saves are never sent and Madeira stays open")
        } else { check(false, "continuously changing saves are never sent and Madeira stays open") }

        let cancellation = QuitHarness()
        cancellation.looks = [[changed]]
        let task = Task { await cancellation.uploadForQuit(1) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        task.cancel()
        await task.value
        if case .failed = cancellation.cloudQuit {
            check(cancellation.uploaded.isEmpty, "cancelled stability wait never uploads or quits")
        } else { check(false, "cancelled stability wait never uploads or quits") }

        let conflicts = QuitHarness()
        conflicts.looks = [[entry(.differ, local: 2)], [entry(.differ, local: 2)]]
        await conflicts.uploadForQuit(1)
        check(conflicts.cloudQuit == .conflict(1) && conflicts.uploaded.isEmpty, "two-sided difference still waits for explicit replacement")

        let ended = QuitHarness()
        ended.inSession = false
        ended.sessionChanged(active: true)
        await ended.gate.takeOver()
        ended.cloudTakeover = true
        ended.cloudQuit = .failed("interrupted")
        ended.cloud[1] = SteamCloudState(phase: .ready, checked: Date())
        ended.resumeAfterSession = [42]
        ended.sessionChanged(active: false)
        check(!ended.inSession && !ended.cloudTakeover && ended.cloudQuit == nil,
              "ending a takeover session clears per-session Cloud state")
        check(ended.resumed == [42] && ended.reopenCount == 1,
              "ending a takeover session resumes queued downloads without a second logon")
        check(!ended.cloudAudited && ended.cloud[1]?.checked == nil,
              "return from game forces a fresh save audit")

        let abandoned = QuitHarness()
        abandoned.inSession = false
        abandoned.cloud[1] = SteamCloudState(phase: .checking, checked: Date())
        abandoned.sessionChanged(active: true)
        abandoned.sessionChanged(active: false)
        check(abandoned.cloud[1]?.phase == .ready && abandoned.cloud[1]?.checked == nil,
              "an invalidated in-flight check cannot leave Play permanently syncing after the game ends")

        let replacedSession = QuitHarness()
        replacedSession.inSession = false
        replacedSession.sessionChanged(active: true)
        replacedSession.looks = [[changed]]
        let obsolete = Task { await replacedSession.uploadForQuit(1) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        replacedSession.sessionChanged(active: false)
        replacedSession.sessionChanged(active: true)
        await obsolete.value
        check(replacedSession.cloudQuit == nil && !replacedSession.cloudTakeover && replacedSession.uploaded.isEmpty,
              "an older quit operation cannot upload or publish done in the next game session")

        let held = QuitHarness()
        held.inSession = false
        held.sessionChanged(active: true)
        await held.gate.holdForDock()
        held.resumeAfterSession = [42]
        held.sessionChanged(active: false)
        check(held.inSession && held.resumed.isEmpty && !held.gate.open,
              "Dock account ownership still prevents a second connection")
        held.gate.releaseDock()
        held.sessionChanged(active: false)
        check(!held.inSession && held.resumed == [42] && held.gate.open,
              "Dock release allows the completed session to resume downloads")
        exit(failures == 0 ? 0 : 1)
    }
}
'''
    return run_check(source, "Cloud quit/session lifecycle")


if __name__ == "__main__":
    raise SystemExit(main())
