// SPDX-License-Identifier: GPL-3.0-or-later
// Native Epic installation storage; no Madeira Converter Exception is applied.

import Foundation
import CryptoKit
import Darwin

struct EpicInstallResult: Sendable {
    let executable: String
    let arguments: String
    let totalBytes: Int64
}

struct EpicDownloadCleanupIdentity: Codable, Sendable, Equatable {
    let device: UInt64
    let inode: UInt64
    let manifestHash: String?
    let manifestAppName: String?
}

enum EpicDownloadCleanupError: Error, LocalizedError, Equatable {
    case completed, ownership, changed, storage
    var errorDescription: String? {
        switch self {
        case .completed: return "This Epic download has already completed. Its files were preserved."
        case .ownership: return "Epic cannot safely identify these download files. They were preserved."
        case .changed: return "These download files changed or are still in use. Retry after the download stops."
        case .storage: return "Epic could not finish removing this download. Retry cleanup."
        }
    }
}

/// All file operations stay relative to directory descriptors. O_NOFOLLOW and
/// exclusive final-file rename protect user files even if a path changes while
/// downloading. Existing installs are only resumed for the identical manifest.
final class EpicInstallStore {
    /// Inspection never creates or rewrites installation ownership metadata.
    /// The caller persists this identity and cancellation UUID before deletion.
    static func inspectIncompleteDownload(destination: URL, cancellationID: UUID,
        expectedManifestHash: String? = nil,
        trustedContainer: URL = EpicDirectory.applicationContainer) throws -> EpicDownloadCleanupIdentity? {
        guard let session = try CleanupSession(destination: destination, cancellationID: cancellationID,
            identity: nil, trustedContainer: trustedContainer) else { return nil }
        return try session.preflight(expectedManifestHash: expectedManifestHash)
    }

    static func removeIncompleteDownload(destination: URL, cancellationID: UUID,
        identity: EpicDownloadCleanupIdentity,
        trustedContainer: URL = EpicDirectory.applicationContainer) throws {
        guard let session = try CleanupSession(destination: destination, cancellationID: cancellationID,
            identity: identity, trustedContainer: trustedContainer) else { return }
        _ = try session.preflight(expectedManifestHash: identity.manifestHash)
        try session.remove()
    }

    private struct CleanupNode {
        let name: String
        let logicalName: String
        let device: UInt64
        let inode: UInt64
        let size: Int64
        let hash: Data?
        let children: [CleanupNode]?
    }

    /// All names are interpreted relative to pinned descriptors. Deterministic
    /// quarantine and leaf aliases make interruption recoverable without ever
    /// adopting a newly created directory at the old download path.
    private final class CleanupSession {
        let parent: Int32
        let root: Int32
        var metadata: Int32 = -1
        var lock: Int32 = -1
        let sourceName: String
        let quarantineName: String
        let cancellationID: UUID
        let requestedIdentity: EpicDownloadCleanupIdentity?
        var quarantined: Bool
        var identity: EpicDownloadCleanupIdentity?
        var nodes: [CleanupNode] = []
        var count = 0
        var initialCreation = false
        var expected: [String: [String: EpicFileManifest?]] = [:]
        var receiptName: String { "cancel-receipt-" + cancellationID.uuidString }

        init?(destination: URL, cancellationID: UUID, identity: EpicDownloadCleanupIdentity?, trustedContainer: URL) throws {
            guard destination.isFileURL, !destination.path(percentEncoded: false).contains("\0") else {
                throw EpicDownloadCleanupError.ownership
            }
            let name = destination.lastPathComponent
            guard !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.contains("\0") else {
                throw EpicDownloadCleanupError.ownership
            }
            let parentPath: String
            do { parentPath = try safeParentPath(destination.deletingLastPathComponent().path(percentEncoded: false)) }
            catch { throw EpicDownloadCleanupError.ownership }
            let parentFD: Int32
            do {
                parentFD = try EpicDirectory.openDirectory(at: URL(fileURLWithPath: parentPath, isDirectory: true),
                    trustedContainer: trustedContainer, create: false)
            } catch let failure as EpicDirectory.Failure {
                if case let .operation(_, code) = failure, code == ENOENT { return nil }
                throw EpicDownloadCleanupError.ownership
            } catch { throw EpicDownloadCleanupError.ownership }
            let quarantine = ".madeira-cancel-" + digest(Data(name.utf8)) + "-" + cancellationID.uuidString
            var status = stat()
            let exists = fstatat(parentFD, quarantine, &status, AT_SYMLINK_NOFOLLOW) == 0
            if !exists, errno != ENOENT { close(parentFD); throw EpicDownloadCleanupError.storage }
            let selected = exists ? quarantine : name
            let rootFD = openat(parentFD, selected, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard rootFD >= 0 else {
                let code = errno
                close(parentFD)
                if code == ENOENT { return nil }
                throw EpicDownloadCleanupError.ownership
            }
            guard fstat(rootFD, &status) == 0, status.st_mode & S_IFMT == S_IFDIR else {
                close(rootFD); close(parentFD); throw EpicDownloadCleanupError.ownership
            }
            if let identity, UInt64(status.st_dev) != identity.device || UInt64(status.st_ino) != identity.inode {
                close(rootFD); close(parentFD); throw EpicDownloadCleanupError.changed
            }
            parent = parentFD; root = rootFD; sourceName = name; quarantineName = quarantine
            self.cancellationID = cancellationID; requestedIdentity = identity; quarantined = exists
        }

        deinit {
            if lock >= 0 { close(lock) }
            if metadata >= 0 { close(metadata) }
            close(root); close(parent)
        }

        func alias(_ name: String) -> String {
            if name.hasPrefix("partial-") || name.hasPrefix("write-") { return ".madeira-clean-" + name }
            return ".madeira-clean-" + digest(Data(name.utf8)) + "-" + cancellationID.uuidString
        }

        func names(_ directory: Int32) throws -> [String] {
            // dup shares the directory stream offset. Open a fresh description
            // so a second preflight cannot mistake an exhausted stream for an
            // empty directory after the first inspection.
            let fresh = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard fresh >= 0 else { throw EpicDownloadCleanupError.storage }
            guard let stream = fdopendir(fresh) else { close(fresh); throw EpicDownloadCleanupError.storage }
            defer { closedir(stream) }
            var result: [String] = []
            while true {
                errno = 0
                guard let entry = readdir(stream) else {
                    guard errno == 0 else { throw EpicDownloadCleanupError.storage }
                    return result
                }
                let name = withUnsafePointer(to: &entry.pointee.d_name) {
                    $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
                }
                if name != ".", name != ".." {
                    guard result.count < 250_000 else { throw EpicDownloadCleanupError.ownership }
                    result.append(name)
                }
            }
        }

        func actualName(_ directory: Int32, _ logical: String) throws -> String? {
            var status = stat()
            let original = fstatat(directory, logical, &status, AT_SYMLINK_NOFOLLOW) == 0
            if !original, errno != ENOENT { throw EpicDownloadCleanupError.storage }
            let alternate = alias(logical)
            let renamed = quarantined && fstatat(directory, alternate, &status, AT_SYMLINK_NOFOLLOW) == 0
            if !renamed, quarantined, errno != ENOENT { throw EpicDownloadCleanupError.storage }
            guard !(original && renamed) else { throw EpicDownloadCleanupError.changed }
            return original ? logical : (renamed ? alternate : nil)
        }

        func read(_ logical: String, maximum: Int) throws -> Data? {
            guard let name = try actualName(metadata, logical) else { return nil }
            let fd = openat(metadata, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
            guard fd >= 0 else { throw EpicDownloadCleanupError.ownership }
            defer { close(fd) }
            var status = stat()
            guard isRegular(fd), fstat(fd, &status) == 0, status.st_size >= 0,
                status.st_size <= maximum else { throw EpicDownloadCleanupError.ownership }
            var data = Data(count: Int(status.st_size))
            try data.withUnsafeMutableBytes { buffer in
                var offset = 0
                while offset < buffer.count {
                    let bytes = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                    if bytes < 0, errno == EINTR { continue }
                    guard bytes > 0 else { throw EpicDownloadCleanupError.storage }
                    offset += bytes
                }
            }
            return data
        }

        func preflight(expectedManifestHash: String?) throws -> EpicDownloadCleanupIdentity {
            if let expectedManifestHash, !Self.validHash(expectedManifestHash) {
                throw EpicDownloadCleanupError.ownership
            }
            var status = stat()
            guard fstat(root, &status) == 0 else { throw EpicDownloadCleanupError.storage }
            let device = UInt64(status.st_dev), inode = UInt64(status.st_ino)
            guard let metaName = try actualName(root, ".madeira-epic") else {
                guard try names(root).isEmpty else {
                    throw EpicDownloadCleanupError.ownership
                }
                let found = requestedIdentity ?? EpicDownloadCleanupIdentity(device: device, inode: inode,
                    manifestHash: expectedManifestHash, manifestAppName: nil)
                initialCreation = !quarantined
                identity = found; nodes = []; return found
            }
            metadata = openat(root, metaName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard metadata >= 0 else { throw EpicDownloadCleanupError.ownership }
            // The presence of *any* completion entry protects the entire root.
            if fstatat(metadata, "complete", &status, AT_SYMLINK_NOFOLLOW) == 0 { throw EpicDownloadCleanupError.completed }
            guard errno == ENOENT else { throw EpicDownloadCleanupError.storage }
            if let lockName = try actualName(metadata, "lock") {
                lock = openat(metadata, lockName, O_RDWR | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
                guard lock >= 0, isRegular(lock), flock(lock, LOCK_EX | LOCK_NB) == 0 else {
                    throw EpicDownloadCleanupError.changed
                }
            } else {
                let remaining = try names(metadata)
                // An originally empty metadata directory has no lock. Its
                // receipt may already have been written before interruption;
                // only a persisted, pinned identity can authorize that retry.
                guard remaining.isEmpty || (requestedIdentity != nil && remaining.count == 1 &&
                    (remaining[0] == receiptName || (quarantined && remaining[0] == alias(receiptName)))) else {
                    throw EpicDownloadCleanupError.ownership
                }
            }
            let savedManifest = try read("manifest", maximum: EpicManifest.maximumManifestBytes)
            let savedCheckpoint = try read("checkpoint.json", maximum: EpicManifest.maximumManifestBytes)
            let savedReceipt = try read(receiptName, maximum: 4096)
            let receipt = savedReceipt.flatMap { try? JSONDecoder().decode(EpicDownloadCleanupIdentity.self, from: $0) }
            if savedReceipt != nil, receipt == nil { throw EpicDownloadCleanupError.ownership }
            if let receipt, let requestedIdentity, receipt != requestedIdentity { throw EpicDownloadCleanupError.changed }
            let initialCheckpoint = savedCheckpoint.flatMap { try? JSONDecoder().decode(Checkpoint.self, from: $0) }
            let initialCheckpointShape = savedCheckpoint == nil || initialCheckpoint?.completed.isEmpty == true
            if savedManifest == nil, initialCheckpointShape, savedReceipt == nil || receipt == requestedIdentity,
                !quarantined || requestedIdentity?.manifestAppName == nil || savedCheckpoint != nil {
                // init writes lock, then checkpoint, then manifest, and only
                // afterwards creates chunks/game files. A strict footprint
                // from that first phase owns no installed game or cache data.
                let checkpoint = initialCheckpoint
                if savedCheckpoint != nil {
                    guard let checkpoint, checkpoint.version == 1, checkpoint.completed.isEmpty,
                        Self.validHash(checkpoint.manifestHash), Self.validBuilder(checkpoint.appName),
                        expectedManifestHash == nil || expectedManifestHash == checkpoint.manifestHash else {
                        throw EpicDownloadCleanupError.ownership
                    }
                }
                let found = EpicDownloadCleanupIdentity(device: device, inode: inode,
                    manifestHash: checkpoint?.manifestHash ?? expectedManifestHash, manifestAppName: checkpoint?.appName)
                if let requestedIdentity, requestedIdentity != found { throw EpicDownloadCleanupError.changed }
                expected[""] = [".madeira-epic": nil]
                expected[".madeira-epic"] = ["lock": nil, "checkpoint.json": nil, receiptName: nil]
                for name in try names(metadata) {
                    let logical = quarantined && name.hasPrefix(".madeira-clean-") ? String(name.dropFirst(".madeira-clean-".count)) : name
                    if logical.hasPrefix("write-"), Self.privateTemporary(logical) {
                        expected[".madeira-epic", default: [:]][logical] = .some(nil)
                    }
                }
                nodes = try scan(root, path: "", depth: 0, tail: true)
                initialCreation = true; identity = found; return found
            }
            if let manifestData = savedManifest, let checkpointData = savedCheckpoint {
                guard let checkpoint = try? JSONDecoder().decode(Checkpoint.self, from: checkpointData),
                      checkpoint.version == 1, checkpoint.completed.count <= 250_000,
                      let manifest = try? EpicManifest.parse(manifestData),
                      checkpoint.manifestHash == digest(manifestData), checkpoint.appName == manifest.appName,
                      expectedManifestHash == nil || expectedManifestHash == checkpoint.manifestHash else {
                    throw EpicDownloadCleanupError.ownership
                }
                for path in checkpoint.completed {
                    do { _ = try EpicManifest.safePath(path) } catch { throw EpicDownloadCleanupError.ownership }
                }
                let found = EpicDownloadCleanupIdentity(device: device, inode: inode,
                    manifestHash: checkpoint.manifestHash, manifestAppName: manifest.appName)
                if let requestedIdentity, requestedIdentity != found { throw EpicDownloadCleanupError.changed }
                if let receipt, receipt != found { throw EpicDownloadCleanupError.changed }
                identity = found
                // Optional dictionary values describe declared directories;
                // actual game files additionally carry their expected SHA1.
                expected[""] = [".madeira-epic": nil]
                for file in manifest.fileManifestList {
                    let parts = file.filename.split(separator: "/").map(String.init)
                    var prefix = ""
                    for (index, part) in parts.enumerated() {
                        expected[prefix, default: [:]][part] = .some(index == parts.count - 1 ? file : nil)
                        prefix = prefix.isEmpty ? part : prefix + "/" + part
                    }
                }
                expected[".madeira-epic"] = ["lock": nil, "manifest": nil, "checkpoint.json": nil,
                    "chunks": nil, receiptName: nil]
                expected[".madeira-epic/chunks"] = Dictionary(uniqueKeysWithValues: manifest.chunks.keys.map { ($0, Optional<EpicFileManifest>.none) })
                nodes = try scan(root, path: "", depth: 0, tail: false)
                return found
            }
            // A cleanup can crash while retiring ownership metadata. Only its
            // pinned quarantine, persisted identity, matching receipt and an
            // otherwise empty root may continue this final phase. Once receipt
            // is gone, only the lock or empty directories may remain.
            guard quarantined, let requestedIdentity,
                  receipt == requestedIdentity || (savedReceipt == nil && savedManifest == nil && savedCheckpoint == nil) else {
                throw EpicDownloadCleanupError.ownership
            }
            if let savedManifest, digest(savedManifest) != requestedIdentity.manifestHash { throw EpicDownloadCleanupError.ownership }
            if let savedCheckpoint {
                guard let checkpoint = try? JSONDecoder().decode(Checkpoint.self, from: savedCheckpoint),
                    checkpoint.version == 1, checkpoint.manifestHash == requestedIdentity.manifestHash,
                    checkpoint.appName == requestedIdentity.manifestAppName else { throw EpicDownloadCleanupError.ownership }
            }
            identity = requestedIdentity
            expected[""] = [".madeira-epic": nil]
            expected[".madeira-epic"] = ["lock": nil, "manifest": nil, "checkpoint.json": nil, receiptName: nil]
            nodes = try scan(root, path: "", depth: 0, tail: true)
            return requestedIdentity
        }

        func scan(_ directory: Int32, path: String, depth: Int, tail: Bool) throws -> [CleanupNode] {
            guard depth <= 64 else { throw EpicDownloadCleanupError.ownership }
            let allowed = expected[path] ?? [:]
            var aliases: [String: String] = [:]
            if quarantined {
                for name in allowed.keys {
                    let alternate = alias(name)
                    guard allowed[alternate] == nil, aliases[alternate] == nil else { throw EpicDownloadCleanupError.ownership }
                    aliases[alternate] = name
                }
            }
            var seen = Set<String>(), result: [CleanupNode] = []
            for name in try names(directory) {
                count += 1
                guard count <= 250_000 else { throw EpicDownloadCleanupError.ownership }
                var logical = allowed[name] != nil ? name : aliases[name]
                if logical == nil, !tail, path == ".madeira-epic" {
                    let candidate = name.hasPrefix(".madeira-clean-") && quarantined ? String(name.dropFirst(".madeira-clean-".count)) : name
                    if Self.privateTemporary(candidate) { logical = candidate }
                }
                guard let logical, seen.insert(logical).inserted else { throw EpicDownloadCleanupError.ownership }
                let childPath = path.isEmpty ? logical : path + "/" + logical
                let isDirectory = childPath == ".madeira-epic" || (!tail && (childPath == ".madeira-epic/chunks" || expected[childPath] != nil))
                let fd = openat(directory, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC | (isDirectory ? O_DIRECTORY : 0))
                guard fd >= 0 else { throw EpicDownloadCleanupError.ownership }
                do {
                    var status = stat()
                    guard fstat(fd, &status) == 0 else { throw EpicDownloadCleanupError.storage }
                    let children: [CleanupNode]?, hash: Data?
                    if isDirectory {
                        guard status.st_mode & S_IFMT == S_IFDIR else { throw EpicDownloadCleanupError.ownership }
                        children = try scan(fd, path: childPath, depth: depth + 1, tail: tail); hash = nil
                    } else {
                        guard isRegular(fd), status.st_size >= 0,
                            status.st_size <= 512 * 1024 * 1024 * 1024 else { throw EpicDownloadCleanupError.ownership }
                        let hashes = try Self.hashes(fd, size: status.st_size)
                        if let game = allowed[logical] ?? nil {
                            guard status.st_size == game.fileSize, hashes.sha1 == game.fileHash else {
                                throw EpicDownloadCleanupError.ownership
                            }
                        }
                        if logical == "lock", path == ".madeira-epic", status.st_size != 0 { throw EpicDownloadCleanupError.ownership }
                        children = nil; hash = hashes.sha256
                    }
                    result.append(CleanupNode(name: name, logicalName: logical, device: UInt64(status.st_dev),
                        inode: UInt64(status.st_ino), size: status.st_size, hash: hash, children: children))
                    close(fd)
                } catch { close(fd); throw error }
            }
            return result
        }

        static func privateTemporary(_ name: String) -> Bool {
            let suffix: String
            if name.hasPrefix("partial-") { suffix = String(name.dropFirst(8)) }
            else if name.hasPrefix("write-") { suffix = String(name.dropFirst(6)) }
            else { return false }
            return UUID(uuidString: suffix)?.uuidString == suffix
        }

        static func validHash(_ value: String) -> Bool {
            value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
        }

        static func validBuilder(_ value: String) -> Bool {
            !value.isEmpty && value.utf8.count <= 256 && !value.unicodeScalars.contains { $0.value < 32 }
        }

        static func hashes(_ fd: Int32, size: Int64) throws -> (sha1: Data, sha256: Data) {
            guard lseek(fd, 0, SEEK_SET) == 0 else { throw EpicDownloadCleanupError.storage }
            var sha1 = Insecure.SHA1(), sha256 = SHA256(), length: Int64 = 0
            var buffer = [UInt8](repeating: 0, count: 1024 * 1024)
            while true {
                let bytes = Darwin.read(fd, &buffer, buffer.count)
                if bytes < 0, errno == EINTR { continue }
                guard bytes >= 0 else { throw EpicDownloadCleanupError.storage }
                if bytes == 0 { break }
                length += Int64(bytes)
                guard length <= size else { throw EpicDownloadCleanupError.changed }
                let data = Data(buffer.prefix(bytes)); sha1.update(data: data); sha256.update(data: data)
            }
            guard length == size else { throw EpicDownloadCleanupError.changed }
            return (Data(sha1.finalize()), Data(sha256.finalize()))
        }

        func matches(_ fd: Int32, _ node: CleanupNode) -> Bool {
            var status = stat()
            return fstat(fd, &status) == 0 && UInt64(status.st_dev) == node.device && UInt64(status.st_ino) == node.inode &&
                (node.children != nil ? status.st_mode & S_IFMT == S_IFDIR :
                    (status.st_mode & S_IFMT == S_IFREG && status.st_nlink == 1 && status.st_size == node.size))
        }

        func removeNode(_ node: CleanupNode, parent: Int32) throws {
            let fd = openat(parent, node.name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC | (node.children != nil ? O_DIRECTORY : 0))
            guard fd >= 0 else { throw EpicDownloadCleanupError.changed }
            defer { close(fd) }
            guard matches(fd, node) else { throw EpicDownloadCleanupError.changed }
            let renamed = alias(node.logicalName)
            if node.name != renamed {
                guard renameatx_np(parent, node.name, parent, renamed, UInt32(RENAME_EXCL)) == 0 else {
                    throw EpicDownloadCleanupError.changed
                }
            }
            let check = openat(parent, renamed, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC | (node.children != nil ? O_DIRECTORY : 0))
            guard check >= 0 else { throw EpicDownloadCleanupError.changed }
            defer { close(check) }
            guard matches(check, node) else { throw EpicDownloadCleanupError.changed }
            if let children = node.children {
                for child in children.sorted(by: { priority($0.logicalName) < priority($1.logicalName) }) {
                    try removeNode(child, parent: check)
                }
                guard try names(check).isEmpty else { throw EpicDownloadCleanupError.changed }
            } else {
                guard try Self.hashes(check, size: node.size).sha256 == node.hash else { throw EpicDownloadCleanupError.changed }
            }
            var current = stat()
            guard fstatat(parent, renamed, &current, AT_SYMLINK_NOFOLLOW) == 0,
                UInt64(current.st_dev) == node.device, UInt64(current.st_ino) == node.inode,
                (node.children != nil ? current.st_mode & S_IFMT == S_IFDIR :
                    (current.st_mode & S_IFMT == S_IFREG && current.st_nlink == 1 && current.st_size == node.size)) else {
                throw EpicDownloadCleanupError.changed
            }
            // Only the exclusively renamed, reverified name is unlinked.
            guard unlinkat(parent, renamed, node.children == nil ? 0 : AT_REMOVEDIR) == 0, fsync(parent) == 0 else {
                throw EpicDownloadCleanupError.storage
            }
        }

        func priority(_ name: String) -> Int {
            switch name {
            case ".madeira-epic": return 10
            case "manifest": return 20
            case "checkpoint.json": return 21
            case receiptName: return 22
            case "lock": return 23
            default: return 0
            }
        }

        func unchanged(_ before: [CleanupNode], _ after: [CleanupNode], metadata: Bool = false) -> Bool {
            let old = Dictionary(uniqueKeysWithValues: before.map { ($0.logicalName, $0) })
            let new = Dictionary(uniqueKeysWithValues: after.map { ($0.logicalName, $0) })
            let ignored = metadata ? Set([receiptName]) : Set<String>()
            guard Set(old.keys).subtracting(ignored) == Set(new.keys).subtracting(ignored) else { return false }
            for (name, node) in old where !ignored.contains(name) {
                guard let current = new[name], node.device == current.device, node.inode == current.inode,
                    node.hash == current.hash, (node.children == nil) == (current.children == nil) else { return false }
                if let children = node.children, let updated = current.children,
                    !unchanged(children, updated, metadata: name == ".madeira-epic") { return false }
            }
            return true
        }

        func remove() throws {
            guard let identity else { throw EpicDownloadCleanupError.ownership }
            if !quarantined {
                var before = stat()
                guard fstatat(parent, sourceName, &before, AT_SYMLINK_NOFOLLOW) == 0,
                    before.st_mode & S_IFMT == S_IFDIR, UInt64(before.st_dev) == identity.device,
                    UInt64(before.st_ino) == identity.inode else { throw EpicDownloadCleanupError.changed }
                if metadata >= 0 {
                    if fstatat(metadata, "complete", &before, AT_SYMLINK_NOFOLLOW) == 0 {
                        throw EpicDownloadCleanupError.completed
                    }
                    guard errno == ENOENT else { throw EpicDownloadCleanupError.storage }
                }
                // No deletion or rename occurs until *all* files pass preflight.
                // Write recovery proof before moving the original owned root.
                if metadata >= 0 {
                    do { try atomicWrite(metadata, name: receiptName, data: JSONEncoder().encode(identity)) }
                    catch { throw EpicDownloadCleanupError.storage }
                }
                guard renameatx_np(parent, sourceName, parent, quarantineName, UInt32(RENAME_EXCL)) == 0 else {
                    throw EpicDownloadCleanupError.changed
                }
                quarantined = true
                let pinned = openat(parent, quarantineName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard pinned >= 0 else { throw EpicDownloadCleanupError.changed }
                defer { close(pinned) }
                var status = stat()
                guard fstat(pinned, &status) == 0, UInt64(status.st_dev) == identity.device,
                    UInt64(status.st_ino) == identity.inode else {
                    // A racing replacement is preserved and restored when its
                    // previous name is still free; never overwrite a new root.
                    _ = renameatx_np(parent, quarantineName, parent, sourceName, UInt32(RENAME_EXCL))
                    throw EpicDownloadCleanupError.changed
                }
                guard fsync(parent) == 0 else { throw EpicDownloadCleanupError.storage }
                // Include the new receipt in the snapshot; all other entries
                // must still satisfy full ownership and game-file verification.
                count = 0
                let updated = try scan(root, path: "", depth: 0, tail: initialCreation)
                guard unchanged(nodes, updated) else { throw EpicDownloadCleanupError.changed }
                nodes = updated
            }
            for node in nodes.sorted(by: { priority($0.logicalName) < priority($1.logicalName) }) {
                try removeNode(node, parent: root)
            }
            guard try names(root).isEmpty else { throw EpicDownloadCleanupError.changed }
            let pinned = openat(parent, quarantineName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard pinned >= 0 else { throw EpicDownloadCleanupError.changed }
            defer { close(pinned) }
            var status = stat()
            guard fstat(pinned, &status) == 0, UInt64(status.st_dev) == identity.device,
                UInt64(status.st_ino) == identity.inode else { throw EpicDownloadCleanupError.changed }
            guard fstatat(parent, quarantineName, &status, AT_SYMLINK_NOFOLLOW) == 0,
                status.st_mode & S_IFMT == S_IFDIR, UInt64(status.st_dev) == identity.device,
                UInt64(status.st_ino) == identity.inode else { throw EpicDownloadCleanupError.changed }
            guard unlinkat(parent, quarantineName, AT_REMOVEDIR) == 0, fsync(parent) == 0 else {
                throw EpicDownloadCleanupError.storage
            }
        }
    }

    private let root: Int32
    private let metadata: Int32
    private let lock: Int32
    private let manifestHash: String
    private let appName: String
    private let rootPath: String

    private struct Checkpoint: Codable {
        let version: Int
        let manifestHash: String
        let appName: String
        // Retained for decoding older checkpoints. Resume always verifies
        // actual manifest files; this list is never proof of completion.
        let completed: [String]
    }

    init(destination: URL, manifestData: Data, appName: String,
         trustedContainer: URL = EpicDirectory.applicationContainer) throws {
        guard destination.isFileURL, !destination.path(percentEncoded: false).contains("\0") else {
            throw EpicDownloadError.unsafeDestination
        }
        self.manifestHash = Self.digest(manifestData)
        self.appName = appName
        // Only the protected system /var alias is normalized. User-controlled
        // ancestors remain visible to the O_NOFOLLOW descriptor walk below.
        var status = stat()
        if lstat(destination.path, &status) == 0, (status.st_mode & S_IFMT) != S_IFDIR {
            throw EpicDownloadError.unsafeDestination
        }
        let parent = try Self.safeParentPath(destination.deletingLastPathComponent().path(percentEncoded: false))
        let parentFD: Int32
        do {
            parentFD = try EpicDirectory.openDirectory(at: URL(fileURLWithPath: parent, isDirectory: true),
                                                       trustedContainer: trustedContainer, create: true)
        } catch {
            throw EpicDownloadError.unsafeDestination
        }
        defer { close(parentFD) }
        let name = destination.lastPathComponent
        guard !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.contains("\0") else {
            throw EpicDownloadError.unsafeDestination
        }
        rootPath = parent + "/" + name
        if mkdirat(parentFD, name, 0o700) != 0, errno != EEXIST { throw EpicDownloadError.filesystem }
        let rootFD = openat(parentFD, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard rootFD >= 0 else { throw EpicDownloadError.unsafeDestination }
        var metadataFD: Int32 = -1
        var lockFD: Int32 = -1
        do {
            var metaStatus = stat()
            let existingMetadata = fstatat(rootFD, ".madeira-epic", &metaStatus, AT_SYMLINK_NOFOLLOW) == 0
            if existingMetadata {
                guard (metaStatus.st_mode & S_IFMT) == S_IFDIR else { throw EpicDownloadError.unsafeDestination }
            } else {
                // An unrelated folder is never silently adopted as an install.
                guard try Self.directoryNames(rootFD).isEmpty else { throw EpicDownloadError.unsafeDestination }
                guard mkdirat(rootFD, ".madeira-epic", 0o700) == 0 else { throw EpicDownloadError.filesystem }
            }
            metadataFD = openat(rootFD, ".madeira-epic", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard metadataFD >= 0 else { throw EpicDownloadError.unsafeDestination }
            lockFD = openat(metadataFD, "lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard lockFD >= 0, Self.isRegular(lockFD), flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
                throw EpicDownloadError.unsafeDestination
            }
            if let saved = try Self.readFile(metadataFD, "checkpoint.json", maximum: EpicManifest.maximumManifestBytes) {
                guard let checkpoint = try? JSONDecoder().decode(Checkpoint.self, from: saved),
                      checkpoint.version == 1, checkpoint.manifestHash == manifestHash,
                      checkpoint.appName == appName, checkpoint.completed.count <= 250_000 else {
                    throw EpicDownloadError.differentInstall
                }
                for path in checkpoint.completed { _ = try EpicManifest.safePath(path) }
            } else {
                // Existing metadata without ownership is not trusted. An initial
                // interrupted creation may leave just our lock and can be retried.
                guard try Self.directoryNames(metadataFD).allSatisfy({ $0 == "lock" }) else {
                    throw EpicDownloadError.differentInstall
                }
            }
            root = rootFD; metadata = metadataFD; lock = lockFD
        } catch {
            if lockFD >= 0 { close(lockFD) }
            if metadataFD >= 0 { close(metadataFD) }
            close(rootFD)
            throw error
        }
        try checkpoint()
        try atomicMetadata("manifest", data: manifestData)
        if mkdirat(metadata, "chunks", 0o700) != 0, errno != EEXIST { throw EpicDownloadError.filesystem }
        let chunksFD = openat(metadata, "chunks", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard chunksFD >= 0 else { throw EpicDownloadError.unsafeDestination }
        close(chunksFD)
    }

    deinit { close(lock); close(metadata); close(root) }

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    func verifiedExisting(_ file: EpicFileManifest) throws -> Bool {
        let (directory, name) = try fileParent(file.filename, create: false)
        guard directory >= 0 else { return false }
        defer { close(directory) }
        let descriptor = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if descriptor < 0 {
            guard errno == ENOENT else { throw EpicDownloadError.unsafeDestination }
            return false
        }
        defer { close(descriptor) }
        guard Self.isRegular(descriptor) else { throw EpicDownloadError.unsafeDestination }
        // The hash, not the checkpoint, is authoritative after a restart.
        guard try Self.fileHash(descriptor, expectedSize: file.fileSize) == file.fileHash else {
            throw EpicDownloadError.existingFile
        }
        return true
    }

    func cachedChunk(_ chunk: EpicChunkInfo) throws -> Data? {
        let dir = try chunkDirectory()
        defer { close(dir) }
        guard let data = try Self.readFile(dir, chunk.guid, maximum: EpicManifest.maximumChunkBytes) else { return nil }
        guard data.count == chunk.windowSize, Data(Insecure.SHA1.hash(data: data)) == chunk.sha1 else {
            // Only this install's private chunk cache is replaced. Game files
            // and user data are never removed for a failed resume check.
            guard unlinkat(dir, chunk.guid, 0) == 0 else { throw EpicDownloadError.filesystem }
            return nil
        }
        return data
    }

    func cacheChunk(_ chunk: EpicChunkInfo, data: Data) throws {
        let dir = try chunkDirectory()
        defer { close(dir) }
        try Self.atomicWrite(dir, name: chunk.guid, data: data)
    }

    func makePartial() throws -> (Int32, String) {
        let name = "partial-" + UUID().uuidString
        let fd = openat(metadata, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw EpicDownloadError.filesystem }
        return (fd, name)
    }

    func discardPartial(_ name: String) { _ = unlinkat(metadata, name, 0) }

    func commitPartial(_ name: String, descriptor: Int32, file: EpicFileManifest) throws {
        let (parent, leaf) = try fileParent(file.filename, create: true)
        defer { close(parent) }
        var opened = stat(), source = stat(), committed = stat()
        guard fstat(descriptor, &opened) == 0, fstatat(metadata, name, &source, AT_SYMLINK_NOFOLLOW) == 0,
              source.st_mode & S_IFMT == S_IFREG, source.st_nlink == 1,
              source.st_dev == opened.st_dev, source.st_ino == opened.st_ino else {
            throw EpicDownloadError.unsafeDestination
        }
        // RENAME_EXCL never replaces a differing existing user/game file.
        guard renameatx_np(metadata, name, parent, leaf, UInt32(RENAME_EXCL)) == 0 else {
            throw errno == EEXIST ? EpicDownloadError.existingFile : EpicDownloadError.filesystem
        }
        guard fstatat(parent, leaf, &committed, AT_SYMLINK_NOFOLLOW) == 0,
              committed.st_mode & S_IFMT == S_IFREG, committed.st_nlink == 1,
              committed.st_dev == opened.st_dev, committed.st_ino == opened.st_ino else {
            throw EpicDownloadError.unsafeDestination
        }
        guard fsync(parent) == 0 else { throw EpicDownloadError.filesystem }
    }

    func complete(chunks: [String: EpicChunkInfo]) throws {
        var opened = stat(), current = stat()
        guard fstat(root, &opened) == 0, lstat(rootPath, &current) == 0,
              current.st_mode & S_IFMT == S_IFDIR, opened.st_dev == current.st_dev,
              opened.st_ino == current.st_ino else { throw EpicDownloadError.unsafeDestination }
        try checkpoint()
        // Completed game files are the durable resume source. Release only
        // this manifest's verified private chunk-cache filenames after success.
        let directory = try chunkDirectory()
        defer { close(directory) }
        for guid in chunks.keys {
            var status = stat()
            if fstatat(directory, guid, &status, AT_SYMLINK_NOFOLLOW) == 0 {
                guard status.st_mode & S_IFMT == S_IFREG, status.st_nlink == 1 else {
                    throw EpicDownloadError.unsafeDestination
                }
                guard unlinkat(directory, guid, 0) == 0 else { throw EpicDownloadError.filesystem }
            } else if errno != ENOENT { throw EpicDownloadError.filesystem }
        }
        guard fsync(directory) == 0 else { throw EpicDownloadError.filesystem }
        try atomicMetadata("complete", data: Data(manifestHash.utf8))
    }

    static func write(_ descriptor: Int32, data: Data) throws {
        try data.withUnsafeBytes { buffer in
            guard let base = buffer.baseAddress else { return }
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(descriptor, base.advanced(by: offset), buffer.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { throw EpicDownloadError.filesystem }
                offset += count
            }
        }
    }

    private func checkpoint() throws {
        // Ownership is durable before any game file is written. Cancellation
        // and failures preserve committed files/chunks, which resume re-hashes.
        // Avoid rewriting an ever-growing journal for every small game file.
        let checkpoint = Checkpoint(version: 1, manifestHash: manifestHash, appName: appName, completed: [])
        try atomicMetadata("checkpoint.json", data: JSONEncoder().encode(checkpoint))
    }

    private func atomicMetadata(_ name: String, data: Data) throws {
        try Self.atomicWrite(metadata, name: name, data: data)
    }

    private func chunkDirectory() throws -> Int32 {
        let dir = openat(metadata, "chunks", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dir >= 0 else { throw EpicDownloadError.unsafeDestination }
        return dir
    }

    private func fileParent(_ path: String, create: Bool) throws -> (Int32, String) {
        let parts = path.split(separator: "/").map(String.init)
        var current = dup(root)
        guard current >= 0 else { throw EpicDownloadError.filesystem }
        do {
            for component in parts.dropLast() {
                if create, mkdirat(current, component, 0o700) != 0, errno != EEXIST {
                    throw EpicDownloadError.filesystem
                }
                let next = openat(current, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                if next < 0 {
                    if !create, errno == ENOENT { close(current); return (-1, parts.last!) }
                    throw EpicDownloadError.unsafeDestination
                }
                close(current); current = next
            }
            return (current, parts.last!)
        } catch { close(current); throw error }
    }

    private static func safeParentPath(_ path: String) throws -> String {
        guard path.hasPrefix("/"), !path.contains("\0"),
              path.split(separator: "/").allSatisfy({ $0 != "." && $0 != ".." }) else {
            throw EpicDownloadError.unsafeDestination
        }
        if path == "/var" || path.hasPrefix("/var/") { return "/private" + path }
        return path
    }

    private static func isRegular(_ fd: Int32) -> Bool {
        var status = stat()
        return fstat(fd, &status) == 0 && status.st_mode & S_IFMT == S_IFREG && status.st_nlink == 1
    }

    private static func readFile(_ directory: Int32, _ name: String, maximum: Int) throws -> Data? {
        let fd = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0 {
            guard errno == ENOENT else { throw EpicDownloadError.unsafeDestination }
            return nil
        }
        defer { close(fd) }
        var status = stat()
        guard isRegular(fd), fstat(fd, &status) == 0, status.st_size >= 0, status.st_size <= maximum else {
            throw EpicDownloadError.unsafeDestination
        }
        var data = Data(count: Int(status.st_size))
        try data.withUnsafeMutableBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { throw EpicDownloadError.filesystem }
                offset += count
            }
        }
        return data
    }

    private static func fileHash(_ fd: Int32, expectedSize: Int64) throws -> Data {
        var status = stat()
        guard fstat(fd, &status) == 0, status.st_size == expectedSize else { throw EpicDownloadError.existingFile }
        var hash = Insecure.SHA1(), buffer = [UInt8](repeating: 0, count: 1024 * 1024)
        var length: Int64 = 0
        while true {
            try Task.checkCancellation()
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0, errno == EINTR { continue }
            guard count >= 0 else { throw EpicDownloadError.filesystem }
            if count == 0 { break }
            hash.update(data: Data(buffer.prefix(count)))
            length += Int64(count)
            guard length <= expectedSize else { throw EpicDownloadError.existingFile }
        }
        guard length == expectedSize else { throw EpicDownloadError.existingFile }
        return Data(hash.finalize())
    }

    private static func atomicWrite(_ directory: Int32, name: String, data: Data) throws {
        var status = stat()
        if fstatat(directory, name, &status, AT_SYMLINK_NOFOLLOW) == 0 {
            guard status.st_mode & S_IFMT == S_IFREG, status.st_nlink == 1 else { throw EpicDownloadError.unsafeDestination }
        } else if errno != ENOENT { throw EpicDownloadError.filesystem }
        let temporary = "write-" + UUID().uuidString
        let fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw EpicDownloadError.filesystem }
        defer { close(fd); _ = unlinkat(directory, temporary, 0) }
        try write(fd, data: data)
        guard fsync(fd) == 0, renameat(directory, temporary, directory, name) == 0, fsync(directory) == 0 else {
            throw EpicDownloadError.filesystem
        }
    }

    private static func directoryNames(_ fd: Int32) throws -> [String] {
        let duplicate = dup(fd)
        guard duplicate >= 0 else { throw EpicDownloadError.filesystem }
        guard let directory = fdopendir(duplicate) else { close(duplicate); throw EpicDownloadError.filesystem }
        defer { closedir(directory) }
        var names: [String] = []
        while let entry = readdir(directory) {
            let name = withUnsafePointer(to: &entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
            }
            if name != ".", name != ".." { names.append(name) }
        }
        return names
    }
}
