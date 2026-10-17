#!/usr/bin/env python3
"""Run Cloud account isolation and connection takeover against synthetic accounts.

Only temporary JSON records and synthetic asynchronous replies are used. These
checks exercise production baseline/account-reset/audit/gate methods; no token,
real account, Cloud endpoint, device prefix or user backup is accessed.
"""

from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app/Madeira"
HELPERS = runpy.run_path(str(ROOT / "tests/host/check-steam-cloud-lifecycle.py"))
DECLARATION = HELPERS["declaration"]


def main() -> int:
    owned = (APP / "SteamOwnedLibrary.swift").read_text()
    cloud = (APP / "SteamCloud.swift").read_text()
    models = "\n".join([
        DECLARATION(cloud, "struct SteamCloudEntry:"),
        DECLARATION(cloud, "struct SteamCloudPlan:"),
        DECLARATION(cloud, "struct SteamCloudState:"),
        DECLARATION(owned, "@MainActor final class SteamConnectionGate"),
    ])
    methods = "\n".join([
        DECLARATION(owned, "private static func accountKey("),
        DECLARATION(owned, "private struct CloudBaseline:"),
        DECLARATION(owned, "private func baseline("),
        DECLARATION(owned, "private func recordBaseline("),
        DECLARATION(owned, "private func signInChanged("),
        DECLARATION(owned, "private func clearCaches("),
        DECLARATION(owned, "private static func cloudCancelled("),
        DECLARATION(owned, "@discardableResult\n    func checkCloud("),
        DECLARATION(owned, "enum CloudQuit:"),
        DECLARATION(owned, "enum CloudHold:"),
        DECLARATION(owned, "func cloudHold("),
        DECLARATION(owned, "func settleCloud("),
        DECLARATION(owned, "func syncCloud("),
        HELPERS["identity_members"](owned),
    ])
    context_identity = ", identity: CloudIdentity" if "private struct CloudIdentity:" in owned else ""
    source = r'''
import Foundation
import CryptoKit
import Combine
enum SteamLog { static func event(_ message: String) {} }
@MainActor enum SteamSignIn {
    static var accountName: String? = "account-a"
    static var isSignedIn: Bool { accountName != nil }
    static func message(_ error: Error) -> String { error.localizedDescription }
}
enum DockOffline { static func clear() {} }
struct SteamAppInfo {
    struct SaveFile { var root: String }
    var saveFiles: [SaveFile] = []
    var rootOverrides: [String] = []
}
struct SteamCloudPaths {}
struct SteamCloudListing { var entries: [SteamCloudEntry] = [] }
struct SteamCloudAudit: Equatable, Sendable {
    var entries: [SteamCloudEntry] = []
    var cloudFiles: Int { entries.count }
    var summary: String { "fixture" }
    func count(_ kind: SteamCloudEntry.Kind) -> Int { entries.filter { $0.kind == kind }.count }
    static func run(listing: SteamCloudListing, saveFiles: [SteamAppInfo.SaveFile], paths: SteamCloudPaths) -> SteamCloudAudit {
        SteamCloudAudit(entries: listing.entries)
    }
    static func findElsewhere(cloudPath: String, drive: URL) -> String? { nil }
}
''' + models + r'''
@MainActor final class Accounts {
    static let cloudEnabled = true
    static let cloudNoFolders = "no folders"
    static let cloudAutomatic = true
    static let cloudPlayCheck = true
    static let cloudFresh: TimeInterval = 600
    static var supportFolder: URL!
    static var cloudBaselineURL: URL { supportFolder.appendingPathComponent("steam-cloud-sync.json") }
    static var cacheURL: URL { supportFolder.appendingPathComponent("library.json") }
    static var playtimeURL: URL { supportFolder.appendingPathComponent("playtime.json") }
    static let drive = URL(fileURLWithPath: "/unused-cloud-test")
    var signedIn = true
    var cachedAccount: String?
    var cloudAccount: String? = Accounts.accountKey("account-a")
    var cloudGeneration = UUID()
    var cloudBaselineAccount: String?
    var cloudBaseline: [String: [String: String]]?
    var cloud: [Int: SteamCloudState] = [:]
    var cloudAudited = true
    var cloudWaitingFor: Int? = 1
    var cloudWaitingOperation: UUID?
    var cloudQuit: CloudQuit? = .done(1)
    var cloudQuitOperation: UUID?
    var cloudTakeover = true
    var cloudSessionGeneration = 0
    var cloudBlocked = false
    var cloudBusy = Set<Int>()
    var inSession = false
    var owned: [Int] = []
    var libraryUpdated: Date?
    var playtime: [Int: Int] = [:]
    var downloads: [Int: Int] = [:]
    var held: CheckedContinuation<Void, Never>?
    var delayed = false
    var cancellation = false
    var listing = SteamCloudListing()
    final class Session { func logoff() {} }
    let session = Session()
    func pause(_ id: Int) {}
    func refreshLibrary(interactive: Bool) async {}
    static func reason(_ error: Error) -> String { "fixture" }
    private func cloudContext(_ id: Int''' + context_identity + r''') async throws -> (info: SteamAppInfo, paths: SteamCloudPaths)? {
        if delayed { await withCheckedContinuation { held = $0 } }
        if cancellation { throw CancellationError() }
        return (SteamAppInfo(), SteamCloudPaths())
    }
    private func cloudListing(_ id: Int''' + context_identity + r''') async throws -> SteamCloudListing { listing }
    private func download(_ id: Int, _ wanted: [SteamCloudEntry]) async -> Bool { false }
    private func upload(_ id: Int, _ wanted: [SteamCloudEntry]) async -> [String: Data] { [:] }
''' + methods + r'''
    func read(_ id: Int) -> [String: String] { baseline(id) }
    func seed(_ account: String, apps: [String: [String: String]]) throws {
        try JSONEncoder().encode(CloudBaseline(account: Self.accountKey(account)!, apps: apps)).write(to: Self.cloudBaselineURL)
    }
    func stored() throws -> (String, [String: [String: String]]) {
        let value = try JSONDecoder().decode(CloudBaseline.self, from: Data(contentsOf: Self.cloudBaselineURL))
        return (value.account, value.apps)
    }
    func changed() { signInChanged() }
    func diskBytes() throws -> Data { try Data(contentsOf: Self.cloudBaselineURL) }
}
@main struct AccountChecks {
    @MainActor static func main() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        Accounts.supportFolder = directory
        let deviceSave = directory.appendingPathComponent("game.sav")
        let backupSave = directory.appendingPathComponent("backup.sav")
        let preserved = Data("user progress".utf8)
        try preserved.write(to: deviceSave)
        try preserved.write(to: backupSave)
        var failures = 0
        func check(_ condition: Bool, _ label: String) {
            print((condition ? "PASS: " : "FAIL: ") + label)
            if !condition { failures += 1 }
        }

        SteamSignIn.accountName = "account-a"
        let busy = Accounts()
        busy.delayed = true
        let pendingSync = Task { await busy.syncCloud(1) }
        while busy.held == nil { await Task.yield() }
        SteamSignIn.accountName = "account-b"
        busy.changed()
        check(busy.cloudBusy == [1], "account reset retains busy ownership until the old operation returns")
        await busy.syncCloud(1)
        busy.held?.resume()
        await pendingSync.value
        check(busy.cloudBusy.isEmpty && busy.cloud.isEmpty,
              "late old sync releases only its own busy marker without publishing into the new account")

        SteamSignIn.accountName = "account-a"
        let waiting = Accounts()
        waiting.cloudBusy = [1]
        let pendingPlay = Task { await waiting.settleCloud(1) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        SteamSignIn.accountName = "account-b"
        waiting.changed()
        waiting.cloudWaitingFor = 2
        waiting.cloudBusy.remove(1)
        let playResult = await pendingPlay.value
        if case .unchecked = playResult { check(true, "an old account's Play wait cannot authorize a game under the new account") }
        else { check(false, "an old account's Play wait cannot authorize a game under the new account") }
        check(waiting.cloudWaitingFor == 2, "old Play cleanup never clears the new account's waiting indicator")
        SteamSignIn.accountName = "account-a"
        let cache = Accounts()
        try cache.seed("account-a", apps: ["1": ["a.sav": "aaaa"]])
        check(cache.read(1) == ["a.sav": "aaaa"], "the matching synthetic account baseline is loaded")
        SteamSignIn.accountName = "account-b"
        try cache.seed("account-b", apps: ["1": ["b.sav": "bbbb"]])
        check(cache.read(1) == ["b.sav": "bbbb"], "switching account reloads its baseline rather than cached account A")

        SteamSignIn.accountName = "account-a"
        let reset = Accounts()
        reset.cachedAccount = "old-account-key"
        reset.cloud[1] = SteamCloudState(phase: .ready, checked: Date())
        reset.cloudBaseline = ["1": ["a.sav": "aaaa"]]
        let before = try reset.diskBytes()
        SteamSignIn.accountName = "account-b"
        reset.changed()
        check(reset.cloud.isEmpty && reset.cloudBaseline == nil && !reset.cloudAudited && reset.cloudWaitingFor == nil,
              "account notification clears only in-memory Cloud comparisons and loaded baseline")
        check(reset.cloudQuit == nil && !reset.cloudTakeover,
              "account notification invalidates the previous account's quit result and takeover")
        check((try? reset.diskBytes()) == before && (try? Data(contentsOf: deviceSave)) == preserved && (try? Data(contentsOf: backupSave)) == preserved,
              "account reset preserves persistent metadata, game progress and backups")

        for cancels in [false, true] {
            SteamSignIn.accountName = "account-a"
            let late = Accounts()
            late.delayed = true
            late.cancellation = cancels
            var old = SteamCloudEntry(path: "a.sav", kind: .same)
            old.cloudSHA = Data(repeating: 1, count: 20)
            old.localSHA = old.cloudSHA
            late.listing = SteamCloudListing(entries: [old])
            late.cloud[1] = SteamCloudState(phase: .ready, checked: Date())
            let pending = Task { await late.checkCloud(1) }
            while late.held == nil { await Task.yield() }
            SteamSignIn.accountName = "account-b"
            late.changed()
            try late.seed("account-b", apps: ["1": ["b.sav": "bbbb"]])
            let baselineB = try late.diskBytes()
            let newState = SteamCloudState(phase: .ready, checked: Date(), problem: "account B comparison")
            late.cloud[1] = newState
            late.held?.resume()
            _ = await pending.value
            check(late.cloud[1] == newState,
                  cancels ? "late cancellation from A cannot restore A's state over B" : "late successful A audit cannot publish into B's state")
            check((try? late.diskBytes()) == baselineB,
                  "late account A audit never writes its comparison into B's persistent baseline")
        }

        var closes: [CheckedContinuation<Void, Never>] = []
        var reopens = 0
        let gate = SteamConnectionGate(close: {
            await withCheckedContinuation { closes.append($0) }
        }, reopen: { reopens += 1 })
        let first = gate.close()
        while closes.isEmpty { await Task.yield() }
        var takeoverEntered = false
        let takeover = Task { await gate.takeOver(if: { takeoverEntered = true; return true }) }
        while !takeoverEntered { await Task.yield() }
        gate.reopen(sessionRunning: false)
        let second = gate.close()
        while closes.count < 2 { await Task.yield() }
        closes[0].resume()
        await first.value
        await takeover.value
        check(!gate.open && reopens == 1,
              "an old awaited takeover cannot reopen a newer session's closed connection")
        closes[1].resume()
        await second.value

        var valid = true
        var close: CheckedContinuation<Void, Never>?
        var accountReopens = 0
        let accountGate = SteamConnectionGate(close: {
            await withCheckedContinuation { close = $0 }
        }, reopen: { accountReopens += 1 })
        let closing = accountGate.close()
        while close == nil { await Task.yield() }
        var accountTakeoverEntered = false
        let accountTakeover = Task { await accountGate.takeOver(if: { accountTakeoverEntered = true; return valid }) }
        while !accountTakeoverEntered { await Task.yield() }
        valid = false
        close?.resume()
        await closing.value
        await accountTakeover.value
        check(!accountGate.open && accountReopens == 0,
              "takeover rechecks account validity after the close finishes before reconnecting")

        var invalidClose: CheckedContinuation<Void, Never>?
        var invalidFinished = false
        let invalidGate = SteamConnectionGate(close: {
            await withCheckedContinuation { invalidClose = $0 }
        }, reopen: {})
        let invalidClosing = invalidGate.close()
        while invalidClose == nil { await Task.yield() }
        let invalidTask = Task { await invalidGate.takeOver(if: { false }); invalidFinished = true }
        try? await Task.sleep(nanoseconds: 50_000_000)
        let returnedWithoutClose = invalidFinished
        invalidClose?.resume() // always release, including a failed regression
        await invalidClosing.value
        await invalidTask.value
        check(returnedWithoutClose, "an invalid takeover returns without waiting for an unrelated pending close")
        exit(failures == 0 ? 0 : 1)
    }
}
'''
    return HELPERS["run_check"](source, "Cloud synthetic account isolation")


if __name__ == "__main__":
    raise SystemExit(main())
