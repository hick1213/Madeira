// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import CryptoKit
import Darwin

/// A bounded save directory snapshot. All local access is relative to the
/// OS-provided Documents capability; an archive never supplies a destination.
enum LocalSaveSnapshot {
    static let maxFiles = 1_024
    static let maxFileBytes = 16 * 1_024 * 1_024
    static let maxTotalBytes = 64 * 1_024 * 1_024
    static let maxArchiveBytes = 96 * 1_024 * 1_024
    private static let backupDirectory = "iCloud Save Backups"

    struct File: Codable, Sendable, Equatable {
        let path: String
        let data: Data
        let sha256: String
    }

    struct Archive: Codable, Sendable {
        let version: Int
        let id: UUID
        let gameID: UUID
        let title: String
        let created: Date
        let files: [File]

        /// Content and Windows-equivalent paths only; IDs and dates are not content.
        var fingerprint: String { LocalSaveSnapshot.fingerprint(files) }
    }

    struct Receipt: Sendable {
        let backupURL: URL
        let changedFiles: Int
    }

    enum Failure: Error, LocalizedError {
        case unsafePath, invalidArchive, changed, cancelled
        case filesystem(Int32), unsupportedSwap, recoveryNeeded(URL)

        var errorDescription: String? {
            switch self {
            case .unsafePath: "Choose a dedicated game save folder without links or account files."
            case .invalidArchive: "The save backup is invalid, unsafe, or exceeds the supported limits."
            case .changed: "The save folder changed. Review the files and confirm again."
            case .cancelled: "Save synchronization was cancelled."
            case .filesystem: "The save files could not be read or written safely."
            case .unsupportedSwap: "This location does not support atomic save restoration. The original saves were kept."
            case .recoveryNeeded: "The save folder changed during replacement. Both trees and the original backup were kept for recovery."
            }
        }
    }

    static func validateDirectory(documents: URL, relativeDirectory: String) throws {
        let pinned = try PinnedDirectory(documents: documents, relative: relativeDirectory)
        try pinned.revalidate()
    }

    /// Use the same Windows/Unicode comparison for UI overwrite counts.
    static func pathKey(_ path: String) -> String { key(path) }

    static func capture(documents: URL, relativeDirectory: String, gameID: UUID, title: String) throws -> Archive {
        let pinned = try PinnedDirectory(documents: documents, relative: relativeDirectory)
        let tree = try captureStable(pinned)
        let archive = Archive(version: 1, id: UUID(), gameID: gameID, title: title, created: Date(), files: tree.files)
        try validate(archive)
        return archive
    }

    static func encode(_ archive: Archive) throws -> Data {
        try validate(archive)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let bytes = try encoder.encode(archive)
        guard bytes.count <= maxArchiveBytes else { throw Failure.invalidArchive }
        return bytes
    }

    static func decode(_ data: Data) throws -> Archive {
        guard data.count <= maxArchiveBytes else { throw Failure.invalidArchive }
        do {
            let archive = try JSONDecoder().decode(Archive.self, from: data)
            try validate(archive)
            return archive
        } catch { throw Failure.invalidArchive }
    }

    /// Confirm against a freshly captured local fingerprint. Stage the entire
    /// merged directory and swap it in one filesystem operation, never per file.
    static func restore(_ archive: Archive, documents: URL, relativeDirectory: String,
                        expectedFingerprint: String) throws -> Receipt {
        try validate(archive)
        try checkCancellation()
        let pinned = try PinnedDirectory(documents: documents, relative: relativeDirectory)
        let original = try captureStable(pinned)
        guard fingerprint(original.files) == expectedFingerprint else { throw Failure.changed }
        let merged = try merge(original, incoming: archive.files)
        let localByKey = Dictionary(uniqueKeysWithValues: original.files.map { (key($0.path), $0.sha256) })
        let changedFiles = archive.files.filter { localByKey[key($0.path)] != $0.sha256 }.count
        let originalArchive = Archive(version: 1, id: UUID(), gameID: archive.gameID,
                                      title: archive.title, created: Date(), files: original.files)
        let backupData = try encode(originalArchive)
        try pinned.revalidate()
        try checkCancellation()
        let backupURL = try writeBackup(backupData, pinned: pinned, documents: documents)
        let stageName = ".madeira-save-stage-" + UUID().uuidString
        guard mkdirat(pinned.parent, stageName, 0o700) == 0 else { throw Failure.filesystem(errno) }
        let stage = openat(pinned.parent, stageName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard stage >= 0 else { throw Failure.filesystem(errno) }
        defer { close(stage) }
        let stageIdentity = try identity(stage)
        var cleanupStage = true
        defer {
            if cleanupStage { try? removeOwnedTree(parent: pinned.parent, name: stageName, expected: stageIdentity) }
        }
        try populate(merged, at: stage)
        let staged = try scan(stage)
        guard fingerprint(staged.files) == fingerprint(merged.files), staged.directories.keys.sorted() == [""] + merged.directories.sorted() else {
            throw Failure.changed
        }
        let beforeCommit = try captureStable(pinned)
        guard beforeCommit == original else { throw Failure.changed }
        guard try scan(stage) == staged else { throw Failure.changed }
        try pinned.revalidate()
        try checkCancellation()
        guard renameatx_np(pinned.parent, stageName, pinned.parent, pinned.leaf, UInt32(RENAME_SWAP)) == 0 else {
            let code = errno
            if code == ENOTSUP || code == EINVAL || code == EXDEV { throw Failure.unsupportedSwap }
            throw Failure.filesystem(code)
        }

        // A writer or renamed directory between the last check and the swap must
        // not cause success on a different original tree. Roll back only while
        // both names still identify the two pinned directories.
        do {
            try pinned.revalidateParents()
            try requireIdentity(parent: pinned.parent, name: pinned.leaf, expected: stageIdentity)
            try requireIdentity(parent: pinned.parent, name: stageName, expected: original.rootIdentity)
            guard try scan(pinned.root).matchesAfterRename(original) else { throw Failure.changed }
            guard try scan(stage).matchesAfterRename(staged) else { throw Failure.changed }
        } catch {
            cleanupStage = false
            do {
                try pinned.revalidateParents()
                try requireIdentity(parent: pinned.parent, name: pinned.leaf, expected: stageIdentity)
                try requireIdentity(parent: pinned.parent, name: stageName, expected: original.rootIdentity)
                guard renameatx_np(pinned.parent, stageName, pinned.parent, pinned.leaf, UInt32(RENAME_SWAP)) == 0 else {
                    throw Failure.recoveryNeeded(backupURL)
                }
            } catch { throw Failure.recoveryNeeded(backupURL) }
            cleanupStage = true
            throw Failure.changed
        }
        // The stage now holds the old tree, so cleanup must check its old inode.
        cleanupStage = false
        try? removeOwnedTree(parent: pinned.parent, name: stageName, expected: original.rootIdentity)
        _ = fsync(pinned.parent)
        return Receipt(backupURL: backupURL, changedFiles: changedFiles)
    }

    private static func checkCancellation() throws {
        if Task<Never, Never>.isCancelled { throw Failure.cancelled }
    }

    private static func key(_ path: String) -> String {
        path.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive],
            locale: Locale(identifier: "en_US_POSIX")).precomposedStringWithCanonicalMapping
    }

    private static func components(_ path: String) throws -> [String] {
        guard !path.isEmpty, path.utf8.count <= 4_096 else { throw Failure.unsafePath }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard parts.count <= 32 else { throw Failure.unsafePath }
        for part in parts {
            guard !part.isEmpty, part != ".", part != "..", part.utf8.count <= 255,
                  !part.contains(where: { "\\:<>\"|?*".contains($0) }),
                  !part.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
                  !part.hasSuffix("."), !part.hasSuffix(" ") else { throw Failure.unsafePath }
            let base = key(part).split(separator: ".", omittingEmptySubsequences: false).first.map(String.init) ?? ""
            let devices = ["con", "prn", "aux", "nul"] + (1...9).flatMap { ["com\($0)", "lpt\($0)"] }
            guard !devices.contains(base) else { throw Failure.unsafePath }
        }
        return parts
    }

    private static func credential(_ component: String) -> Bool {
        let name = key(component)
        if ["local.vdf", "loginusers.vdf", "cookies", "cookies-journal"].contains(name) { return true }
        return name.hasPrefix("ssfn") && name.count > 4 && name.dropFirst(4).allSatisfy { $0.isASCII && $0.isNumber }
    }

    private static func configuredParts(_ path: String) throws -> [String] {
        let parts = try components(path)
        let lower = parts.map(key)
        guard !["epic", "steam", key(backupDirectory)].contains(lower[0]),
              !parts.contains(where: credential) else { throw Failure.unsafePath }
        if lower.count >= 2, lower[0] == "wine", lower[1] == "drive_c" {
            guard lower.count > 2, lower[2] != "windows",
                  !(lower.count == 3 && ["users", "programdata", "program files", "program files (x86)"].contains(lower[2])) else {
                throw Failure.unsafePath
            }
            if lower[2] == "users" {
                guard lower.count >= 6 else { throw Failure.unsafePath }
                if ["documents", "saved games"].contains(lower[4]) {
                    guard lower.count >= 6 else { throw Failure.unsafePath }
                } else if lower[4] == "appdata" {
                    guard lower.count >= 7, ["local", "locallow", "roaming"].contains(lower[5]),
                          !["steam", "epicgameslauncher", "madeira", "htmlcache"].contains(lower[6]) else {
                        throw Failure.unsafePath
                    }
                } else { throw Failure.unsafePath }
            }
        }
        return parts
    }

    private static func validate(_ archive: Archive) throws {
        guard archive.version == 1, archive.title.utf8.count <= 256,
              !archive.title.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              archive.created.timeIntervalSinceReferenceDate.isFinite, archive.files.count <= maxFiles else {
            throw Failure.invalidArchive
        }
        var total = 0
        var files: Set<String> = []
        var directories: [String: String] = [:]
        for file in archive.files {
            let parts = try components(file.path)
            guard !parts.contains(where: credential), file.data.count <= maxFileBytes,
                  file.sha256 == digest(file.data) else { throw Failure.invalidArchive }
            total += file.data.count
            guard total <= maxTotalBytes else { throw Failure.invalidArchive }
            let fileKey = key(file.path)
            guard files.insert(fileKey).inserted else { throw Failure.invalidArchive }
            for count in 1..<parts.count {
                let name = parts.prefix(count).joined(separator: "/")
                let canonical = key(name)
                if let prior = directories[canonical], prior != name { throw Failure.invalidArchive }
                directories[canonical] = name
            }
        }
        guard directories.count < 4_096, files.isDisjoint(with: Set(directories.keys)) else { throw Failure.invalidArchive }
    }

    private static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func fingerprint(_ files: [File]) -> String {
        var hash = SHA256()
        hash.update(data: Data("MadeiraLocalSaveSnapshot/v1\0".utf8))
        for file in files.sorted(by: { key($0.path).utf8.lexicographicallyPrecedes(key($1.path).utf8) }) {
            let path = Data(key(file.path).utf8)
            for number in [UInt64(path.count), UInt64(file.data.count)] {
                var bigEndian = number.bigEndian
                withUnsafeBytes(of: &bigEndian) { hash.update(data: Data($0)) }
            }
            hash.update(data: path)
            hash.update(data: Data(SHA256.hash(data: file.data)))
        }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private struct Identity: Equatable {
        let device: Int32
        let inode: UInt64
        init(_ status: stat) { device = status.st_dev; inode = status.st_ino }
    }

    private struct Signature: Equatable {
        let identity: Identity
        let mode: UInt16
        let links: UInt16
        let size: Int64
        let modifiedSeconds: Int64
        let modifiedNanoseconds: Int64
        let changedSeconds: Int64
        let changedNanoseconds: Int64
        init(_ status: stat) {
            identity = Identity(status); mode = status.st_mode; links = status.st_nlink; size = status.st_size
            modifiedSeconds = Int64(status.st_mtimespec.tv_sec); modifiedNanoseconds = Int64(status.st_mtimespec.tv_nsec)
            changedSeconds = Int64(status.st_ctimespec.tv_sec); changedNanoseconds = Int64(status.st_ctimespec.tv_nsec)
        }
    }

    private static func status(_ fd: Int32) throws -> stat {
        var value = stat()
        guard fstat(fd, &value) == 0 else { throw Failure.filesystem(errno) }
        return value
    }

    private static func identity(_ fd: Int32) throws -> Identity { Identity(try status(fd)) }

    private static func requireIdentity(parent: Int32, name: String, expected: Identity) throws {
        var value = stat()
        guard fstatat(parent, name, &value, AT_SYMLINK_NOFOLLOW) == 0,
              value.st_mode & S_IFMT == S_IFDIR, Identity(value) == expected else { throw Failure.changed }
    }

    private final class PinnedDirectory {
        let documentsPath: String
        let documents: Int32
        let parent: Int32
        let leaf: String
        let root: Int32
        let documentsIdentity: Identity
        let nodes: [(parent: Int32, name: String, identity: Identity)]
        let descriptors: [Int32]

        init(documents url: URL, relative: String) throws {
            let parts = try configuredParts(relative)
            var path = url.path(percentEncoded: false)
            guard url.isFileURL, path.hasPrefix("/"), !path.contains("\0"),
                  !path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." }) else { throw Failure.unsafePath }
            if path == "/var" || path.hasPrefix("/var/") { path = "/private" + path }
            let documentFD = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard documentFD >= 0 else { throw Failure.filesystem(errno) }
            var opened = [documentFD]
            do {
                let documentStatus = try status(documentFD)
                guard documentStatus.st_mode & S_IFMT == S_IFDIR else { throw Failure.unsafePath }
                var current = documentFD
                var pinned: [(Int32, String, Identity)] = []
                for part in parts {
                    let next = openat(current, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                    guard next >= 0 else { throw Failure.filesystem(errno) }
                    opened.append(next)
                    pinned.append((current, part, try identity(next)))
                    current = next
                }
                var currentDocuments = stat()
                guard lstat(path, &currentDocuments) == 0,
                      Identity(currentDocuments) == Identity(documentStatus), currentDocuments.st_mode & S_IFMT == S_IFDIR else {
                    throw Failure.changed
                }
                for node in pinned {
                    try requireIdentity(parent: node.0, name: node.1, expected: node.2)
                }
                documentsPath = path; documents = documentFD; documentsIdentity = Identity(documentStatus)
                parent = opened[opened.count - 2]; leaf = parts.last!; root = current
                nodes = pinned; descriptors = opened
            } catch {
                opened.forEach { close($0) }
                throw error
            }
        }

        deinit { descriptors.forEach { close($0) } }

        func revalidateParents() throws {
            var value = stat()
            guard lstat(documentsPath, &value) == 0, value.st_mode & S_IFMT == S_IFDIR,
                  Identity(value) == documentsIdentity else { throw Failure.changed }
            for node in nodes.dropLast() {
                try requireIdentity(parent: node.parent, name: node.name, expected: node.identity)
            }
        }

        func revalidate() throws {
            try revalidateParents()
            try requireIdentity(parent: parent, name: leaf, expected: nodes.last!.identity)
        }
    }

    private struct Tree: Equatable {
        let files: [File]
        let directories: [String: Signature]
        var rootIdentity: Identity { directories[""]!.identity }

        func matchesAfterRename(_ prior: Tree) -> Bool {
            guard files == prior.files, directories.keys.sorted() == prior.directories.keys.sorted() else { return false }
            for (path, current) in directories {
                guard let old = prior.directories[path] else { return false }
                if path.isEmpty {
                    // rename changes ctime on Darwin, while inode, contents and
                    // modification time must still identify the original tree.
                    guard current.identity == old.identity, current.mode == old.mode,
                          current.links == old.links, current.size == old.size,
                          current.modifiedSeconds == old.modifiedSeconds,
                          current.modifiedNanoseconds == old.modifiedNanoseconds else { return false }
                } else if current != old { return false }
            }
            return true
        }
    }

    private static func captureStable(_ pinned: PinnedDirectory) throws -> Tree {
        try pinned.revalidate()
        let first = try scan(pinned.root)
        try pinned.revalidate()
        let second = try scan(pinned.root)
        try pinned.revalidate()
        guard first == second else { throw Failure.changed }
        return first
    }

    private static func directoryNames(_ fd: Int32) throws -> [String] {
        let duplicate = fcntl(fd, F_DUPFD_CLOEXEC, 0)
        guard duplicate >= 0 else { throw Failure.filesystem(errno) }
        guard let directory = fdopendir(duplicate) else {
            close(duplicate)
            throw Failure.filesystem(errno)
        }
        defer { closedir(directory) }
        rewinddir(directory)
        var names: [String] = []
        while true {
            errno = 0
            guard let entry = readdir(directory) else {
                guard errno == 0 else { throw Failure.filesystem(errno) }
                break
            }
            let name = withUnsafePointer(to: &entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(validatingCString: $0) }
            }
            guard let name else { throw Failure.unsafePath }
            if name != ".", name != ".." { names.append(name) }
            guard names.count <= 4_096 else { throw Failure.invalidArchive }
        }
        return names.sorted()
    }

    private static func readFile(parent: Int32, name: String, initial: stat) throws -> Data {
        guard initial.st_mode & S_IFMT == S_IFREG, initial.st_nlink == 1,
              initial.st_size >= 0, initial.st_size <= maxFileBytes else { throw Failure.unsafePath }
        let fd = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard fd >= 0 else { throw Failure.filesystem(errno) }
        defer { close(fd) }
        guard Signature(try status(fd)) == Signature(initial) else { throw Failure.changed }
        func readOnce() throws -> Data {
            guard lseek(fd, 0, SEEK_SET) == 0 else { throw Failure.filesystem(errno) }
            var data = Data(count: Int(initial.st_size))
            try data.withUnsafeMutableBytes { buffer in
                var offset = 0
                while offset < buffer.count {
                    try checkCancellation()
                    let count = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                    if count < 0, errno == EINTR { continue }
                    guard count > 0 else { throw Failure.changed }
                    offset += count
                }
            }
            var extra: UInt8 = 0
            var count = Darwin.read(fd, &extra, 1)
            while count < 0, errno == EINTR { count = Darwin.read(fd, &extra, 1) }
            guard count == 0, Signature(try status(fd)) == Signature(initial) else { throw Failure.changed }
            return data
        }
        let first = try readOnce()
        guard first == (try readOnce()) else { throw Failure.changed }
        var named = stat()
        guard fstatat(parent, name, &named, AT_SYMLINK_NOFOLLOW) == 0,
              Signature(named) == Signature(initial) else { throw Failure.changed }
        return first
    }

    private static func scan(_ root: Int32) throws -> Tree {
        var files: [File] = []
        var directories: [String: Signature] = [:]
        var total = 0
        var entries = 0
        var spellings: [String: String] = [:]
        func walk(_ fd: Int32, path: String) throws {
            try checkCancellation()
            let before = try status(fd)
            guard before.st_mode & S_IFMT == S_IFDIR else { throw Failure.unsafePath }
            directories[path] = Signature(before)
            guard directories.count <= 4_096 else { throw Failure.invalidArchive }
            for name in try directoryNames(fd) {
                let relative = path.isEmpty ? name : path + "/" + name
                let parts = try components(relative)
                guard !parts.contains(where: credential) else { throw Failure.unsafePath }
                let canonical = key(relative)
                guard spellings.updateValue(relative, forKey: canonical) == nil else { throw Failure.unsafePath }
                entries += 1
                guard entries <= 8_192 else { throw Failure.invalidArchive }
                var named = stat()
                guard fstatat(fd, name, &named, AT_SYMLINK_NOFOLLOW) == 0 else { throw Failure.filesystem(errno) }
                switch named.st_mode & S_IFMT {
                case S_IFDIR:
                    let child = openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                    guard child >= 0 else { throw Failure.filesystem(errno) }
                    defer { close(child) }
                    guard Signature(try status(child)) == Signature(named) else { throw Failure.changed }
                    try walk(child, path: relative)
                    var after = stat()
                    guard fstatat(fd, name, &after, AT_SYMLINK_NOFOLLOW) == 0,
                          Signature(after) == Signature(named) else { throw Failure.changed }
                case S_IFREG:
                    guard files.count < maxFiles else { throw Failure.invalidArchive }
                    let data = try readFile(parent: fd, name: name, initial: named)
                    total += data.count
                    guard total <= maxTotalBytes else { throw Failure.invalidArchive }
                    files.append(File(path: relative, data: data, sha256: digest(data)))
                default: throw Failure.unsafePath
                }
            }
            guard Signature(try status(fd)) == Signature(before) else { throw Failure.changed }
        }
        try walk(root, path: "")
        return Tree(files: files.sorted { key($0.path) < key($1.path) }, directories: directories)
    }

    private struct Merged {
        let files: [File]
        let directories: [String]
    }

    private static func merge(_ original: Tree, incoming: [File]) throws -> Merged {
        var files = Dictionary(uniqueKeysWithValues: original.files.map { (key($0.path), $0) })
        var directorySpellings = Dictionary(uniqueKeysWithValues: original.directories.keys.filter { !$0.isEmpty }.map { (key($0), $0) })
        for file in incoming {
            var chosen: [String] = []
            let parts = try components(file.path)
            for count in 1..<parts.count {
                let canonical = key(parts.prefix(count).joined(separator: "/"))
                let proposal = (chosen + [parts[count - 1]]).joined(separator: "/")
                let spelling = directorySpellings[canonical] ?? proposal
                directorySpellings[canonical] = spelling
                chosen = spelling.split(separator: "/").map(String.init)
            }
            let existing = files[key(file.path)]
            let path = existing?.path ?? (chosen + [parts.last!]).joined(separator: "/")
            files[key(file.path)] = File(path: path, data: file.data, sha256: file.sha256)
        }
        let result = files.values.sorted { key($0.path) < key($1.path) }
        try validate(Archive(version: 1, id: UUID(), gameID: UUID(), title: "", created: Date(), files: result))
        guard Set(files.keys).isDisjoint(with: Set(directorySpellings.keys)) else { throw Failure.invalidArchive }
        return Merged(files: result, directories: directorySpellings.values.sorted())
    }

    private static func write(_ data: Data, to fd: Int32) throws {
        try data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                try checkCancellation()
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { throw Failure.filesystem(errno) }
                offset += count
            }
        }
        guard fsync(fd) == 0 else { throw Failure.filesystem(errno) }
    }

    private static func writeBackup(_ data: Data, pinned: PinnedDirectory, documents: URL) throws -> URL {
        if mkdirat(pinned.documents, backupDirectory, 0o700) != 0, errno != EEXIST { throw Failure.filesystem(errno) }
        let folder = openat(pinned.documents, backupDirectory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard folder >= 0 else { throw Failure.filesystem(errno) }
        defer { close(folder) }
        let folderIdentity = try identity(folder)
        let name = UUID().uuidString + ".madeirasaves"
        let fd = openat(folder, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw Failure.filesystem(errno) }
        var completed = false
        defer { close(fd); if !completed { _ = unlinkat(folder, name, 0) } }
        try write(data, to: fd)
        try pinned.revalidate()
        try requireIdentity(parent: pinned.documents, name: backupDirectory, expected: folderIdentity)
        guard fsync(folder) == 0 else { throw Failure.filesystem(errno) }
        completed = true
        return documents.appendingPathComponent(backupDirectory, isDirectory: true).appendingPathComponent(name)
    }

    private static func populate(_ merged: Merged, at root: Int32) throws {
        func parent(for parts: [String]) throws -> Int32 {
            var fd = fcntl(root, F_DUPFD_CLOEXEC, 0)
            guard fd >= 0 else { throw Failure.filesystem(errno) }
            do {
                for part in parts {
                    if mkdirat(fd, part, 0o700) != 0, errno != EEXIST { throw Failure.filesystem(errno) }
                    let next = openat(fd, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                    guard next >= 0 else { throw Failure.filesystem(errno) }
                    close(fd); fd = next
                }
                return fd
            } catch { close(fd); throw error }
        }
        for path in merged.directories {
            let fd = try parent(for: components(path))
            guard fsync(fd) == 0 else { close(fd); throw Failure.filesystem(errno) }
            close(fd)
        }
        for file in merged.files {
            try checkCancellation()
            let parts = try components(file.path)
            let directory = try parent(for: Array(parts.dropLast()))
            defer { close(directory) }
            let fd = openat(directory, parts.last!, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fd >= 0 else { throw Failure.filesystem(errno) }
            defer { close(fd) }
            try write(file.data, to: fd)
            guard fsync(directory) == 0 else { throw Failure.filesystem(errno) }
        }
        guard fsync(root) == 0 else { throw Failure.filesystem(errno) }
    }

    /// Cleanup only a task-created or successfully swapped, pinned directory.
    /// Never follows a link, including links introduced after staging.
    private static func removeOwnedTree(parent: Int32, name: String, expected: Identity) throws {
        try requireIdentity(parent: parent, name: name, expected: expected)
        let fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw Failure.filesystem(errno) }
        defer { close(fd) }
        guard try identity(fd) == expected else { throw Failure.changed }
        for child in try directoryNames(fd) {
            var named = stat()
            guard fstatat(fd, child, &named, AT_SYMLINK_NOFOLLOW) == 0 else { throw Failure.filesystem(errno) }
            if named.st_mode & S_IFMT == S_IFDIR {
                try removeOwnedTree(parent: fd, name: child, expected: Identity(named))
            } else {
                guard unlinkat(fd, child, 0) == 0 else { throw Failure.filesystem(errno) }
            }
        }
        try requireIdentity(parent: parent, name: name, expected: expected)
        guard unlinkat(parent, name, AT_REMOVEDIR) == 0 else { throw Failure.filesystem(errno) }
    }
}
