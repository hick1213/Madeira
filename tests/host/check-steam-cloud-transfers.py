#!/usr/bin/env python3
"""Exercise production Cloud transfers on synthetic files and a scripted service.

The real paths, backup writer, SHA-1, protobuf codec, download and upload methods
run unchanged. Only Steam's service/HTTP boundary is substituted; no user save or
account is accessed. Cancellation boundaries deliberately return a reply after
cancel, as an async dependency need not immediately throw on cancellation.
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
    proto = (APP / "SwiftSteam/Proto/SteamProtoMessages.swift").read_text()
    compare = cloud[cloud.index("// MARK: - Where a cloud path lives in the Wine prefix"):
                    cloud.index("// MARK: - Download")]
    models = "\n".join([
        DECLARATION(cloud, "struct SteamCloudFile:"),
        DECLARATION(cloud, "struct SteamCloudListing:"),
        DECLARATION(proto, "enum ProtoWireType:"),
        DECLARATION(proto, "struct ProtobufEncoder"),
        DECLARATION(proto, "struct ProtobufDecoder"),
        DECLARATION(cloud, "struct SteamCloudPlan:"),
        DECLARATION(cloud, "struct SteamCloudState:"),
        DECLARATION(cloud, "struct SteamCloudUploadBlock:"),
    ])
    transfer = DECLARATION(cloud, "static func place(")
    cancelled = DECLARATION(owned, "private static func cloudCancelled")
    methods = "\n".join([
        DECLARATION(owned, "private func download("),
        DECLARATION(owned, "private func upload("),
    ])
    source = r'''
import Foundation
import CryptoKit
enum SteamLog { static func event(_ message: String) {} }
enum SteamFileError: Error { case invalid(String) }
enum SteamError: Error { case protobufError(String) }
enum SteamSignIn { static var accountName: String? = "synthetic"; static func message(_ error: Error) -> String { error.localizedDescription } }
struct SteamAppInfo {
    struct SaveFile { var root: String; var path: String; var pattern: String; var recursive: Bool; var platforms: [String] }
    struct RootOverride { var root: String; var os: String; var useInstead: String; var addPath: String }
    var buildID: UInt32 = 1
}
''' + compare + models + r'''
@MainActor enum SteamCloudTransfer {
    static let maxFileBytes = 256 << 20
    static var sendError: Error?
    static var onSend: (() -> Void)?
    static func send(_ data: Data, blocks: [SteamCloudUploadBlock]) async throws -> [Int] {
        onSend?()
        if let sendError { throw sendError }
        return [200]
    }
''' + transfer + r'''
}
@MainActor final class ScriptedSession {
    enum Method { case cloudCompleteAppUploadBatch, cloudBeginAppUploadBatch, cloudClientBeginFileUpload, cloudClientCommitFileUpload }
    var cellID: UInt32 = 0
    var accepted = true
    var began = 0
    var completed: [UInt64] = []
    var onCommit: (() -> Void)?
    var commitSucceeded: [Bool] = []
    func callServiceMethod(method: Method, body: Data, timeout: Int) async throws -> Data {
        var reply = ProtobufEncoder()
        switch method {
        case .cloudBeginAppUploadBatch:
            began += 1
            reply.writeUInt64(fieldNumber: 1, value: 55)
        case .cloudClientBeginFileUpload:
            break
        case .cloudClientCommitFileUpload:
            var success = false
            var request = ProtobufDecoder(body)
            while let tag = try request.readTag() {
                if tag.fieldNumber == 1 { success = try request.readVarint() != 0 }
                else { try request.skip(wireType: tag.wireType) }
            }
            commitSucceeded.append(success)
            onCommit?()
            reply.writeBool(fieldNumber: 1, value: accepted)
        case .cloudCompleteAppUploadBatch:
            var request = ProtobufDecoder(body)
            while let tag = try request.readTag() {
                if tag.fieldNumber == 3 { completed.append(try request.readVarint()) }
                else { try request.skip(wireType: tag.wireType) }
            }
        }
        return reply.data
    }
}
@MainActor final class Transfers {
    static let cloudEnabled = true
    static var fixture: URL!
    static let cloudClientID: UInt64 = 1
    static func cloudBackup(_ id: Int) -> URL { fixture.appendingPathComponent("backup/\(id)") }
    static func reason(_ error: Error) -> String { "test" }
    var cloud: [Int: SteamCloudState] = [1: SteamCloudState(phase: .ready, checked: Date())]
    var signedIn = true
    var inSession = false
    var cloudBlocked = false
    var cloudAccount = Transfers.accountKey(SteamSignIn.accountName)
    var cloudGeneration = UUID()
    var cloudBaseline: [String: [String: String]]?
    var cloudBaselineAccount: String?
    var cloudTakeover = false
    var cloudAudited = false
    var cloudWaitingFor: Int?
''' + DECLARATION(owned, "enum CloudQuit:") + r'''
    var cloudQuit: CloudQuit?
    let session = ScriptedSession()
    var cancelContext = false
    var cancelFileNumber = 0
    var reads = 0
    var interruptWithError = false
    var switchContext = false
    var switchFile = false
    var settled: [String: String] = [:]
    let paths: SteamCloudPaths
    init(_ folder: URL) {
        paths = SteamCloudPaths(drive: folder, userFolder: folder, installFolder: folder,
                                remoteFolder: folder, steamID: 1, overrides: [])
    }
    private func cloudContext(_ id: Int, identity: CloudIdentity) async throws -> (info: SteamAppInfo, paths: SteamCloudPaths)? {
        if switchContext { changeAccount() }
        if cancelContext { withUnsafeCurrentTask { $0?.cancel() } }
        return (SteamAppInfo(), paths)
    }
    private func cloudFile(_ id: Int, _ entry: SteamCloudEntry, identity: CloudIdentity) async throws -> Data {
        reads += 1
        if interruptWithError { throw CancellationError() }
        if switchFile { changeAccount() }
        if reads == cancelFileNumber { withUnsafeCurrentTask { $0?.cancel() } }
        return Data("fresh progress".utf8)
    }
    private func recordBaseline(_ id: Int, settled: [String: String], identity: CloudIdentity) { self.settled.merge(settled) { _, new in new } }
    func changeAccount() {
        SteamSignIn.accountName = "other-synthetic-account"
        resetCloudState()
        cloud[1] = SteamCloudState(phase: .ready, checked: Date(), problem: "new account comparison")
    }
''' + cancelled + "\n" + DECLARATION(owned, "private static func accountKey(") + "\n" + HELPERS["identity_members"](owned) + "\n" + methods + r'''
    func get(_ entries: [SteamCloudEntry]) async -> Bool { await download(1, entries) }
    func put(_ entries: [SteamCloudEntry]) async -> [String: Data] { await upload(1, entries) }
}

@main struct TransferChecks {
    @MainActor static func main() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        Transfers.fixture = directory
        let local = directory.appendingPathComponent("save.dat")
        let second = directory.appendingPathComponent("other.dat")
        let old = Data("old progress".utf8), fresh = Data("fresh progress".utf8)
        let expected = Data([255, 236, 23, 249, 207, 6, 6, 48, 141, 19, 135, 48, 86, 173, 131, 103, 217, 138, 247, 243])
        var failures = 0
        func check(_ condition: Bool, _ label: String) {
            print((condition ? "PASS: " : "FAIL: ") + label)
            if !condition { failures += 1 }
        }
        func wanted(_ name: String = "save.dat", kind: SteamCloudEntry.Kind = .cloudOnly) -> SteamCloudEntry {
            var entry = SteamCloudEntry(path: name, kind: kind)
            entry.cloudSHA = expected
            entry.localSHA = Data(repeating: 9, count: 20) // deliberately stale, not the current file's bytes
            return entry
        }

        try old.write(to: local)
        let cancelled = Transfers(directory)
        cancelled.cancelFileNumber = 1
        let interrupted = await Task { await cancelled.get([wanted()]) }.value
        check(!interrupted && (try? Data(contentsOf: local)) == old,
              "download reply after cancellation never replaces the device save")
        check(cancelled.cloud[1]?.checked == nil && cancelled.cloud[1]?.phase == .ready,
              "interrupted transfer remains retryable and requires a new comparison")

        let thrown = Transfers(directory)
        thrown.interruptWithError = true
        _ = await Task { await thrown.get([wanted()]) }.value
        check(!(thrown.cloud[1]?.problem ?? "").contains("Swift.CancellationError"),
              "download cancellation never displays Swift.CancellationError")

        try old.write(to: local)
        try old.write(to: second)
        let partial = Transfers(directory)
        partial.cancelFileNumber = 2
        _ = await Task { await partial.get([wanted(), wanted("other.dat")]) }.value
        check((try? Data(contentsOf: local)) == fresh && (try? Data(contentsOf: second)) == old,
              "cancellation preserves completed downloads and leaves the next save unchanged")
        check(partial.cloud[1]?.lastDownload?.files == 1 && partial.cloud[1]?.lastDownload?.backedUp == 1,
              "partial download reports only the file actually replaced and backed up")
        let backup = directory.appendingPathComponent("backup/1/device/remote/save.dat")
        check((try? Data(contentsOf: backup)) == old,
              "the original device save is backed up before replacement")

        try fresh.write(to: local)
        var upload = wanted(kind: .localOnly)
        upload.cloudSHA = Data()
        let accepted = Transfers(directory)
        let result = await accepted.put([upload])
        check(result == ["save.dat": expected] && accepted.cloud[1]?.lastUpload == 1,
              "upload confirmation carries the SHA of actual accepted bytes, not the earlier audit")
        check(accepted.session.completed == [1] && (try? Data(contentsOf: local)) == fresh,
              "successful upload completes the batch and leaves device bytes untouched")

        let rejected = Transfers(directory)
        rejected.session.accepted = false
        let rejectedResult = await rejected.put([upload])
        check(rejectedResult.isEmpty && rejected.cloud[1]?.problem != nil && rejected.session.completed == [2],
              "a rejected file is not counted as accepted and completes the batch as failed")

        let cancelledUpload = Transfers(directory)
        cancelledUpload.cancelContext = true
        let stopped = await Task { await cancelledUpload.put([upload]) }.value
        check(stopped.isEmpty && cancelledUpload.session.began == 0,
              "cancelled upload context never opens a remote write batch")

        SteamSignIn.accountName = "synthetic"
        try old.write(to: local)
        let otherDownload = Transfers(directory)
        otherDownload.switchFile = true
        let oldDownload = await otherDownload.get([wanted()])
        check(!oldDownload && (try? Data(contentsOf: local)) == old && otherDownload.settled.isEmpty,
              "a download reply from account A never overwrites or records a save after switching to B")
        check(otherDownload.cloud[1]?.problem == "new account comparison",
              "late download completion preserves the new account's published state")

        SteamSignIn.accountName = "synthetic"
        try fresh.write(to: local)
        let otherContext = Transfers(directory)
        otherContext.switchContext = true
        let oldContext = await otherContext.put([upload])
        check(oldContext.isEmpty && otherContext.session.began == 0,
              "an old upload context never opens a batch under the new account")

        SteamSignIn.accountName = "synthetic"
        let otherCommit = Transfers(directory)
        otherCommit.session.onCommit = { otherCommit.changeAccount() }
        let oldCommit = await otherCommit.put([upload])
        check(oldCommit.isEmpty && otherCommit.session.completed.isEmpty && otherCommit.cloud[1]?.lastUpload == nil,
              "an old commit reply cannot complete a batch or publish acceptance under the new account")
        check(otherCommit.cloud[1]?.problem == "new account comparison" && (try? Data(contentsOf: local)) == fresh,
              "late upload completion preserves B's state and the device's progress")

        SteamSignIn.accountName = "synthetic"
        let sendChangesAccount = Transfers(directory)
        SteamCloudTransfer.sendError = URLError(.timedOut)
        SteamCloudTransfer.onSend = { sendChangesAccount.changeAccount() }
        let failedOldSend = await sendChangesAccount.put([upload])
        check(failedOldSend.isEmpty && sendChangesAccount.session.commitSucceeded.isEmpty,
              "a failed HTTP send after switching account never issues a commit under the new account")
        check(sendChangesAccount.cloud[1]?.problem == "new account comparison" && (try? Data(contentsOf: local)) == fresh,
              "an old account's failed send preserves the new account's state and local progress")

        SteamSignIn.accountName = "synthetic"
        SteamCloudTransfer.onSend = nil
        let sameAccountFailure = Transfers(directory)
        let failedCurrentSend = await sameAccountFailure.put([upload])
        check(failedCurrentSend.isEmpty && sameAccountFailure.session.commitSucceeded == [false]
              && sameAccountFailure.session.completed == [2],
              "an unchanged account's failed HTTP send reports transfer failure and aborts its own batch")
        check(sameAccountFailure.cloud[1]?.problem != nil && (try? Data(contentsOf: local)) == fresh,
              "a failed current-account send remains retryable without changing the device save")
        SteamCloudTransfer.sendError = nil
        exit(failures == 0 ? 0 : 1)
    }
}
'''
    return HELPERS["run_check"](source, "Cloud transfer cancellation and accepted bytes")


if __name__ == "__main__":
    raise SystemExit(main())
