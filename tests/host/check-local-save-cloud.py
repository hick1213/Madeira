#!/usr/bin/env python3
"""Run production local-save files/model against exact owned APFS fixtures.

Foundation bookmarks, NSFileCoordinator, CryptoKit, FD reads/writes, immutable
versions, snapshots, restores, and GameSessionWork are real production code.
Only the surrounding library/account/native-session boundaries are doubles.
The selected provider directory is a local simulation, not an iCloud test.
"""

from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/LocalSaveCloud.swift"
SNAPSHOT = ROOT / "app/Madeira/LocalSaveSnapshot.swift"
SESSION = ROOT / "app/Madeira/GameSessionWork.swift"
OUT = ROOT / "app/Build/Products/IPAInspection/external-game-library-build17/local-save-cloud-tests"
DOMAIN_PREFIX = "Madeira.LocalSaveCloud.Tests."

BOUNDARIES = r'''
import Foundation
import Combine
struct LibraryEntry: Sendable {
    var id = UUID()
    var title: String
    var relativePath: String
    var externalLibraryID: UUID?
    var steamAppID: Int?
    var epicAppName: String?
    var desktop: Bool?
}
enum LibraryError: Error { case message(String) }
@MainActor final class LibraryModel {
    static let shared = LibraryModel()
    static var documents: URL { URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("Documents") }
    var current: UUID?
    var paused = false
    func pauseOptionalWork(_ value: Bool) { paused = value }
}
@MainActor final class SteamOwnedLibrary {
    static let shared = SteamOwnedLibrary()
    var hasInFlightDownload = false
    var hasActiveCloudSaveOperation = false
    var paused = false
    func sessionChanged(active: Bool) { paused = active }
}
@MainActor final class EpicLibraryModel {
    static let shared = EpicLibraryModel()
    var hasInFlightDownload = false
    var paused = false
    func sessionChanged(active: Bool) { paused = active }
}
@MainActor var nativeWine: Int32 = 0
@MainActor var nativeServer: Int32 = 0
@MainActor func wine_process_is_running() -> Int32 { nativeWine }
@MainActor func wineserver_is_running() -> Int32 { nativeServer }
'''

MAIN = r'''
import Foundation
import CryptoKit
import Darwin

enum Failed: Error { case assertion(String) }
// A real NSFilePresenter delays a real coordinator accessor. This never
// substitutes a model method, file IO, or the game-work state machine.
final class FolderGate: NSObject, NSFilePresenter, @unchecked Sendable {
    let presentedItemURL: URL?
    let presentedItemOperationQueue: OperationQueue
    private let lock = NSLock()
    private let releaseSignal = DispatchSemaphore(value: 0)
    private var didEnter = false
    private var released = false
    var entered: Bool { lock.lock(); defer { lock.unlock() }; return didEnter }
    init(_ url: URL) {
        presentedItemURL = url
        presentedItemOperationQueue = OperationQueue()
        presentedItemOperationQueue.maxConcurrentOperationCount = 1
        super.init()
    }
    func open() { lock.lock(); released = true; lock.unlock(); releaseSignal.signal() }
    func relinquishPresentedItem(toReader reader: @escaping @Sendable ((@Sendable () -> Void)?) -> Void) {
        lock.lock(); didEnter = true; let shouldWait = !released; lock.unlock()
        if shouldWait { releaseSignal.wait() }
        reader(nil)
    }
}

@main struct Main {
    @MainActor static func main() async {
        do { try await run() }
        catch { print("FAIL \(error)"); exit(1) }
    }
    @MainActor static func run() async throws {
        guard CommandLine.arguments.count == 3 else { throw Failed.assertion("owned fixture/domain arguments") }
        let fixture = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let docs = LibraryModel.documents
        let provider = fixture.appendingPathComponent("Provider")
        let gameFolder = docs.appendingPathComponent("wine/drive_c/users/player/Saved Games/Fixture")
        let fm = FileManager.default
        try fm.createDirectory(at: gameFolder, withIntermediateDirectories: true)
        try fm.createDirectory(at: provider, withIntermediateDirectories: true)
        let suiteName = CommandLine.arguments[2]
        guard let defaults = UserDefaults(suiteName: suiteName) else { throw Failed.assertion("isolated defaults") }
        defer { defaults.removePersistentDomain(forName: suiteName) }
        var checks: [String] = []
        func check(_ value: Bool, _ name: String) throws {
            guard value else { throw Failed.assertion(name) }; checks.append(name)
        }
        func reject(_ name: String, _ body: () throws -> Void) throws {
            var rejected = false
            do { try body() } catch { rejected = true }
            try check(rejected, name)
        }
        var actualFS = statfs()
        guard statfs(fixture.path, &actualFS) == 0 else { throw Failed.assertion("fixture statfs") }
        let filesystem = withUnsafePointer(to: &actualFS.f_fstypename) {
            $0.withMemoryRebound(to: CChar.self, capacity: 16) { String(cString: $0) }
        }
        try check(filesystem == "apfs", "actual owned fixture uses APFS for native link/swap contracts")
        func bytes(_ text: String) -> Data { Data(text.utf8) }
        func file(_ path: String, _ text: String) -> LocalSaveSnapshot.File {
            let value = bytes(text)
            return LocalSaveSnapshot.File(path: path, data: value, sha256: LocalSaveCloudFiles.hash(value))
        }
        func archive(_ files: [LocalSaveSnapshot.File], game: UUID, id: UUID = UUID(), title: String = "Fixture", created: Date = Date(timeIntervalSince1970: 1_700_000_000)) -> LocalSaveSnapshot.Archive {
            LocalSaveSnapshot.Archive(version: 1, id: id, gameID: game, title: title, created: created, files: files)
        }
        func record(_ value: LocalSaveSnapshot.Archive, raw: Data? = nil) throws -> LocalSaveCloudFiles.Record {
            let data = try raw ?? LocalSaveSnapshot.encode(value)
            return LocalSaveCloudFiles.Record(id: value.id, gameID: value.gameID, title: value.title, created: value.created,
                files: value.files.count, bytes: data.count, fingerprint: value.fingerprint, archiveHash: LocalSaveCloudFiles.hash(data))
        }
        func metadata(_ original: LocalSaveCloudFiles.Record, title: String? = nil, files: Int? = nil,
                      size: Int? = nil, fingerprint: String? = nil, hash: String? = nil) -> LocalSaveCloudFiles.Record {
            LocalSaveCloudFiles.Record(id: original.id, gameID: original.gameID, title: title ?? original.title,
                created: original.created, files: files ?? original.files, bytes: size ?? original.bytes,
                fingerprint: fingerprint ?? original.fingerprint, archiveHash: hash ?? original.archiveHash)
        }
        func versions(_ folder: URL, _ game: UUID) throws -> String {
            LocalSaveCloudFiles.versionFingerprint(try LocalSaveCloudFiles.records(in: folder), gameID: game)
        }
        func settle(_ model: LocalSaveCloudModel, pending: Bool = false) async throws {
            for _ in 0..<500 {
                if pending ? model.pending != nil || !model.busy : !model.busy { return }
                try await Task.sleep(for: .milliseconds(10))
            }
            throw Failed.assertion("model operation reached bounded completion")
        }
        func gameContents() throws -> [String: Data] {
            var result: [String: Data] = [:]
            for url in try fm.contentsOfDirectory(at: gameFolder, includingPropertiesForKeys: nil) {
                result[url.lastPathComponent] = try Data(contentsOf: url)
            }
            return result
        }
        func providerContents() throws -> [String: Data] {
            let folder = provider.appendingPathComponent(LocalSaveCloudFiles.directoryName)
            guard fm.fileExists(atPath: folder.path) else { return [:] }
            var result: [String: Data] = [:]
            for url in try fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil) {
                result[url.lastPathComponent] = try Data(contentsOf: url)
            }
            return result
        }
        func backupCount() throws -> Int {
            let folder = docs.appendingPathComponent("iCloud Save Backups")
            return fm.fileExists(atPath: folder.path) ? try fm.contentsOfDirectory(atPath: folder.path).count : 0
        }

        let cloudGame = UUID()
        let fixedID = UUID(uuidString: "AAAAAAAA-1111-4222-8333-BBBBBBBBBBBB")!
        let first = archive([file("save.dat", "FIRST")], game: cloudGame, id: fixedID)
        let firstRecord = try record(first)
        try check(LocalSaveCloudFiles.valid(firstRecord), "valid bounded metadata accepted")
        for (name, value) in [
            ("empty title", metadata(firstRecord, title: "")),
            ("control title", metadata(firstRecord, title: "bad\nname")),
            ("oversize title", metadata(firstRecord, title: String(repeating: "x", count: 513))),
            ("negative file count", metadata(firstRecord, files: -1)),
            ("zero payload size", metadata(firstRecord, size: 0)),
            ("oversize payload size", metadata(firstRecord, size: (96 << 20) + 1)),
            ("invalid fingerprint", metadata(firstRecord, fingerprint: "invalid")),
            ("uppercase payload hash", metadata(firstRecord, hash: String(repeating: "A", count: 64)))
        ] { try check(!LocalSaveCloudFiles.valid(value), "metadata rejects \(name)") }
        let firstURL = try LocalSaveCloudFiles.upload(first, to: provider, expectedVersions: try versions(provider, cloudGame))
        let originalBytes = try Data(contentsOf: firstURL)
        try check(try LocalSaveCloudFiles.load(firstRecord, from: provider).fingerprint == first.fingerprint, "actual new payload and metadata load together")
        let second = archive([file("save.dat", "SECOND")], game: cloudGame, created: Date(timeIntervalSince1970: 1_700_000_001))
        _ = try LocalSaveCloudFiles.upload(second, to: provider, expectedVersions: try versions(provider, cloudGame))
        try check(try LocalSaveCloudFiles.records(in: provider).count == 2 && Data(contentsOf: firstURL) == originalBytes, "new immutable version keeps previous payload bytes")
        try reject("O_EXCL refuses existing version ID") { _ = try LocalSaveCloudFiles.upload(first, to: provider, expectedVersions: versions(provider, cloudGame)) }
        try check(try Data(contentsOf: firstURL) == originalBytes, "exclusive failure leaves previous payload unchanged")

        // A differently cased UUID is the same decoded UUID and same archive
        // content, but different bytes: only the payload hash catches it.
        let aliasBytes = bytes(String(decoding: originalBytes, as: UTF8.self).replacingOccurrences(of: fixedID.uuidString, with: fixedID.uuidString.lowercased()))
        try check(aliasBytes.count == originalBytes.count && aliasBytes != originalBytes, "semantically identical payload hash fixture has equal size and distinct bytes")
        try aliasBytes.write(to: firstURL)
        try reject("payload hash rejects semantically identical altered bytes") { _ = try LocalSaveCloudFiles.load(firstRecord, from: provider) }
        try originalBytes.write(to: firstURL)
        let metadataURL = provider.appendingPathComponent(LocalSaveCloudFiles.directoryName).appendingPathComponent(fixedID.uuidString + ".json")
        let originalMetadata = try Data(contentsOf: metadataURL)
        var metadataObject = try JSONSerialization.jsonObject(with: originalMetadata) as! [String: Any]
        metadataObject["id"] = "not-a-uuid"
        try JSONSerialization.data(withJSONObject: metadataObject).write(to: metadataURL)
        try reject("metadata malformed UUID rejects enumeration") { _ = try LocalSaveCloudFiles.records(in: provider) }
        metadataObject["id"] = UUID().uuidString
        try JSONSerialization.data(withJSONObject: metadataObject).write(to: metadataURL)
        try reject("metadata filename and UUID must agree") { _ = try LocalSaveCloudFiles.records(in: provider) }
        try originalMetadata.write(to: metadataURL)

        let unsafe = archive([file("../escape.dat", "BAD")], game: cloudGame)
        let unsafeRaw = try JSONEncoder().encode(unsafe)
        let unsafeRecord = try record(unsafe, raw: unsafeRaw)
        let badFolder = fixture.appendingPathComponent("BadProvider")
        let badVersions = badFolder.appendingPathComponent(LocalSaveCloudFiles.directoryName)
        try fm.createDirectory(at: badVersions, withIntermediateDirectories: true)
        let badMeta = badVersions.appendingPathComponent(unsafe.id.uuidString + ".json")
        let badPayload = badVersions.appendingPathComponent(unsafe.id.uuidString + ".madeirasaves")
        try JSONEncoder().encode(unsafeRecord).write(to: badMeta); try unsafeRaw.write(to: badPayload)
        try reject("hash-valid traversal archive rejects before restore") { _ = try LocalSaveCloudFiles.load(unsafeRecord, from: badFolder) }
        let wrongArchive = archive([file("save.dat", "FIRST")], game: cloudGame, id: unsafe.id)
        let wrongRaw = try LocalSaveSnapshot.encode(wrongArchive)
        let mismatch = LocalSaveCloudFiles.Record(id: firstRecord.id, gameID: cloudGame, title: wrongArchive.title, created: wrongArchive.created,
            files: 1, bytes: wrongRaw.count, fingerprint: wrongArchive.fingerprint, archiveHash: LocalSaveCloudFiles.hash(wrongRaw))
        try wrongRaw.write(to: firstURL); try JSONEncoder().encode(mismatch).write(to: metadataURL)
        try reject("payload archive identity must match metadata") { _ = try LocalSaveCloudFiles.load(mismatch, from: provider) }
        try originalBytes.write(to: firstURL); try originalMetadata.write(to: metadataURL)
        try fm.removeItem(at: badPayload)
        try fm.createSymbolicLink(at: badPayload, withDestinationURL: firstURL)
        try reject("payload symlink rejects without following") { _ = try LocalSaveCloudFiles.load(unsafeRecord, from: badFolder) }
        try fm.removeItem(at: badPayload)
        try fm.linkItem(at: firstURL, to: badPayload)
        try reject("payload hardlink rejects shared inode") { _ = try LocalSaveCloudFiles.load(unsafeRecord, from: badFolder) }
        try fm.removeItem(at: badPayload)
        let linkedRoot = fixture.appendingPathComponent("LinkedProvider")
        try fm.createSymbolicLink(at: linkedRoot, withDestinationURL: provider)
        try reject("selected provider root symlink rejects") { _ = try LocalSaveCloudFiles.records(in: linkedRoot) }
        let rawNUL = URL(fileURLWithPath: provider.path + "\0tail", isDirectory: true)
        try check(rawNUL.path(percentEncoded: false).contains("\0"), "root NUL fixture preserves raw NUL")
        try reject("selected provider raw NUL rejects before open") { _ = try LocalSaveCloudFiles.records(in: rawNUL) }

        let program = "Games/Fixture/game.exe"
        try check(try LocalSaveDirectoryRules.relative(windowsPath: "C:\\users\\player\\Saved Games\\Fixture", program: program) == "wine/drive_c/users/player/Saved Games/Fixture", "dedicated user-game directory accepted")
        try check(try LocalSaveDirectoryRules.relative(windowsPath: "C:\\Games\\Fixture\\Saves", program: program) == "wine/drive_c/Games/Fixture/Saves", "game executable's own directory accepted")
        for (name, path, executable) in [
            ("Windows root", "C:\\", program),
            ("whole user", "C:\\users\\player", program),
            ("whole documents", "C:\\users\\player\\Documents", program),
            ("traversal", "C:\\Games\\Fixture\\..\\Other", program),
            ("Steam client", "C:\\Program Files (x86)\\Steam", "Program Files (x86)/Steam/steam.exe"),
            ("Epic client", "C:\\Program Files\\Epic Games\\Launcher", "Program Files/Epic Games/Launcher/epic.exe"),
            ("Steam account AppData", "C:\\users\\player\\AppData\\Local\\Steam", program)
        ] { try reject("directory rules reject \(name)") { _ = try LocalSaveDirectoryRules.relative(windowsPath: path, program: executable) } }

        let entry = LibraryEntry(title: "Local Fixture", relativePath: program)
        let model = LocalSaveCloudModel(defaults: defaults)
        try model.setFolder(provider)
        var externalGame = entry; externalGame.id = UUID(); externalGame.externalLibraryID = UUID()
        try model.configure(externalGame, windowsPath: "C:\\users\\player\\Saved Games\\Fixture")
        try check(model.configuration(externalGame.id)?.directory == "wine/drive_c/users/player/Saved Games/Fixture", "external games allow internal Windows-profile saves")
        try reject("external root-relative executable grants no internal installation permission") {
            try model.configure(externalGame, windowsPath: "C:\\Games\\Fixture\\Saves")
        }
        try reject("external mount save folder is not enabled") {
            try model.configure(externalGame, windowsPath: "C:\\madeira-external\\\(externalGame.externalLibraryID!.uuidString)\\Saves")
        }
        try model.configure(entry, windowsPath: "C:\\users\\player\\Saved Games\\Fixture")
        let reloaded = LocalSaveCloudModel(defaults: defaults)
        try check(reloaded.hasFolder && reloaded.configuration(entry.id)?.directory == "wine/drive_c/users/player/Saved Games/Fixture", "bookmark and explicit local configuration persist in isolated domain")
        var steam = entry; steam.steamAppID = 42
        var epic = entry; epic.epicAppName = "FixtureEpic"
        var desktop = entry; desktop.desktop = true
        for denied in [steam, epic, desktop] {
            try reject("non-local entry rejected \(denied.steamAppID ?? 0)-\(denied.epicAppName ?? "desktop")") { try model.configure(denied, windowsPath: "C:\\users\\player\\Saved Games\\Fixture") }
        }
        try bytes("LOCAL-A").write(to: gameFolder.appendingPathComponent("save.dat"))
        let countBefore = try LocalSaveCloudFiles.records(in: provider).count
        let cloudBeforePreview = try providerContents(), localBeforePreview = try gameContents()
        let backupsBeforePreview = try backupCount()
        model.prepareUpload(entry); try await settle(model, pending: true)
        guard case .upload? = model.pending else { throw Failed.assertion("upload preview produced") }
        try check(model.busy && GameSessionWork.shared.isSynchronizingSaves, "upload preview retains real save-operation ownership")
        try check(try LocalSaveCloudFiles.records(in: provider).count == countBefore && providerContents() == cloudBeforePreview && gameContents() == localBeforePreview && backupCount() == backupsBeforePreview, "upload preparation performs zero cloud version writes")
        model.cancel()
        try check(!model.busy && !GameSessionWork.shared.isSynchronizingSaves && model.pending == nil, "cancelling pending preview releases owned token without IO")
        try check(try LocalSaveCloudFiles.records(in: provider).count == countBefore && providerContents() == cloudBeforePreview && gameContents() == localBeforePreview && backupCount() == backupsBeforePreview, "cancelled preview leaves cloud versions unchanged")
        model.prepareUpload(entry); try await settle(model, pending: true); model.confirm(); try await settle(model)
        let uploaded = try LocalSaveCloudFiles.records(in: provider).first { $0.gameID == entry.id }!
        let configurationKey = "madeiraLocalSaveCloudConfigurations"
        let previousConfigurations = defaults.data(forKey: configurationKey)
        let oldInstallConfig = LocalSaveCloudModel.Configuration(game: externalGame.id, directory: "wine/drive_c/Games/Fixture/Saves", cloudGame: externalGame.id)
        defaults.set(try JSONEncoder().encode([oldInstallConfig]), forKey: configurationKey)
        let legacyExternal = LocalSaveCloudModel(defaults: defaults)
        let beforeLegacyCloud = try providerContents(), beforeLegacyLocal = try gameContents()
        let beforeLegacyBackups = try backupCount()
        legacyExternal.prepareUpload(externalGame)
        try check(!legacyExternal.busy && legacyExternal.pending == nil && !GameSessionWork.shared.isSynchronizingSaves, "external upload rejects persisted installation-save config before IO")
        legacyExternal.prepareDownload(uploaded, into: externalGame)
        try check(!legacyExternal.busy && legacyExternal.pending == nil && !GameSessionWork.shared.isSynchronizingSaves, "external download rejects persisted installation-save config before IO")
        try check(legacyExternal.configuration(externalGame.id)?.directory == oldInstallConfig.directory && providerContents() == beforeLegacyCloud && gameContents() == beforeLegacyLocal && backupCount() == beforeLegacyBackups, "external policy rejection preserves old config, local saves, cloud versions and backups")
        defaults.set(previousConfigurations, forKey: configurationKey)
        try check(try LocalSaveCloudFiles.load(uploaded, from: provider).files.first?.data == bytes("LOCAL-A"), "confirmed upload publishes actual captured local bytes")
        let uploadURL = provider.appendingPathComponent(LocalSaveCloudFiles.directoryName).appendingPathComponent(uploaded.id.uuidString + ".madeirasaves")
        let intactUpload = try Data(contentsOf: uploadURL)
        try bytes("CORRUPT").write(to: uploadURL)
        model.prepareUpload(entry); try await settle(model, pending: true)
        try check(!model.busy && model.pending == nil && model.message != "The current cloud version already has these saves.", "equal fingerprint still validates corrupt current payload")
        try intactUpload.write(to: uploadURL)

        try bytes("LOCAL-B").write(to: gameFolder.appendingPathComponent("save.dat"))
        model.prepareUpload(entry); try await settle(model, pending: true)
        let beforeLocalRace = try LocalSaveCloudFiles.records(in: provider).count
        let cloudBeforeLocalRace = try providerContents()
        try bytes("LOCAL-C").write(to: gameFolder.appendingPathComponent("save.dat"))
        model.confirm(); try await settle(model)
        try check(try LocalSaveCloudFiles.records(in: provider).count == beforeLocalRace && providerContents() == cloudBeforeLocalRace, "post-preview local change rejects upload before version writes")
        model.prepareUpload(entry); try await settle(model, pending: true)
        let external = archive([file("save.dat", "OTHER DEVICE")], game: entry.id)
        _ = try LocalSaveCloudFiles.upload(external, to: provider, expectedVersions: versions(provider, entry.id))
        let afterExternal = try LocalSaveCloudFiles.records(in: provider).count
        let cloudAfterExternal = try providerContents()
        model.confirm(); try await settle(model)
        try check(try LocalSaveCloudFiles.records(in: provider).count == afterExternal && providerContents() == cloudAfterExternal, "post-preview cloud version change rejects upload without overwriting")

        try bytes("LOCAL-ONLY").write(to: gameFolder.appendingPathComponent("extra.dat"))
        model.prepareDownload(uploaded, into: entry); try await settle(model, pending: true)
        guard case .download(_, _, _, _, let replaced)? = model.pending else { throw Failed.assertion("download preview produced") }
        try check(replaced == 1, "download preview counts actual differing existing save")
        let backupBefore = try backupCount()
        model.confirm(); try await settle(model)
        try check(try gameContents() == ["save.dat": bytes("LOCAL-A"), "extra.dat": bytes("LOCAL-ONLY")], "confirmed download merges cloud files and preserves local-only files")
        try check(try backupCount() == backupBefore + 1, "confirmed restore keeps an actual original-save archive")
        let backups = try fm.contentsOfDirectory(at: docs.appendingPathComponent("iCloud Save Backups"), includingPropertiesForKeys: nil)
        let restoredBackup = try LocalSaveSnapshot.decode(Data(contentsOf: backups.last!))
        try check(restoredBackup.files.contains { $0.path == "save.dat" && $0.data == bytes("LOCAL-C") }, "restore backup contains pre-replacement local bytes")

        model.prepareDownload(uploaded, into: entry); try await settle(model, pending: true)
        try bytes("CHANGED AFTER REVIEW").write(to: gameFolder.appendingPathComponent("save.dat"))
        let beforeReject = try gameContents(), backupsBeforeReject = try backupCount()
        model.confirm(); try await settle(model)
        try check(try gameContents() == beforeReject && backupCount() == backupsBeforeReject, "post-preview local change rejects download before backup or replacement")
        model.prepareDownload(uploaded, into: entry); try await settle(model, pending: true)
        try bytes("BROKEN AFTER REVIEW").write(to: uploadURL)
        model.confirm(); try await settle(model)
        try check(try gameContents() == beforeReject && backupCount() == backupsBeforeReject, "post-preview cloud corruption rejects download before local writes")
        try intactUpload.write(to: uploadURL)

        let sharpEntry = LibraryEntry(title: "Unicode saves", relativePath: program)
        let sharpDir = docs.appendingPathComponent("wine/drive_c/users/player/Saved Games/Unicode")
        try fm.createDirectory(at: sharpDir, withIntermediateDirectories: true)
        try bytes("OLD").write(to: sharpDir.appendingPathComponent("Straße.dat"))
        try model.configure(sharpEntry, windowsPath: "C:\\users\\player\\Saved Games\\Unicode")
        let sharp = archive([file("STRASSE.dat", "NEW")], game: sharpEntry.id)
        _ = try LocalSaveCloudFiles.upload(sharp, to: provider, expectedVersions: versions(provider, sharpEntry.id))
        let sharpRecord = try record(sharp)
        model.prepareDownload(sharpRecord, into: sharpEntry); try await settle(model, pending: true)
        guard case .download(_, _, _, _, let unicodeReplaced)? = model.pending else { throw Failed.assertion("Unicode download preview produced") }
        try check(unicodeReplaced == 1, "Unicode sharp-s and SS overwrite count uses production pathKey")
        model.cancel()

        let gate = FolderGate(provider)
        let cloudBeforeGate = try providerContents()
        NSFileCoordinator.addFilePresenter(gate)
        defer { gate.open(); NSFileCoordinator.removeFilePresenter(gate) }
        model.prepareUpload(entry)
        for _ in 0..<500 {
            if gate.entered { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        try check(gate.entered && model.busy, "real coordinator is blocked behind actual file presenter")
        model.cancel(); model.cancel()
        try check(model.busy && GameSessionWork.shared.isSynchronizingSaves, "repeated page cancellation keeps guard while actual IO is unwinding")
        try reject("new game cannot begin until cancelled IO actually exits") { _ = try GameSessionWork.shared.beginLaunchPreparation() }
        gate.open()
        try await settle(model)
        try check(!GameSessionWork.shared.isSynchronizingSaves && model.pending == nil, "coordinator completion releases cancelled operation guard")
        try check(try LocalSaveCloudFiles.records(in: provider).count == afterExternal + 1 && providerContents() == cloudBeforeGate, "cancelled coordinated prepare performs zero additional version writes")
        let report: [String: Any] = ["checks": checks, "count": checks.count,
            "provider": "owned local bookmark/FileCoordinator simulation", "real_iCloud_tested": false]
        print(String(decoding: try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), as: UTF8.self))
    }
}
'''

DEFAULTS_CLEANUP = r'''
import Foundation
import Darwin
@main struct Cleanup {
    static func main() {
        let prefix = "Madeira.LocalSaveCloud.Tests."
        guard CommandLine.arguments.count == 2 else { exit(2) }
        let name = CommandLine.arguments[1]
        guard name.hasPrefix(prefix), UUID(uuidString: String(name.dropFirst(prefix.count))) != nil,
              let defaults = UserDefaults(suiteName: name) else { exit(2) }
        let before = defaults.persistentDomain(forName: name) ?? [:]
        defaults.removePersistentDomain(forName: name); defaults.synchronize()
        let after = defaults.persistentDomain(forName: name) ?? [:]
        let report: [String: Any] = ["domain": name, "beforeKeys": before.keys.sorted(), "afterEmpty": after.isEmpty]
        guard let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) else { exit(3) }
        print(String(decoding: data, as: UTF8.self)); exit(after.isEmpty ? 0 : 4)
    }
}
'''


def run_native(cloud_source: Path, label: str, optimized: bool, runtime_timeout: float = 30) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    domain = DOMAIN_PREFIX + str(uuid.uuid4())
    (OUT / f"{label}-scope.json").write_text(json.dumps({"domain": domain}, indent=2) + "\n")
    fixture = None
    fixture_identity = None
    outcome = {}
    with tempfile.TemporaryDirectory(prefix="compile-", dir=OUT) as temporary:
        compile_dir = Path(temporary)
        cleanup_source = compile_dir / "cleanup.swift"
        cleanup_binary = compile_dir / "cleanup"
        cleanup_source.write_text(DEFAULTS_CLEANUP)
        compiled_cleanup = subprocess.run(["xcrun", "swiftc", "-swift-version", "6", "-parse-as-library",
            str(cleanup_source), "-o", str(cleanup_binary)], text=True, capture_output=True, timeout=90)
        (OUT / f"{label}-cleanup-compile.log").write_text(compiled_cleanup.stdout + compiled_cleanup.stderr)
        if compiled_cleanup.returncode:
            raise RuntimeError("native cleanup helper failed before any domain or APFS fixture was opened")
        stage = "compile"
        try:
            main = compile_dir / "main.swift"
            doubles = compile_dir / "boundaries.swift"
            binary = compile_dir / "check"
            main.write_text(MAIN)
            doubles.write_text(BOUNDARIES)
            # Pin unchanged source bytes for this one execution. A concurrent
            # worker edit cannot make the report hash differ from the binary.
            inputs = {path.name: path.read_bytes() for path in (SNAPSHOT, cloud_source, SESSION)}
            for name, data in inputs.items():
                (compile_dir / name).write_bytes(data)
            command = ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete", "-warnings-as-errors", "-parse-as-library"]
            if optimized:
                command.append("-O")
            command.extend([str(compile_dir / path.name) for path in (SNAPSHOT, cloud_source, SESSION)])
            command.extend([str(doubles), str(main), "-o", str(binary)])
            compiled = subprocess.run(command, text=True, capture_output=True, timeout=120)
            (OUT / f"{label}-compile.log").write_text(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                raise RuntimeError(f"{label} production test failed to compile; not a runtime RED")
            fixture = Path(tempfile.mkdtemp(prefix=".madeira-local-save-cloud-", dir=Path.home()))
            fixture_stat = fixture.lstat()
            fixture_identity = (fixture_stat.st_dev, fixture_stat.st_ino)
            (OUT / f"{label}-scope.json").write_text(json.dumps({"domain": domain, "apfs_fixture": str(fixture)}, indent=2) + "\n")
            stage = "runtime"
            result = subprocess.run([str(binary), str(fixture), domain], text=True, capture_output=True, timeout=runtime_timeout)
            (OUT / f"{label}-runtime.log").write_text(result.stdout + result.stderr)
            outcome.update({"label": label, "optimized": optimized, "returncode": result.returncode,
                "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
                "report": json.loads(result.stdout) if result.returncode == 0 else None})
            (OUT / f"{label}-execution.json").write_text(json.dumps(outcome, indent=2) + "\n")
            return outcome
        finally:
            exception = sys.exc_info()[0]
            proof = {"domain": domain, "stage": stage, "execution_error": exception.__name__ if exception else None,
                     "native_returncode": None, "native": None,
                     "apfs_fixture": str(fixture) if fixture else None}
            try:
                cleaned = subprocess.run([str(cleanup_binary), domain], capture_output=True, text=True, timeout=20)
                (OUT / f"{label}-cleanup.log").write_text(cleaned.stdout + cleaned.stderr)
                proof["native_returncode"] = cleaned.returncode
                proof["native"] = json.loads(cleaned.stdout) if cleaned.returncode == 0 else None
            finally:
                # A defaults-cleanup failure must not strand independent APFS
                # resources. Refuse cleanup if the exact owned root was replaced.
                if fixture is not None:
                    status = fixture.lstat()
                    if (status.st_dev, status.st_ino) != fixture_identity:
                        raise RuntimeError("owned APFS fixture root changed identity; refused broad cleanup")
                    shutil.rmtree(fixture)
                proof["apfs_fixture_removed"] = fixture is None or not fixture.exists()
                (OUT / f"{label}-cleanup.json").write_text(json.dumps(proof, indent=2) + "\n")
            if proof["native_returncode"] or not proof["native"]["afterEmpty"] or not proof["apfs_fixture_removed"]:
                raise RuntimeError("exact owned defaults domain/APFS fixture cleanup failed")
            outcome["cleanup"] = proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-mutations", action="store_true")
    arguments = parser.parse_args()
    prefix = "python-optimized-" if sys.flags.optimize else ""
    production = [run_native(SOURCE, prefix + "debug", False), run_native(SOURCE, prefix + "optimized", True)]
    if any(result["returncode"] for result in production):
        raise RuntimeError("production native behavior failed; inspect retained runtime logs")
    mutation_results = []
    mutations = [
        ("ignore-payload-hash", "data.count == record.bytes, hash(data) == record.archiveHash", "data.count == record.bytes", "payload hash rejects semantically identical altered bytes"),
        ("ignore-metadata-filename", 'name == record.id.uuidString + ".json"', "true", "metadata filename and UUID must agree"),
        ("allow-unbound-folder", "guard allowed else", "guard allowed || true else", "directory rules reject whole user"),
        ("ignore-local-preview-change", "guard current.fingerprint == archive.fingerprint else { throw LocalSaveCloudFiles.Failure.changed }", "_ = current", "post-preview local change rejects upload before version writes"),
        ("release-cancelled-io-early", "if let task { task.cancel() }", "if let task { task.cancel(); if let owned = token { finish(owned) } }", "repeated page cancellation keeps guard while actual IO is unwinding"),
    ]
    if not arguments.no_mutations:
        original = SOURCE.read_text()
        with tempfile.TemporaryDirectory(prefix="mutants-", dir=OUT) as temporary:
            for name, old, new, expected in mutations:
                expected_count = 2 if name == "ignore-local-preview-change" else 1
                if original.count(old) != expected_count:
                    raise RuntimeError(f"mutation {name} no longer matches the known production boundary")
                mutant = Path(temporary) / "LocalSaveCloud.swift"
                mutant.write_text(original.replace(old, new, 1))
                result = run_native(mutant, prefix + name, True)
                runtime = (OUT / f"{prefix + name}-runtime.log").read_text()
                if result["returncode"] != 1 or expected not in runtime:
                    raise RuntimeError(f"mutation {name} did not reach the intended runtime assertion")
                mutation_results.append(result)
    current_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (SOURCE, SNAPSHOT, SESSION)}
    if any(result["source_sha256"] != current_hashes for result in production):
        raise RuntimeError("production source changed during verification; rerun against the final sources")
    report = {"production": production, "detected_runtime_mutations": mutation_results,
        "source_sha256": current_hashes,
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "real_iCloud_tested": False}
    (OUT / ("result-python-optimized.json" if prefix else "result.json")).write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {production[0]['report']['count']} production assertions in Swift 6 Debug/-O; {len(mutation_results)} compiled runtime mutants rejected; local provider only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
