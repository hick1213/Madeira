#!/usr/bin/env python3
"""Run the complete production Epic library model against isolated dependencies.

Apple Combine, EpicLibrary, EpicRecords, EpicLaunch, EpicCatalog, EpicManifest,
EpicInstall and EpicDirectory
are real code. Only account/API/download operations and the surrounding library
are synthetic: no network, Keychain, Steam, device or user file is accessed.
Each scenario runs in a separate process because the production model owns a
private singleton initializer. Compiler files and reports stay on the external
IPAInspection disk. Model fixtures use scoped APFS temporary directories inside
the real NSHomeDirectory, matching production's trusted-container boundary.
Both are removed in finally. UIKit background expiry is
not executable in this macOS host suite; it requires device validation.
"""

from pathlib import Path
import argparse
import base64
from contextlib import ExitStack
import hashlib
import json
import os
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "app/Build/Products/IPAInspection/epic-container-regression/library"
SOURCES = [ROOT / "app/Madeira" / f"{name}.swift" for name in
           ("GameSessionWork", "EpicDirectory", "EpicLibrary", "EpicRecords", "EpicLaunch", "EpicCatalog", "EpicManifest", "EpicInstall")]

DOUBLES = r'''
import Foundation
import Combine
import CryptoKit

struct Failure: Error { let message: String }
// Diagnostics are a surrounding service boundary; no real log/UI/device data.
@MainActor final class LogStore {
    static let shared = LogStore()
    var messages: [String] = []
    func log(_ message: String) { messages.append(message) }
}
@MainActor final class EpicAccount: ObservableObject {
    static let shared = EpicAccount()
    @Published private(set) var sessionGeneration: UInt64 = 0
    @Published private(set) var accountID: String?
    @Published private(set) var signedIn = false
    @Published private(set) var displayName = ""
    // Matches production signOut's synchronous ordering, including willSet.
    func signOut() {
        sessionGeneration &+= 1
        signedIn = false; accountID = nil; displayName = ""
    }
    func accept(_ id: String) {
        accountID = id; displayName = "Fixture Player"; signedIn = true
    }
    func switchTo(_ id: String) { signOut(); accept(id) }
}
actor Gate {
    var entered = false
    var opened = false
    var waiters: [CheckedContinuation<Void, Never>] = []
    func stop() async {
        entered = true
        if opened { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func open() { opened = true; waiters.forEach { $0.resume() }; waiters.removeAll() }
    func isEntered() -> Bool { entered }
}
@MainActor final class EpicAPI {
    static let shared = EpicAPI()
    var gamesCalls: [String] = []
    var manifestCalls: [String] = []
    var exchangeCalls = 0
    var ownershipCalls: [String] = []
    var ownershipHandler: () async throws -> Data = { Data("synthetic-owned-token".utf8) }
    var gamesHandler: (String) async throws -> [EpicGame] = { _ in [] }
    var manifestHandler: @MainActor (EpicGame) async throws -> EpicManifestLocation = { game in
        let data = try EpicDownloader.manifestOverrides[game.appName] ?? EpicDownloader.fixture(game.appName)
        return EpicManifestLocation(url: URL(string: "https://fixture.invalid/" + game.appName)!,
                             chunkBaseURLs: [URL(string: "https://fixture.invalid/chunks")!],
                             sha1: EpicDownloader.sha1(data))
    }
    var exchangeHandler: () async throws -> String = { "synthetic_exchange_code_123456789" }
    func games() async throws -> [EpicGame] {
        try Task.checkCancellation()
        let id = EpicAccount.shared.accountID ?? "SIGNED_OUT"
        gamesCalls.append(id); return try await gamesHandler(id)
    }
    func manifest(game: EpicGame) async throws -> EpicManifestLocation {
        try Task.checkCancellation()
        manifestCalls.append(game.appName); return try await manifestHandler(game)
    }
    func exchangeCode() async throws -> String {
        try Task.checkCancellation()
        exchangeCalls += 1; return try await exchangeHandler()
    }
    func ownershipToken(game: EpicGame) async throws -> Data {
        ownershipCalls.append(game.appName)
        return try await ownershipHandler()
    }
}
struct LibraryEntry: Codable, Identifiable, Equatable, Sendable {
    var id = UUID()
    var title: String
    var relativePath: String
    var bits: Int
    var arguments = ""
    var resolution = "1144x800"
    var fpsMode = 1
    var controllerMode: String?
    var config: String?
    var steamAppID: Int?
    var epicAppName: String?
    var epicAccountKey: String?
    var folderBytes: Int64?
    var startServices: Bool?
    var desktop: Bool?
    var launchArguments: String {
        desktop == true ? "/desktop=shell,\(resolution) C:\\windows\\system32\\services.exe" : arguments
    }
    var isBatch: Bool { ["bat", "cmd"].contains(URL(fileURLWithPath: relativePath).pathExtension.lowercased()) }
}
enum LibraryError: Error { case message(String) }
@MainActor final class LibraryModel: ObservableObject {
    static let shared = LibraryModel()
    static var documents: URL { URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true) }
    static var drive: URL { documents.appendingPathComponent("wine/drive_c", isDirectory: true).resolvingSymlinksInPath() }
    @Published var current: UUID? { didSet { GameSessionWork.shared.reconcile() } }
    func pauseOptionalWork(_ paused: Bool) {}
    var entries: [LibraryEntry] = []
    var saves = 0
    func save(_ entry: LibraryEntry) {
        if let index = entries.firstIndex(where: { $0.id == entry.id }) { entries[index] = entry }
        else { entries.append(entry) }
        saves += 1
    }
    __EXECUTABLE_VALIDATION__
}
@MainActor final class SteamOwnedLibrary {
    static let shared = SteamOwnedLibrary()
    func sessionChanged(active: Bool) {}
    var hasInFlightDownload: Bool { false }
    var hasActiveCloudSaveOperation: Bool { false }
}
@MainActor func wine_process_is_running() -> Int32 { 0 }
@MainActor func wineserver_is_running() -> Int32 { 0 }
struct InstallPlan: Sendable {
    var gate: Gate?
    var omitExecutable = false
    var ignoreCancellation = false
    var validateRealStore = false
    var seedIncompleteStore = false
    var resumeRealStore = false
}
@MainActor enum EpicDownloader {
    enum CDNFailure: Error, Codable, Equatable, Sendable {
        case http(Int), network(Int), integrity, other
    }
    struct CDNMeasurement: Codable, Equatable, Sendable {
        let host: String
        let scheme: String
        let bytesPerSecond: Double?
        let failed: Bool
        var failure: CDNFailure? = nil
    }
    static var measurementCallbacks: [@Sendable (CDNMeasurement) -> Void] = []
    static var transferRateCallbacks: [@Sendable (Double) -> Void] = []
    static var plans: [InstallPlan] = []
    static var installApps: [String] = []
    static var destinations: [URL] = []
    static var callbacks: [@Sendable (Int64, Int64) -> Void] = []
    static var manifestOverrides: [String: Data] = [:]
    static var fetchErrors: [String: Error] = [:]
    static var manifestFetches: [String] = []
    static let ownedBinaryManifest = Data(base64Encoded: "__OWNED_BINARY_MANIFEST__")!
    static var realStoreRejections = 0
    static var realStoreManifestRejections = 0
    static func fixture(_ app: String) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "AppNameString": app, "LaunchExeString": "Bin/Game.exe", "LaunchCommand": "--from-manifest",
            "ChunkFilesizeList": [String: String](), "ChunkHashList": [String: String](),
            "ChunkShaList": [String: String](), "DataGroupList": [String: String](),
            "FileManifestList": [["Filename": "Bin/Game.exe", "FileHash": String(repeating: "000", count: 20),
                                  "FileChunkParts": [[String: String]]()]]], options: [.sortedKeys])
    }
    static func sha1(_ data: Data) -> String {
        Insecure.SHA1.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    static func fetchManifest(_ url: URL) async throws -> Data {
        try Task.checkCancellation()
        manifestFetches.append(url.lastPathComponent)
        if let error = fetchErrors[url.lastPathComponent] { throw error }
        return try manifestOverrides[url.lastPathComponent] ?? fixture(url.lastPathComponent)
    }
    static func install(manifestData: Data, baseURLs: [URL], destination: URL,
                        progress: @escaping @Sendable (Int64, Int64) -> Void,
                        onTransferRate: (@Sendable (Double) -> Void)? = nil,
                        onCDNMeasurement: (@Sendable (CDNMeasurement) -> Void)? = nil) async throws -> EpicInstallResult {
        let manifest = try EpicManifest.parse(manifestData)
        let index = installApps.count
        let plan = index < plans.count ? plans[index] : InstallPlan()
        installApps.append(manifest.appName); destinations.append(destination); callbacks.append(progress)
        if let onCDNMeasurement { measurementCallbacks.append(onCDNMeasurement) }
        if let onTransferRate { transferRateCallbacks.append(onTransferRate) }
        if plan.validateRealStore {
            // Both Documents and the link target are owned APFS fixtures in
            // real Home. Run the actual store even for the old resolved caller:
            // a Home-prefix guard must not hide its ancestor-link regression.
            do {
                let store = try EpicInstallStore(destination: destination, manifestData: manifestData, appName: manifest.appName)
                withExtendedLifetime(store) {}
                throw Failure(message: "linked installer ancestor was accepted")
            } catch EpicDownloadError.unsafeDestination {
                realStoreRejections += 1
                throw EpicDownloadError.unsafeDestination
            }
        }
        if plan.resumeRealStore {
            do {
                let store = try EpicInstallStore(destination: destination, manifestData: manifestData, appName: manifest.appName)
                withExtendedLifetime(store) {}
                throw Failure(message: "different manifest was unexpectedly adopted")
            } catch EpicDownloadError.differentInstall {
                realStoreManifestRejections += 1
                throw EpicDownloadError.differentInstall
            }
        }
        var ownedStore: EpicInstallStore?
        if plan.seedIncompleteStore {
            ownedStore = try EpicInstallStore(destination: destination, manifestData: manifestData, appName: manifest.appName)
            let file = destination.appendingPathComponent(manifest.launchExecutable)
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data().write(to: file)
        }
        defer { withExtendedLifetime(ownedStore) {} }
        progress(50, 100)
        if let gate = plan.gate { await gate.stop() }
        if !plan.ignoreCancellation { try Task.checkCancellation() }
        if !plan.omitExecutable && !plan.seedIncompleteStore {
            let executable = destination.appendingPathComponent(manifest.launchExecutable)
            try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("MZ synthetic download output".utf8).write(to: executable)
        }
        progress(100, 100)
        return EpicInstallResult(executable: manifest.launchExecutable, arguments: manifest.launchCommand, totalBytes: 100)
    }
}
'''

HARNESS = r'''
import Foundation
import Combine
import CryptoKit

@main struct Run {
    @MainActor static func main() async {
        do { try await run(CommandLine.arguments[1]) }
        catch let error as Failure { print("FAIL: " + error.message); exit(1) }
        catch { print("FAIL: unexpected error " + String(describing: type(of: error))); exit(1) }
    }
    @MainActor static func settle() async {
        for _ in 0..<12 { await Task.yield() }
        try? await Task.sleep(for: .milliseconds(10))
    }
    @MainActor static func until(_ message: String, _ condition: () async -> Bool) async throws {
        let deadline = Date().addingTimeInterval(3)
        while !(await condition()) {
            guard Date() < deadline else { throw Failure(message: "timed out: " + message) }
            await settle()
        }
    }
    @MainActor static func run(_ scenario: String) async throws {
        var checks = 0
        func check(_ value: @autoclosure () throws -> Bool, _ message: String) throws {
            guard try value() else { throw Failure(message: message) }; checks += 1
        }
        let account = EpicAccount.shared, api = EpicAPI.shared, library = LibraryModel.shared
        let fm = FileManager.default
        try fm.createDirectory(at: LibraryModel.drive, withIntermediateDirectories: true)
        let sentinel = LibraryModel.documents.appendingPathComponent("steam-sentinel.bin")
        try Data("KEEP STEAM".utf8).write(to: sentinel)
        let store = try EpicRecordStore(documents: LibraryModel.documents)
        func game(_ name: String) -> EpicGame {
            EpicGame(appName: name, namespace: "fixture", catalogItemID: name, title: name)
        }
        let a = "account-A", b = "account-B", keyA = EpicLibraryModel.key(a), keyB = EpicLibraryModel.key(b)
        func cache(_ id: String, _ games: [EpicGame]) throws {
            try store.write(JSONEncoder().encode(games), name: "library-" + EpicLibraryModel.key(id) + ".json")
        }
        func cached(_ id: String) throws -> [EpicGame] {
            let data = try store.read(name: "library-" + EpicLibraryModel.key(id) + ".json")!
            return try JSONDecoder().decode([EpicGame].self, from: data)
        }
        func queue() throws -> [EpicDownload] {
            try JSONDecoder().decode([EpicDownload].self, from: store.read(name: "downloads.json") ?? Data("[]".utf8))
        }
        func seedInstall(_ name: String, _ key: String, missing: Bool = false,
                         requirement: String? = nil, ownership: Bool = false,
                         suffix: String = ".exe") throws -> EpicInstallation {
            var g = game(name); g.launchRequirement = requirement; g.requiresOwnershipToken = ownership
            let relative = "Program Files/Epic Games/fixture-" + key.prefix(12) + "/" + name + suffix
            if !missing {
                let url = LibraryModel.drive.appendingPathComponent(relative)
                try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                try Data("MZ fixture".utf8).write(to: url)
            }
            return EpicInstallation(game: g, accountKey: key, relativePath: relative, arguments: "--windowed", bytes: 123)
        }
        func cancellationManifest(_ builder: String = "InternalBuilder") throws -> Data {
            var json = try JSONSerialization.jsonObject(with: EpicDownloader.fixture(builder)) as! [String: Any]
            json["FileManifestList"] = [["Filename": "Bin/Game.exe", "FileHash": Insecure.SHA1.hash(data: Data()).map { String(format: "%03d", Int($0)) }.joined(), "FileChunkParts": [[String: String]]()]]
            return try JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])
        }
        func cancelRoot(_ name: String, _ key: String) -> URL {
            LibraryModel.documents.appendingPathComponent("wine/drive_c/Program Files/Epic Games/" + key.prefix(12) + "-" + EpicLibraryModel.key(name).prefix(20))
        }
        func seedPaused(_ name: String = "one", _ key: String = keyA, complete: Bool = false) throws -> URL {
            let root = cancelRoot(name, key), data = try cancellationManifest()
            let installer = try EpicInstallStore(destination: root, manifestData: data, appName: "InternalBuilder")
            let file = root.appendingPathComponent("Bin/Game.exe")
            try fm.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data().write(to: file)
            if complete { try installer.complete(chunks: [:]) }
            withExtendedLifetime(installer) {}
            return root
        }
        switch scenario {
        case "signed-out":
            try cache(a, [game("owned-A")])
            let install = try seedInstall("offline", keyA)
            try store.write(JSONEncoder().encode([install]), name: "installations.json")
            let model = EpicLibraryModel.shared
            model.refresh(force: true); await settle()
            try check(model.games.isEmpty && !model.refreshing, "signed-out never exposes owned cache")
            try check(api.gamesCalls.isEmpty, "signed-out refresh never calls API")
            model.enqueue(game("new"))
            try check(model.downloads.isEmpty && model.error != nil, "signed-out download rejected")
            try check(model.visibleInstallations.count == 1 && model.installed("offline") != nil, "offline installations retained")
            let entry = library.epicEntry(install)
            do { _ = try await model.launch(entry); throw Failure(message: "signed-out authenticated launch accepted") }
            catch EpicError.notSignedIn { checks += 1 }
            try check(api.exchangeCalls == 0 && api.manifestCalls.isEmpty, "signed-out no exchange/manifest")
            try check(try cached(a).map(\.appName) == ["owned-A"], "sign-out never deletes old cache")
        case "logout-refresh":
            account.accept(a); try cache(a, [game("cached-A")])
            let gate = Gate()
            api.gamesHandler = { _ in await gate.stop(); return [game("stale-A")] }
            let model = EpicLibraryModel.shared
            try check(model.games.map(\.appName) == ["cached-A"], "matching account cache restored")
            model.refresh(force: true)
            try await until("refresh entered") { await gate.isEntered() }
            model.refresh(force: true)
            try check(api.gamesCalls == [a], "refresh single-flight")
            var updates: [[String]] = []
            let observer = model.$games.dropFirst().sink { updates.append($0.map(\.appName)) }
            account.signOut(); await settle()
            try check(model.games.isEmpty && !model.refreshing && model.accountKey == nil, "committed sign-out clears owned view")
            try check(updates.allSatisfy(\.isEmpty), "willSet observer never restores previous account cache")
            await gate.open(); await settle()
            try check(model.games.isEmpty && !model.refreshing, "stale completed refresh rejected")
            try check(api.gamesCalls == [a], "logout does not start old-account fetch")
            try check(try cached(a).map(\.appName) == ["cached-A"], "cancelled refresh does not persist stale result")
            withExtendedLifetime(observer) {}
        case "session-refresh":
            account.accept(a); try cache(a, [game("cached-A")])
            let gate = Gate()
            api.gamesHandler = { _ in await gate.stop(); return [game("stale-A")] }
            let model = EpicLibraryModel.shared
            model.refresh(force: true); try await until("catalogue entered") { await gate.isEntered() }
            model.sessionChanged(active: true)
            try check(!model.refreshing, "entering a game cancels optional catalogue refresh")
            model.refresh(force: true); await settle()
            try check(api.gamesCalls == [a], "game session defers new catalogue requests")
            await gate.open(); await settle()
            try check(model.games.map(\.appName) == ["cached-A"], "cancelled catalogue cannot publish during game")
            try check(try cached(a).map(\.appName) == ["cached-A"], "cancelled catalogue does not replace cache")
            model.sessionChanged(active: false)
            try await until("deferred catalogue resumes") { model.games.first?.appName == "stale-A" }
            try check(api.gamesCalls == [a, a] && !model.refreshing, "one deferred refresh resumes after game")
        case "session-refresh-identity":
            account.accept(a); try cache(a, [game("cached-A")])
            let ga = Gate(), gb = Gate()
            var call = 0
            api.gamesHandler = { _ in
                call += 1
                if call == 1 { await ga.stop(); return [game("stale-A")] }
                await gb.stop(); return [game("fresh-A")]
            }
            let model = EpicLibraryModel.shared
            model.refresh(force: true); try await until("first refresh entered") { await ga.isEntered() }
            model.sessionChanged(active: true); model.sessionChanged(active: false)
            try await until("new refresh entered") { await gb.isEntered() }
            await ga.open(); await settle()
            try check(model.refreshing && model.games.map(\.appName) == ["cached-A"], "stale cancelled task cannot clear current refresh")
            model.refresh(force: true); await settle()
            try check(api.gamesCalls == [a, a], "stale cleanup cannot enable a parallel third refresh")
            await gb.open(); try await until("new refresh commits") { model.games.first?.appName == "fresh-A" }
            try check(!model.refreshing && (try cached(a)).map(\.appName) == ["fresh-A"], "only live catalogue request persists")
        case "save-session":
            account.accept(a)
            let model = EpicLibraryModel.shared
            let token = try GameSessionWork.shared.beginSaveOperation()
            model.enqueue(game("one")); model.refresh(force: true); await settle()
            try check(model.download("one")?.state == "waiting", "local save operation pauses Epic queue")
            try check(api.gamesCalls.isEmpty && api.manifestCalls.isEmpty, "local save operation makes no background catalogue or manifest request")
            model.pause("one")
            GameSessionWork.shared.endSaveOperation(token)
            try await until("save operation refresh resumed") { !api.gamesCalls.isEmpty }
            try check(model.download("one")?.state == "paused" && api.manifestCalls.isEmpty, "manual pause survives local save completion")
        case "save-drain":
            account.accept(a)
            let gate = Gate()
            EpicDownloader.plans = [InstallPlan(gate: gate, ignoreCancellation: true)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("save pre-existing install") { await gate.isEntered() }
            let work = GameSessionWork.shared, token = try GameSessionWork.shared.beginSaveOperation()
            var ready = false
            let wait = Task { @MainActor in try await work.waitForSaveOperation(token); ready = true }
            await settle()
            try check(!ready && model.hasInFlightDownload && model.download("one")?.state == "waiting", "save waits for real cancelled download model task")
            await gate.open(); try await wait.value
            try check(ready && !model.hasInFlightDownload && model.installed("one") == nil, "cancel-ignoring worker drains before save proceeds")
            try check(model.download("one")?.state == "waiting", "save keeps resumable queue waiting while exclusive")
            model.pause("one"); work.endSaveOperation(token); await settle()
            try check(model.download("one")?.state == "paused", "explicit user pause wins after real transfer drain")
        case "switch-refresh":
            account.accept(a); try cache(a, [game("cached-A")]); try cache(b, [game("cached-B")])
            let ga = Gate(), gb = Gate()
            api.gamesHandler = { id in
                if id == a { await ga.stop(); return [game("stale-A")] }
                await gb.stop(); return [game("fresh-B")]
            }
            let model = EpicLibraryModel.shared
            model.refresh(force: true); try await until("A refresh") { await ga.isEntered() }
            account.switchTo(b); await settle()
            try await until("B refresh") { await gb.isEntered() }
            try check(model.games.map(\.appName) == ["cached-B"] && model.accountKey == keyB, "switch restores B cache only")
            try check(api.gamesCalls == [a, b], "one owned fetch for committed B session")
            await gb.open(); try await until("B committed") { model.games.first?.appName == "fresh-B" }
            await ga.open(); await settle()
            try check(model.games.map(\.appName) == ["fresh-B"] && !model.refreshing, "A completion cannot replace B")
            try check(try cached(a).map(\.appName) == ["cached-A"], "A stale result not written")
            try check(try cached(b).map(\.appName) == ["fresh-B"], "B fresh result written to separate key")
        case "cancel-persist-failure", "cancel-identity-persist-failure":
            account.accept(a)
            let root = try seedPaused()
            try store.write(JSONEncoder().encode([EpicDownload(game: game("one"), accountKey: keyA)]), name: "downloads.json")
            let model = EpicLibraryModel.shared
            let queueFile = LibraryModel.documents.appendingPathComponent("epic/downloads.json")
            let link = LibraryModel.documents.appendingPathComponent("queue-hardlink")
            var linked = false
            let observer = model.$downloads.sink { incoming in
                if scenario == "cancel-identity-persist-failure", !linked,
                   incoming.contains(where: { $0.cleanupIdentity != nil }) {
                    linked = true
                    try? fm.linkItem(at: queueFile, to: link)
                }
            }
            if scenario == "cancel-persist-failure" { try fm.linkItem(at: queueFile, to: link) }
            model.cancel("one")
            if scenario == "cancel-identity-persist-failure" {
                try await until("identity save rejected") { model.download("one")?.state == "cancel-failed" }
                try check(linked && model.download("one")?.cleanupIdentity != nil, "identity persistence failure reached after inspection")
            } else {
                try check(model.download("one")?.cancellationID == nil && model.download("one")?.state == "paused", "intent write failure restores original queue")
            }
            try check(model.error != nil && fm.fileExists(atPath: root.appendingPathComponent("Bin/Game.exe").path), "failed persistence prevents file deletion")
            try fm.removeItem(at: link)
            model.cancel("one"); try await until("cleanup retries after record save recovers") { model.download("one") == nil }
            try check(!fm.fileExists(atPath: root.path) && (try queue()).isEmpty, "recovery clears files then saves empty queue")
            withExtendedLifetime(observer) {}
        case "cancel-updated-manifest":
            account.accept(a)
            let root = try seedPaused()
            var job = EpicDownload(game: game("one"), accountKey: keyA)
            let oldHash = EpicInstallStore.digest(try cancellationManifest())
            job.manifestHash = oldHash
            try store.write(JSONEncoder().encode([job]), name: "downloads.json")
            EpicDownloader.manifestOverrides["one"] = try cancellationManifest("UpdatedBuilder")
            EpicDownloader.plans = [InstallPlan(resumeRealStore: true)]
            let model = EpicLibraryModel.shared
            model.resume("one"); try await until("updated manifest rejected") { model.download("one")?.state == "failed" }
            try check(EpicDownloader.realStoreManifestRejections == 1, "real store specifically rejects differentInstall")
            try check(model.download("one")?.manifestHash == oldHash, "rejected new manifest does not replace on-disk ownership digest")
            model.cancel("one"); try await until("old unfinished manifest can be cleared") { model.download("one") == nil }
            try check(!fm.fileExists(atPath: root.path), "old owned root removed after upstream update")
        case "cancel-paused", "cancel-retry", "cancel-reload", "cancel-completed":
            account.accept(a)
            let root = try seedPaused(complete: scenario == "cancel-completed")
            let otherRoot = try seedPaused("sibling", keyB)
            var job = EpicDownload(game: game("one"), accountKey: keyA)
            if scenario == "cancel-reload" { job.cancellationID = UUID(); job.state = "cancelling" }
            try store.write(JSONEncoder().encode([job]), name: "downloads.json")
            let unknown = root.appendingPathComponent("user-save.txt")
            if scenario == "cancel-retry" { try Data("KEEP USER SAVE".utf8).write(to: unknown) }
            let model = EpicLibraryModel.shared
            if scenario == "cancel-reload" {
                try check(model.download("one")?.state == "cancel-failed", "interrupted cleanup restores retry state")
                model.resume("one"); await settle()
                try check(api.manifestCalls.isEmpty, "interrupted cleanup cannot resume downloading")
            }
            model.cancel("one")
            if scenario == "cancel-completed" {
                try await until("completed cancellation refused") { model.download("one")?.error != nil }
                try check(model.download("one")?.cancellationID == nil && model.download("one")?.state == "paused", "complete files return to resumable registration")
                try check(fm.fileExists(atPath: root.appendingPathComponent(".madeira-epic/complete").path), "complete install kept")
            } else {
                if scenario == "cancel-retry" {
                    try await until("unsafe cleanup refused") { model.download("one")?.state == "cancel-failed" }
                    try check(fm.fileExists(atPath: root.appendingPathComponent("Bin/Game.exe").path), "preflight failure keeps unfinished executable")
                    try check(try Data(contentsOf: unknown) == Data("KEEP USER SAVE".utf8), "preflight keeps user file")
                    let saved = try queue().first!
                    try check(saved.cancellationID != nil, "failed cleanup intent persists")
                    model.resume("one"); await settle()
                    try check(api.manifestCalls.isEmpty, "failed cleanup cannot resume")
                    try fm.removeItem(at: unknown)
                    model.cancel("one")
                }
                try await until("paused files and queue cleared") { model.download("one") == nil }
                try check(!fm.fileExists(atPath: root.path), "full unfinished root removed")
                try check(try queue().isEmpty, "queue removal persisted after cleanup")
            }
            try check(fm.fileExists(atPath: otherRoot.appendingPathComponent("Bin/Game.exe").path), "other account and game untouched")
            try check(api.manifestCalls.isEmpty && EpicDownloader.installApps.isEmpty, "cleanup never starts download")
        case "cancel-drain", "cancel-switch":
            account.accept(a)
            let gate = Gate(), nextGate = Gate()
            EpicDownloader.manifestOverrides["one"] = try cancellationManifest()
            EpicDownloader.plans = [InstallPlan(gate: gate, ignoreCancellation: true, seedIncompleteStore: true), InstallPlan(gate: nextGate)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("writer entered") { await gate.isEntered() }
            let root = EpicDownloader.destinations[0]
            model.pause("one"); model.cancel("one"); model.resume("one")
            await settle()
            try check(model.download("one")?.state == "cancelling" && model.hasInFlightDownload, "cancel waits for real writer completion")
            try check(fm.fileExists(atPath: root.appendingPathComponent("Bin/Game.exe").path), "no deletion while writer ignores cancellation")
            try check(api.manifestCalls == ["one"], "resume blocked while cancelling")
            try check(try queue().first?.manifestHash != nil && (try queue()).first?.cancellationID != nil, "trusted manifest and cancellation intent persisted")
            if scenario == "cancel-switch" {
                account.switchTo(b); await settle(); model.enqueue(game("one"))
                try check(model.download("one")?.state == "queued", "new account queue waits behind cleanup")
            }
            await gate.open()
            try await until("old account cleaned") { !model.downloads.contains { $0.id == "one" && $0.accountKey == keyA } }
            try check(!fm.fileExists(atPath: root.path), "writer drained then root removed")
            try check(model.installations.isEmpty && library.entries.isEmpty, "cancelled writer never registers")
            if scenario == "cancel-switch" {
                try await until("new account starts after cleanup") { await nextGate.isEntered() }
                try check(EpicDownloader.destinations[1] != root && model.download("one")?.accountKey == keyB, "cleanup does not remove same-name new-account job")
                await nextGate.open(); try await until("new account installed") { model.installed("one") != nil }
            } else {
                try check(!model.hasInFlightDownload && (try queue()).isEmpty, "no worker or queue left after cancellation")
            }
        case "cancel-empty":
            account.accept(a)
            try store.write(JSONEncoder().encode([EpicDownload(game: game("one"), accountKey: keyA)]), name: "downloads.json")
            let model = EpicLibraryModel.shared
            try check(model.download("one")?.state == "paused", "legacy download is paused after restore")
            model.cancel("one")
            try await until("cancel removes empty legacy job") { model.download("one") == nil }
            try check(try queue().isEmpty, "cancelled queue removal persisted")
            try check(api.manifestCalls.isEmpty && EpicDownloader.installApps.isEmpty, "cancel does not fetch or resume")
            try check(try Data(contentsOf: sentinel) == Data("KEEP STEAM".utf8), "cancel preserves Steam sentinel")
        case "queue-resume":
            account.accept(a)
            let g1 = Gate(), g2 = Gate(), g3 = Gate()
            EpicDownloader.plans = [InstallPlan(gate: g1), InstallPlan(gate: g2), InstallPlan(gate: g3)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("first install") { await g1.isEntered() }
            model.enqueue(game("two")); model.enqueue(game("one")); await settle()
            try check(model.downloads.count == 2 && model.download("two")?.state == "queued", "one active download and deduplicated queue")
            try check(try queue().count == 2, "real queue persisted")
            model.pause("one"); model.resume("one")
            try check(model.download("one")?.state == "queued", "immediate resume records new intent")
            await g1.open(); try await until("resumed install") { await g2.isEntered() }
            try check(EpicDownloader.installApps == ["one", "one"], "cancel finishing pumps resumed job once")
            try check(model.download("one")?.state == "downloading" && model.download("one")?.error == nil, "cancel does not overwrite resumed state")
            await g2.open(); try await until("second job") { await g3.isEntered() }
            try check(model.installed("one") != nil && model.download("one") == nil, "completed job registered and removed")
            await g3.open(); try await until("queue empty") { model.downloads.isEmpty }
            try check(model.installations.count == 2 && library.entries.count == 2 && library.saves == 2, "queued jobs each register once")
            try check(try queue().isEmpty, "drained queue persisted")
        case "transfer-speed":
            account.accept(a)
            let ga = Gate(), resumed = Gate(), gb = Gate()
            EpicDownloader.plans = [InstallPlan(gate: ga, ignoreCancellation: true),
                InstallPlan(gate: resumed, ignoreCancellation: true), InstallPlan(gate: gb)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("A live transfer") { await ga.isEntered() }
            await settle()
            try check(EpicDownloader.transferRateCallbacks.count == 1, "model observes live transfer rate separately from CDN measurements")
            let oldRate = EpicDownloader.transferRateCallbacks[0]
            let initialJob = model.download("one")!
            let persistedBefore = try store.read(name: "downloads.json")
            for rate in [Double.nan, Double.infinity, -Double.infinity, -1, 0] { oldRate(rate) }
            await settle()
            try check(model.transferSpeed == nil && model.downloadSpeed(initialJob) == nil,
                      "nonfinite and nonpositive transfer rates never create a speed sample")
            oldRate(2048); await settle()
            guard let firstSample = model.transferSpeed else { throw Failure(message: "positive live transfer rate was not recorded") }
            try check(firstSample.id == "one" && firstSample.key == keyA && firstSample.bytesPerSecond == 2048 &&
                      firstSample.updatedAt.isFinite && model.downloadSpeed(initialJob) == 2048,
                      "current active account/job exposes its positive transfer rate")
            oldRate(4096); await settle()
            try check(model.transferSpeed?.bytesPerSecond == 2048,
                      "rapid live transfer samples are throttled to one update per second")
            try await Task.sleep(for: .milliseconds(1100))
            oldRate(4096); await settle()
            guard let freshSample = model.transferSpeed else { throw Failure(message: "refreshed live transfer sample missing") }
            try check(freshSample.bytesPerSecond == 4096 && freshSample.updatedAt > firstSample.updatedAt,
                      "later valid transfer sample refreshes the live rate")
            try check(model.downloadSpeed(initialJob, now: freshSample.updatedAt + 2.9) == 4096 &&
                      model.downloadSpeed(initialJob, now: freshSample.updatedAt + 3.01) == nil,
                      "live speed expires after three seconds without a new transfer sample")
            try check(try store.read(name: "downloads.json") == persistedBefore,
                      "live speed samples do not rewrite or add fields to persisted download queue")
            model.pause("one"); oldRate(8192); await settle()
            try check(model.downloadSpeed(model.download("one")!) == nil && model.transferSpeed?.bytesPerSecond != 8192,
                      "late transfer callback cannot revive speed for a paused job")
            model.resume("one"); await ga.open()
            try await until("resumed live transfer") { await resumed.isEntered() }; await settle()
            let resumedJob = model.download("one")!
            try check(EpicDownloader.transferRateCallbacks.count == 2 && model.downloadSpeed(resumedJob) == nil,
                      "resumed task begins without the previous task's speed sample")
            oldRate(16384); await settle()
            try check(model.downloadSpeed(resumedJob) == nil,
                      "old task callback cannot revive a same-account same-name resumed job")
            let resumedRate = EpicDownloader.transferRateCallbacks[1]
            resumedRate(8192); await settle()
            try check(model.downloadSpeed(resumedJob) == 8192, "new task identity can publish its own live rate")
            account.switchTo(b); await settle(); model.enqueue(game("one"))
            oldRate(32768); resumedRate(32768); await settle()
            try check(model.downloadSpeed(model.download("one")!) == nil,
                      "old-account callback cannot show speed on the new account's same-name queued job")
            await resumed.open(); try await until("B live transfer") { await gb.isEntered() }; await settle()
            let bJob = model.download("one")!
            oldRate(32768); resumedRate(32768); await settle()
            try check(EpicDownloader.transferRateCallbacks.count == 3 && model.downloadSpeed(bJob) == nil,
                      "stale account/task callbacks cannot publish while new account is active")
            let bRate = EpicDownloader.transferRateCallbacks[2]
            bRate(16384); await settle()
            try check(model.transferSpeed?.key == keyB && model.downloadSpeed(bJob) == 16384,
                      "new account's active transfer reports its own speed")
            await gb.open(); try await until("B live transfer completes") { model.installed("one") != nil }
            bRate(65536); await settle()
            try check(model.downloadSpeed(bJob) == nil && model.transferSpeed?.bytesPerSecond != 65536,
                      "completed transfer callback cannot restore a live speed")
        case "cdn-measurements":
            account.accept(a)
            let ga = Gate(), gb = Gate()
            EpicDownloader.plans = [InstallPlan(gate: ga, ignoreCancellation: true), InstallPlan(gate: gb)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("A CDN transfer") { await ga.isEntered() }
            try check(EpicDownloader.measurementCallbacks.count == 1, "model observes downloader CDN measurements")
            let callback = EpicDownloader.measurementCallbacks[0]
            callback(.init(host: "measured.steamcontent.com", scheme: "https", bytesPerSecond: 1000, failed: false))
            await settle()
            try check(model.download("one")?.cdnResults?.contains(where: { $0.host == "measured.steamcontent.com" && $0.bytesPerSecond == 1000 }) == true, "verified speed visible in current download")
            try check(try queue().first?.cdnResults?.contains(where: { $0.host == "measured.steamcontent.com" }) == true, "speed result persists without URL or query")
            model.pause("one")
            callback(.init(host: "after-pause.steamcontent.com", scheme: "https", bytesPerSecond: 9999, failed: false)); await settle()
            try check(model.download("one")?.cdnResults?.contains(where: { $0.host == "after-pause.steamcontent.com" }) != true, "late paused callback cannot update results")
            account.switchTo(b); await settle(); model.enqueue(game("one"))
            await ga.open(); try await until("B CDN transfer") { await gb.isEntered() }
            callback(.init(host: "old-account.steamcontent.com", scheme: "https", bytesPerSecond: 9999, failed: false)); await settle()
            try check(model.download("one")?.cdnResults?.contains(where: { $0.host == "old-account.steamcontent.com" }) != true, "old-account measurement cannot affect same-name job")
            let current = EpicDownloader.measurementCallbacks[1]
            var rejected = EpicDownloader.CDNMeasurement(host: "b.steamcontent.com", scheme: "https", bytesPerSecond: nil, failed: true)
            rejected.failure = .http(403)
            current(rejected); await settle()
            try check(model.download("one")?.cdnResults?.contains(where: { $0.host == "b.steamcontent.com" && $0.failed }) == true, "failed CDN retained with unavailable result")
            try check(try queue().first(where: { $0.accountKey == keyB })?.cdnResults?.contains(where: { $0.host == "b.steamcontent.com" && $0.failure == .http(403) }) == true,
                      "typed HTTP failure persists without a signed URL or error body")
            await gb.open(); try await until("B CDN install") { model.installed("one") != nil }
        case "switch-download":
            account.accept(a)
            let ga = Gate(), gb = Gate()
            EpicDownloader.plans = [InstallPlan(gate: ga, ignoreCancellation: true), InstallPlan(gate: gb)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("same")); try await until("A install") { await ga.isEntered() }; await settle()
            let oldProgress = EpicDownloader.callbacks[0]
            try check(model.download("same")?.received == 50, "current progress delivered")
            account.switchTo(b); await settle()
            try check(model.visibleDownloads.isEmpty && model.downloads.first?.state == "paused", "A queue paused and hidden after switch")
            model.enqueue(game("same")); oldProgress(999, 999); await settle()
            try check(model.download("same")?.received == 0, "stale progress cannot modify same-name B job")
            await ga.open(); try await until("B install") { await gb.isEntered() }; await settle()
            try check(model.installations.isEmpty && library.entries.isEmpty, "cancel-ignoring A result never registered")
            try check(EpicDownloader.destinations.count == 2 && EpicDownloader.destinations[0] != EpicDownloader.destinations[1], "account-specific installation roots")
            oldProgress(777, 777); await settle()
            try check(model.download("same")?.received == 50, "old callback ignored while B is active")
            await gb.open(); try await until("B installed") { model.installed("same") != nil }
            try check(model.installations.count == 1 && model.installations[0].accountKey == keyB, "only B receipt committed")
            try check(model.downloads.count == 1 && model.downloads[0].accountKey == keyA && model.downloads[0].state == "paused", "A queue retained for explicit resume")
        case "registration":
            account.accept(a)
            var existing = LibraryEntry(title: "User title", relativePath: "old/game.exe", bits: 64)
            existing.epicAppName = "one"; existing.epicAccountKey = keyA
            existing.arguments = "--user-setting"; existing.resolution = "1920x1080"; existing.fpsMode = 3
            existing.controllerMode = "keys"; existing.config = "env.TEST = 1"
            var steam = LibraryEntry(title: "Steam untouched", relativePath: "steam/original", bits: 64); steam.steamAppID = 42
            library.entries = [existing, steam]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("registration") { model.download("one") == nil && model.installed("one") != nil }
            let entry = library.entries.first { $0.epicAppName == "one" }!
            try check(entry.id == existing.id && entry.title == existing.title, "update preserves user identity/title")
            try check(entry.arguments == existing.arguments && entry.resolution == existing.resolution && entry.fpsMode == 3, "update preserves arguments/resolution/FPS")
            try check(entry.controllerMode == "keys" && entry.config == existing.config, "update preserves controller/config")
            try check(entry.relativePath != existing.relativePath && entry.folderBytes == 100, "update refreshes executable/size")
            try check(library.entries.first { $0.steamAppID == 42 } == steam && library.entries.count == 2, "Steam entry unchanged")
            let receipts = try JSONDecoder().decode([EpicInstallation].self, from: store.read(name: "installations.json")!)
            try check(receipts.count == 1 && receipts[0].relativePath == entry.relativePath && receipts[0].arguments == "--from-manifest", "real validated receipt written before registration")
        case "missing-executable":
            account.accept(a); EpicDownloader.plans = [InstallPlan(omitExecutable: true)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("missing")); try await until("failed validation") { model.download("missing")?.state == "failed" }
            try check(model.installations.isEmpty && library.entries.isEmpty && library.saves == 0, "unvalidated download never registers")
            try check(try store.read(name: "installations.json") == nil, "unvalidated download never writes receipt")
            try check(model.download("missing")?.error != nil && (try queue()).first?.state == "failed", "failure persisted with safe message")
        case "manifest-builder-name":
            account.accept(a)
            let data = EpicDownloader.ownedBinaryManifest
            let parsed = try EpicManifest.parse(data)
            try check(parsed.appName == "EpicNativeFixture", "owned binary has a distinct internal builder name")
            EpicDownloader.manifestOverrides["one"] = data
            let gate = Gate()
            EpicDownloader.plans = [InstallPlan(gate: gate)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one"))
            try await until("builder identity preparation result") {
                await gate.isEntered() || model.download("one")?.state == "failed"
            }
            let entered = await gate.isEntered()
            try check(entered, "matching authenticated raw SHA must permit a different internal binary name")
            try check(api.manifestCalls == ["one"], "manifest request retains the selected public asset identity")
            try check(LogStore.shared.messages == ["[epic-download] manifest-name-alias accepted"],
                      "accepted alias diagnostic contains no identifier values")
            try check(EpicDownloader.installApps == ["EpicNativeFixture"], "installer retains the binary manifest name")
            try check(model.installations.isEmpty && library.entries.isEmpty && library.saves == 0,
                      "valid manifest cannot register before installation completes")
            try check(try store.read(name: "installations.json") == nil, "no early receipt for verified manifest")
            let publicFolder = String(keyA.prefix(12)) + "-" + String(EpicLibraryModel.key("one").prefix(20))
            try check(EpicDownloader.destinations[0].lastPathComponent == publicFolder,
                      "destination identity stays the public asset and account")
            await gate.open(); try await until("different builder installed") { model.installed("one") != nil }
            let installed = model.installed("one")!
            let receipts = try JSONDecoder().decode([EpicInstallation].self, from: store.read(name: "installations.json")!)
            try check(installed.game.appName == "one" && receipts.first?.game.appName == "one",
                      "installation and persisted receipt retain public asset identity")
            let entry = library.entries[0]
            try check(entry.epicAppName == "one" && entry.relativePath == installed.relativePath,
                      "library registration retains public identity")
            let launch = try await model.launch(entry)
            try check(launch.arguments.contains("-epicapp=one") && !launch.arguments.contains("-epicapp=EpicNativeFixture"),
                      "authenticated launch uses the public asset identity")
        case "manifest-hash", "manifest-missing-hash":
            account.accept(a)
            api.manifestHandler = { g in EpicManifestLocation(url: URL(string: "https://fixture.invalid/" + g.appName)!,
                chunkBaseURLs: [], sha1: scenario == "manifest-hash" ? String(repeating: "0", count: 40) : nil) }
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("manifest digest result") {
                model.download("one")?.state == "failed" || model.installed("one") != nil
            }
            try check(model.download("one")?.state == "failed", "an API digest is mandatory before installation")
            try check(EpicDownloader.installApps.isEmpty && model.installations.isEmpty && library.saves == 0,
                      "missing or wrong API digest rejected before install/registration")
            try check(try store.read(name: "installations.json") == nil, "unverified digest writes no receipt")
            try check(model.download("one")?.error == EpicError.invalidResponse.localizedDescription, "manifest failure safe and specific")
            try check(LogStore.shared.messages.last == "[epic-download] stage=manifest-hash category=invalid-response",
                      "hash failure diagnostics contain only fixed stage/category")
        case "manifest-fallback", "manifest-fallback-cancel", "manifest-fallback-hash":
            account.accept(a)
            let data = try EpicDownloader.fixture("one")
            EpicDownloader.manifestOverrides["backup"] = data
            let first = "unavailable"
            if scenario == "manifest-fallback-hash" {
                EpicDownloader.manifestOverrides[first] = Data("untrusted manifest".utf8)
            } else {
                EpicDownloader.fetchErrors[first] = scenario == "manifest-fallback-cancel"
                    ? CancellationError() : EpicDownloadError.contentServer
            }
            api.manifestHandler = { _ in EpicManifestLocation(
                url: URL(string: "https://fixture.invalid/" + first)!, chunkBaseURLs: [],
                sha1: EpicDownloader.sha1(data), alternateURLs: [URL(string: "https://backup.invalid/backup")!]) }
            let model = EpicLibraryModel.shared
            model.enqueue(game("one"))
            try await until("manifest fallback result") {
                model.installed("one") != nil || ["failed", "paused"].contains(model.download("one")?.state ?? "")
            }
            if scenario == "manifest-fallback" {
                try check(model.installed("one") != nil, "unavailable first CDN falls back to authenticated alternate")
                try check(EpicDownloader.manifestFetches == [first, "backup"], "each CDN attempted once in API order")
                try check(library.saves == 1 && EpicDownloader.installApps == ["one"], "verified alternate manifest installs and registers once")
            } else {
                try check(EpicDownloader.manifestFetches == [first], "cancellation and digest failure never retry")
                try check(library.saves == 0 && EpicDownloader.installApps.isEmpty, "cancelled or untrusted manifest never installs")
            }
        case "download-diagnostics":
            account.accept(a)
            let model = EpicLibraryModel.shared
            let apiFailure = "private-api-app", fetchFailure = "private-fetch-app", parseFailure = "private-parse-app"
            api.manifestHandler = { g in
                if g.appName == apiFailure { throw EpicError.invalidResponse }
                let data = try EpicDownloader.manifestOverrides[g.appName] ?? EpicDownloader.fixture(g.appName)
                return EpicManifestLocation(url: URL(string: "https://fixture.invalid/" + g.appName)!,
                                            chunkBaseURLs: [], sha1: EpicDownloader.sha1(data))
            }
            EpicDownloader.fetchErrors[fetchFailure] = NSError(domain: "https://private.invalid/?signed-secret=TOKEN", code: 5,
                userInfo: [NSLocalizedDescriptionKey: "account-A secret URL TOKEN"])
            EpicDownloader.manifestOverrides[parseFailure] = Data("invalid owned manifest".utf8)
            for name in [apiFailure, fetchFailure, parseFailure] {
                model.enqueue(game(name)); try await until("diagnostic failure") { model.download(name)?.state == "failed" }
            }
            try check(LogStore.shared.messages == [
                "[epic-download] stage=manifest-address category=invalid-response",
                "[epic-download] stage=manifest-fetch category=other",
                "[epic-download] stage=manifest-parse category=download"], "only stable stages/categories enter diagnostics")
            try check(EpicDownloader.installApps.isEmpty && library.saves == 0 && model.installations.isEmpty,
                      "all preparation failures avoid installer and registration")
            try check(model.downloads.allSatisfy { !($0.error ?? "").contains("TOKEN") && !($0.error ?? "").contains("private.invalid") },
                      "underlying error details never enter persisted queue errors")
        case "ownership-launch", "ownership-failure", "ownership-exchange-failure", "ownership-before-start-logout", "ownership-after-claim-logout", "ownership-preparation-ended", "ownership-switch-during-token", "ownership-switch-during-exchange", "ownership-cancel-during-token":
            account.accept(a)
            let installed = try seedInstall("owned", keyA, ownership: true)
            try store.write(JSONEncoder().encode([installed]), name: "installations.json")
            let model = EpicLibraryModel.shared
            let tokenDirectory = LibraryModel.documents.appendingPathComponent("wine/drive_c/" + EpicOwnershipTokenFile.directoryName)
            func files() -> [String] { (try? FileManager.default.contentsOfDirectory(atPath: tokenDirectory.path)) ?? [] }
            let preparation = try GameSessionWork.shared.beginLaunchPreparation()
            if ["ownership-switch-during-token", "ownership-switch-during-exchange", "ownership-cancel-during-token"].contains(scenario) {
                let gate = Gate()
                if scenario == "ownership-switch-during-exchange" { api.exchangeHandler = { await gate.stop(); return "synthetic_exchange_123456789" } }
                else { api.ownershipHandler = { await gate.stop(); return Data("synthetic-owned-token".utf8) } }
                let task = Task { try await model.launch(library.epicEntry(installed)) }
                try await until("ownership request waiting") { await gate.isEntered() }
                if scenario == "ownership-switch-during-exchange" { try check(files().count == 1, "exchange pending keeps one private token file") }
                if scenario == "ownership-cancel-during-token" { task.cancel() } else { account.switchTo(b) }
                await gate.open()
                do { _ = try await task.value; throw Failure(message: "stale ownership preparation accepted") }
                catch EpicError.accountChanged { checks += 1 }
                try check(files().isEmpty && !LogStore.shared.messages.joined().contains("synthetic-owned-token"), "cancelled/stale response leaves no token or secret log")
                GameSessionWork.shared.completePreparation(preparation)
                break
            }
            if scenario == "ownership-failure" { api.ownershipHandler = { throw EpicError.http(403) } }
            if scenario == "ownership-exchange-failure" { api.exchangeHandler = { throw EpicError.http(403) } }
            if scenario == "ownership-failure" || scenario == "ownership-exchange-failure" {
                do { _ = try await model.launch(library.epicEntry(installed)); throw Failure(message: "failed ownership auth started game") }
                catch EpicError.http(403) { checks += 1 }
                try check(files().isEmpty, "failed API/exchange leaves no token file")
                try check(api.exchangeCalls == (scenario == "ownership-failure" ? 0 : 1), "ownership failure never consumes exchange code")
                GameSessionWork.shared.completePreparation(preparation)
                break
            }
            let launch = try await model.launch(library.epicEntry(installed))
            try check(api.ownershipCalls == ["owned"], "ownership launch calls authenticated token API for selected game")
            let ovt = launch.arguments.first { $0.hasPrefix("-epicovt=") }
            try check(ovt != nil, "ownership launch includes token file argument")
            try check(!launch.arguments.joined().contains("synthetic-owned-token"), "token bytes never appear in launch arguments")
            try check(files().count == 1, "prepared launch retains exactly one credential file")
            let tokenPath = tokenDirectory.appendingPathComponent(files()[0])
            try check(try Data(contentsOf: tokenPath) == Data("synthetic-owned-token".utf8), "game token file preserves opaque response")
            if scenario == "ownership-before-start-logout" {
                account.signOut()
                try check(files().isEmpty, "logout synchronously removes unclaimed token")
                do { try launch.claimForRuntime(); throw Failure(message: "logout did not revoke pending native start") }
                catch is EpicLaunchArguments.Invalid { checks += 1 }
            } else if scenario == "ownership-after-claim-logout" {
                library.current = UUID(); try launch.claimForRuntime()
                account.signOut(); await settle()
                GameSessionWork.shared.completePreparation(preparation)
                try check(files().count == 1, "logout and completed preparation preserve active runtime token")
                library.current = nil; await settle()
                try check(files().isEmpty, "real coordinator idle after runtime removes credential")
            } else if scenario == "ownership-preparation-ended" {
                GameSessionWork.shared.completePreparation(preparation); await settle()
                try check(files().isEmpty, "aborted launch preparation clears ownership token")
                do { try launch.claimForRuntime(); throw Failure(message: "ended launch preparation claimed runtime") }
                catch is EpicLaunchArguments.Invalid { checks += 1 }
            } else {
                launch.cancelBeforeStart()
                try check(files().isEmpty, "explicit launch preflight failure clears token")
            }
            GameSessionWork.shared.completePreparation(preparation)
        case "launch":
            account.accept(a)
            let installed = try seedInstall("normal", keyA), foreign = try seedInstall("foreign", keyB)
            let missing = try seedInstall("missing", keyA, missing: true)
            let restricted = try seedInstall("restricted", keyA, requirement: "external launcher")
            let batch = try seedInstall("batch", keyA, suffix: ".cmd")
            try store.write(JSONEncoder().encode([installed, foreign, missing, restricted, batch]), name: "installations.json")
            let model = EpicLibraryModel.shared
            let entry = library.epicEntry(installed)
            let result = try await model.launch(entry)
            try check(result.arguments.contains("--windowed") && result.arguments.contains("-AUTH_PASSWORD=" + result.secret), "valid launch includes user and one-use auth arguments")
            try check(result.arguments.contains("-epicapp=normal") && result.arguments.contains("-epicuserid=" + a), "valid launch uses selected account/app")
            try check(api.exchangeCalls == 1, "valid launch requests one exchange")
            var altered = entry; altered.relativePath = foreign.relativePath
            var wrongAccount = entry; wrongAccount.epicAccountKey = keyB
            for invalid in [altered, wrongAccount, library.epicEntry(foreign), library.epicEntry(missing)] {
                do { _ = try await model.launch(invalid); throw Failure(message: "mismatched/missing install launch accepted") }
                catch EpicError.notSignedIn { checks += 1 }
            }
            try check(api.exchangeCalls == 1, "invalid account/path avoids exchange")
            var invalidQuote = entry; invalidQuote.arguments = "\"open"
            var services = entry; services.startServices = true
            for invalid in [invalidQuote, services, library.epicEntry(restricted), library.epicEntry(batch)] {
                do { _ = try await model.launch(invalid); throw Failure(message: "unsupported launch accepted") }
                catch is EpicLaunchArguments.Invalid { checks += 1 }
            }
            try check(api.exchangeCalls == 1, "invalid command/services/requirements avoid exchange")
            account.signOut(); await settle()
            do { _ = try await model.launch(entry); throw Failure(message: "logout launch accepted") }
            catch EpicError.notSignedIn { checks += 1 }
            try check(api.exchangeCalls == 1, "logout avoids exchange")
        case "launch-stale":
            account.accept(a)
            let installed = try seedInstall("one", keyA)
            try store.write(JSONEncoder().encode([installed]), name: "installations.json")
            let model = EpicLibraryModel.shared, gate = Gate()
            api.exchangeHandler = { await gate.stop(); return "synthetic_exchange_code_123456789" }
            let pending = Task { try await model.launch(library.epicEntry(installed)) }
            try await until("launch exchange") { await gate.isEntered() }
            account.switchTo(b); await settle(); await gate.open()
            do { _ = try await pending.value; throw Failure(message: "stale exchange launch accepted") }
            catch EpicError.accountChanged { checks += 1 }
            try check(api.exchangeCalls == 1 && model.accountKey == keyB, "stale exchange not replayed for B")
        case "reload-queue":
            account.accept(a)
            let records = [EpicDownload(game: game("one"), accountKey: keyA, state: "downloading", received: 7, total: 100),
                           EpicDownload(game: game("two"), accountKey: keyB, state: "queued")]
            try store.write(JSONEncoder().encode(records), name: "downloads.json")
            let model = EpicLibraryModel.shared; await settle()
            try check(model.downloads.count == 2 && model.downloads.allSatisfy { $0.state == "paused" }, "restart never silently resumes persisted queue")
            try check(model.visibleDownloads.count == 1 && model.download("one")?.received == 7, "restart filters account and retains progress")
            try check(api.manifestCalls.isEmpty && EpicDownloader.installApps.isEmpty, "restart no implicit network/install")
        case "corrupt-record":
            account.accept(a); let damaged = Data("{broken".utf8)
            try store.write(damaged, name: "downloads.json")
            let model = EpicLibraryModel.shared
            try check(model.error != nil, "invalid persisted queue reports error")
            model.enqueue(game("one")); await settle()
            try check(model.downloads.isEmpty && api.manifestCalls.isEmpty, "read-only failure blocks new writes/downloads")
            try check(try store.read(name: "downloads.json") == damaged, "corrupt user records preserved")
        case "session":
            account.accept(a)
            let g1 = Gate(), g2 = Gate(), g3 = Gate()
            EpicDownloader.plans = [InstallPlan(gate: g1), InstallPlan(gate: g2), InstallPlan(gate: g3)]
            let model = EpicLibraryModel.shared
            model.enqueue(game("one")); try await until("first session install") { await g1.isEntered() }
            model.enqueue(game("two")); library.current = UUID(); await settle()
            try check(model.download("one")?.state == "waiting" && model.download("two")?.state == "waiting", "real current observer waits active and queued jobs")
            model.enqueue(game("three")); await settle()
            try check(model.download("three")?.state == "waiting", "new download waits while game is active")
            model.pause("two"); await g1.open(); await settle()
            try check(EpicDownloader.installApps == ["one"], "no install continues during session")
            library.current = nil
            try await until("resume after exit") { await g2.isEntered() }
            try check(model.download("two")?.state == "paused", "manual pause is not resumed by game exit")
            try check(model.download("one")?.state == "downloading" && model.download("three")?.state == "queued", "exit resumes only waiting jobs")
            await g2.open(); try await until("waiting third job") { await g3.isEntered() }
            try check(EpicDownloader.installApps == ["one", "one", "three"], "manual paused second job skipped")
            await g3.open(); try await until("session queue settled") { model.download("three") == nil }
            try check(model.downloads.count == 1 && model.download("two")?.state == "paused", "manual pause retained after draining resumed jobs")
        case "session-already-active":
            account.accept(a); library.current = UUID()
            let model = EpicLibraryModel.shared
            model.enqueue(game("one"))
            try check(model.download("one")?.state == "waiting", "initial session blocks immediate enqueue before observer delivery")
            await settle()
            try check(api.manifestCalls.isEmpty && EpicDownloader.installApps.isEmpty, "initial active game makes no download request")
            library.current = nil
            try await until("initial session exited") { model.installed("one") != nil }
            try check(model.downloads.isEmpty && library.entries.count == 1, "initial waiting job resumes after exit")
        case "installer-ancestor-boundary":
            account.accept(a)
            EpicDownloader.plans = [InstallPlan(validateRealStore: true), InstallPlan(validateRealStore: true)]
            let model = EpicLibraryModel.shared
            let wine = LibraryModel.documents.appendingPathComponent("wine", isDirectory: true)
            let drive = wine.appendingPathComponent("drive_c", isDirectory: true)
            let outside = LibraryModel.documents.deletingLastPathComponent().appendingPathComponent("outside", isDirectory: true)
            try fm.createDirectory(at: outside, withIntermediateDirectories: true)
            for (index, link) in [wine, drive].enumerated() {
                // These are this process's empty synthetic directories only.
                try fm.removeItem(at: link)
                try fm.createSymbolicLink(at: link, withDestinationURL: outside)
                let name = index == 0 ? "linked-wine" : "linked-drive"
                model.enqueue(game(name))
                try await until("linked installer rejected") { model.download(name)?.state == "failed" }
                try check(EpicDownloader.destinations.count == index + 1, "linked boundary called installer once")
                let destination = EpicDownloader.destinations[index]
                let expected = LibraryModel.documents.appendingPathComponent("wine/drive_c", isDirectory: true)
                    .appendingPathComponent("Program Files/Epic Games/" + keyA.prefix(12) + "-" + EpicLibraryModel.key(name).prefix(20), isDirectory: true)
                try check(try fm.contentsOfDirectory(atPath: outside.path).isEmpty, "linked outside remains completely untouched")
                print("PASS fixture outside untouched: " + name)
                try check(destination.path == expected.path, "model passes raw ancestor chain to installer nofollow guard")
                try check(EpicDownloader.realStoreRejections == index + 1, "real install-store nofollow rejects linked ancestor")
                try check(model.installations.isEmpty && library.entries.isEmpty, "linked install never registers")
                try check(try store.read(name: "installations.json") == nil, "linked install never writes receipts")
                // Removing a fixture symlink does not remove its target.
                try fm.removeItem(at: link)
                try fm.createDirectory(at: drive, withIntermediateDirectories: true)
            }
            try check(model.downloads.count == 2 && model.downloads.allSatisfy { $0.state == "failed" }, "both linked ancestor failures remain explicit")
        default: throw Failure(message: "unknown scenario")
        }
        try check(try Data(contentsOf: sentinel) == Data("KEEP STEAM".utf8), "Steam sentinel unchanged")
        print("PASS \(scenario): \(checks) production-model assertions")
    }
}
'''

SCENARIOS = ("transfer-speed", "cdn-measurements", "cancel-persist-failure", "cancel-identity-persist-failure", "cancel-updated-manifest", "cancel-empty", "cancel-paused", "cancel-retry", "cancel-reload", "cancel-completed", "cancel-drain", "cancel-switch", "signed-out", "logout-refresh", "session-refresh", "session-refresh-identity", "save-session", "save-drain", "switch-refresh", "queue-resume",
             "switch-download", "registration", "missing-executable", "manifest-builder-name",
             "manifest-hash", "manifest-missing-hash", "manifest-fallback", "manifest-fallback-cancel", "manifest-fallback-hash", "download-diagnostics", "ownership-launch", "ownership-failure", "ownership-exchange-failure", "ownership-before-start-logout", "ownership-after-claim-logout", "ownership-preparation-ended", "ownership-switch-during-token", "ownership-switch-during-exchange", "ownership-cancel-during-token", "launch", "launch-stale", "reload-queue", "corrupt-record", "session",
             "session-already-active", "installer-ancestor-boundary")

MUTATIONS = (
    ("stale-refresh-cleanup", "session-refresh-identity",
     "if refreshIdentity == identity { refreshing = false; refreshTask = nil; refreshIdentity = nil }",
     "if generation == epoch { refreshing = false; refreshTask = nil; refreshIdentity = nil }"),
    ("stale-refresh", "logout-refresh",
     "guard !Task.isCancelled, !sessionActive, refreshIdentity == identity,\n                      generation == epoch, account.sessionGeneration == accountEpoch else { return }",
     "guard true else { return }"),
    ("launch-path", "launch",
     "entry.relativePath == installed.relativePath,", "true,"),
    ("registered-executable", "missing-executable",
     "_ = try LibraryModel.executable(installed.relativePath)", "/* executable validation removed */"),
)


def executable_validation() -> str:
    """Use the real surrounding-library executable guard, not a mirrored one."""
    source = (ROOT / "app/Madeira/Library.swift").read_text()
    start = source.index("    static let programExtensions:")
    # Epic uses the String overload. The neighboring external-library overload
    # needs unrelated security-scoped leases and is not part of this boundary.
    end = source.index("    static func executable(for entry:", start)
    return source[start:end]


def main() -> int:
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--report-dir", type=Path, default=OUT,
                        help="isolated host evidence directory")
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--old-install-boundary", action="store_true",
                        help="Run the ancestor fixture against an isolated pre-fix model snapshot; expected runtime RED")
    options = parser.parse_args()
    OUT = options.report_dir
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"production": {},
              "boundary": "Synthetic account/API/downloader/library, real Combine/queue/cache/records/manifest/launch/install-store",
              "not_executed": ["UIKit background task expiry", "device Epic login/download/launch", "Steam runtime regression"],
              "optimized": options.optimize, "cases": [], "mutations": [], "apfs_fixtures": []}
    suffix = "red-old-install-boundary" if options.old_install_boundary else ("optimized" if options.optimize else "debug")
    result_code = 1
    scratch = None
    apfs_fixtures = []
    try:
        with tempfile.TemporaryDirectory(prefix="scratch-", dir=OUT) as directory, ExitStack() as fixtures:
            scratch = Path(directory)
            snapshots = []
            for source in SOURCES:
                content = source.read_bytes()
                snapshot = scratch / source.name
                snapshot.write_bytes(content)
                snapshots.append(snapshot)
                report["production"][str(source.relative_to(ROOT))] = hashlib.sha256(content).hexdigest()
            if options.old_install_boundary:
                model = scratch / "EpicLibrary.swift"
                source = model.read_text()
                before = ('let destination = LibraryModel.documents.appendingPathComponent("wine/drive_c", isDirectory: true)\n'
                          '                    .appendingPathComponent(relative, isDirectory: true)')
                if source.count(before) != 1:
                    raise RuntimeError("Old installer boundary anchor moved")
                model.write_text(source.replace(before, 'let destination = LibraryModel.drive.appendingPathComponent(relative, isDirectory: true)', 1))
                report["old_model_variant_sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            doubles, harness, binary = (scratch / name for name in ("Doubles.swift", "Runner.swift", "epic-library-check"))
            validation = executable_validation()
            report["real_library_executable_guard_sha256"] = hashlib.sha256(validation.encode()).hexdigest()
            binary_fixture = runpy.run_path(str(ROOT / "tests/host/check-epic-download.py"))["binary_manifest"]()
            doubles.write_text(DOUBLES.replace("__EXECUTABLE_VALIDATION__", validation)
                               .replace("__OWNED_BINARY_MANIFEST__", base64.b64encode(binary_fixture).decode()))
            harness.write_text(HARNESS)
            command = ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
                       "-j", "1", "-parse-as-library", "-module-cache-path", str(scratch / "module-cache"),
                       *map(str, snapshots), str(doubles), str(harness), "-o", str(binary), "-lz"]
            if options.optimize:
                command.insert(2, "-O")
            compiled = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
            (OUT / f"compile-{suffix}.txt").write_text(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                print(compiled.stdout + compiled.stderr, end="")
                report["compile"] = "failed"
                return compiled.returncode
            report["compile"] = "passed"
            environment = dict(os.environ, SWIFT_BACKTRACE="enable=no")
            selected = ["installer-ancestor-boundary"] if options.old_install_boundary else ([options.scenario] if options.scenario else SCENARIOS)
            for scenario in selected:
                apfs = tempfile.TemporaryDirectory(prefix="madeira-epic-container-model-", dir=Path.home())
                fixture = Path(fixtures.enter_context(apfs))
                apfs_fixtures.append(fixture)
                documents = fixture / "Documents"
                documents.mkdir(parents=True)
                try:
                    result = subprocess.run([str(binary), scenario, str(documents)], text=True, capture_output=True,
                                            timeout=15, env=environment, check=False)
                finally:
                    apfs.cleanup()
                output = result.stdout + result.stderr
                (OUT / f"{scenario}-{suffix}.txt").write_text(output)
                counts = re.findall(r"PASS [a-z-]+: (\d+) production-model assertions", result.stdout)
                report["cases"].append({"scenario": scenario, "exit": result.returncode,
                                        "assertions": sum(map(int, counts)), "output": output})
                print(output, end="")
            result_code = 0 if all(case["exit"] == 0 for case in report["cases"]) else 1
            report["assertions"] = sum(case["assertions"] for case in report["cases"])
            if options.mutations and result_code == 0:
                source = (scratch / "EpicLibrary.swift").read_text()
                for label, scenario, before, after in MUTATIONS:
                    if source.count(before) != 1:
                        raise RuntimeError(f"Mutation anchor moved: {label}")
                    folder = scratch / ("mutation-" + label)
                    folder.mkdir()
                    mutated = folder / "EpicLibrary.swift"
                    mutated.write_text(source.replace(before, after, 1))
                    mutation_sources = [mutated if p.name == mutated.name else p for p in snapshots]
                    executable = folder / "check"
                    mutation_command = ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
                                        "-j", "1", "-parse-as-library", "-module-cache-path", str(scratch / "module-cache"),
                                        *map(str, mutation_sources), str(doubles), str(harness), "-o", str(executable), "-lz"]
                    if options.optimize:
                        mutation_command.insert(2, "-O")
                    compiled = subprocess.run(mutation_command, text=True, capture_output=True, timeout=120, check=False)
                    (OUT / f"mutation-{label}-compile-{suffix}.txt").write_text(compiled.stdout + compiled.stderr)
                    if compiled.returncode:
                        result_code = 1
                        report["mutations"].append({"label": label, "compiled": False, "rejected_at_runtime": False})
                        print(f"FAIL mutation {label}: did not compile")
                        continue
                    apfs = tempfile.TemporaryDirectory(prefix="madeira-epic-container-model-mutation-", dir=Path.home())
                    fixture = Path(fixtures.enter_context(apfs))
                    apfs_fixtures.append(fixture)
                    documents = fixture / "Documents"
                    documents.mkdir()
                    try:
                        result = subprocess.run([str(executable), scenario, str(documents)], text=True, capture_output=True,
                                                timeout=15, env=environment, check=False)
                    finally:
                        apfs.cleanup()
                    output = result.stdout + result.stderr
                    rejected = result.returncode != 0 and "FAIL:" in result.stdout
                    report["mutations"].append({"label": label, "compiled": True, "rejected_at_runtime": rejected, "output": output})
                    (OUT / f"mutation-{label}-run-{suffix}.txt").write_text(output)
                    if not rejected:
                        result_code = 1
                    print(f"{'PASS' if rejected else 'FAIL'} mutation rejected at runtime: {label}")
            return result_code
    except subprocess.TimeoutExpired as error:
        result_code = 1
        report["timeout_seconds"] = error.timeout
        print(f"FAIL: bounded command timed out after {error.timeout}s")
        return 1
    except (OSError, RuntimeError) as error:
        result_code = 1
        report["harness_error"] = str(error)
        print(f"FAIL: {error}")
        return 1
    finally:
        report["scratch_removed"] = scratch is not None and not scratch.exists()
        report["apfs_fixtures"] = [{"path": str(path), "removed": not path.exists()} for path in apfs_fixtures]
        report["result"] = result_code
        (OUT / f"report-{suffix}.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
