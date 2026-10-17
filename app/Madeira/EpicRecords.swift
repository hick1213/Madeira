// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Darwin

/// Epic metadata is relative to an owned directory FD. Neither a replacement
/// Epic directory nor a linked record can redirect reads/writes into Steam.
final class EpicRecordStore {
    enum Failure: Error {
        case storage, invalidName, unsafePath
        case directoryOperation(step: String, code: Int32)

        var diagnostic: String {
            switch self {
            case .storage: return "storage"
            case .invalidName: return "invalid-name"
            case .unsafePath: return "unsafe-path"
            case let .directoryOperation(step, code): return "step=\(step) errno=\(code)"
            }
        }
    }
    private let documentsFD: Int32
    private let directoryFD: Int32
    private let identity: stat

    init(documents: URL, trustedContainer: URL = EpicDirectory.applicationContainer) throws {
        let root = try EpicDirectory.openDirectory(at: documents, trustedContainer: trustedContainer, create: false)
        if mkdirat(root, "epic", 0o700) != 0 && errno != EEXIST {
            let code = errno
            close(root)
            throw Failure.directoryOperation(step: "create-epic", code: code)
        }
        let directory = openat(root, "epic", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else {
            let code = errno
            close(root)
            throw Failure.directoryOperation(step: "open-epic", code: code)
        }
        var info = stat()
        guard fstat(directory, &info) == 0 else { close(root); close(directory); throw Failure.storage }
        documentsFD = root; directoryFD = directory; identity = info
    }
    deinit { close(directoryFD); close(documentsFD) }

    private func validate(_ name: String) throws {
        guard !name.isEmpty, name.utf8.count <= 120, name != ".", name != "..",
              name.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || [45,46,95].contains($0) }) else { throw Failure.invalidName }
        var info = stat()
        guard fstatat(documentsFD, "epic", &info, AT_SYMLINK_NOFOLLOW) == 0,
              info.st_dev == identity.st_dev, info.st_ino == identity.st_ino,
              info.st_mode & S_IFMT == S_IFDIR else { throw Failure.unsafePath }
    }
    func read(name: String) throws -> Data? {
        try validate(name)
        let fd = openat(directoryFD, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        if fd < 0 { if errno == ENOENT { return nil }; throw Failure.unsafePath }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
              info.st_size >= 0, info.st_size <= 16 * 1024 * 1024 else { throw Failure.storage }
        var result = Data(), buffer = [UInt8](repeating: 0, count: 8192)
        while true {
            let count = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if count < 0 { if errno == EINTR { continue }; throw Failure.storage }
            if count == 0 { break }
            guard result.count + count <= 16 * 1024 * 1024 else { throw Failure.storage }
            result.append(contentsOf: buffer.prefix(count))
        }
        return result
    }
    func write(_ data: Data, name: String) throws {
        try validate(name)
        guard data.count <= 16 * 1024 * 1024 else { throw Failure.storage }
        var target = stat()
        let present = fstatat(directoryFD, name, &target, AT_SYMLINK_NOFOLLOW)
        guard present == 0 ? (target.st_mode & S_IFMT == S_IFREG && target.st_nlink == 1) : errno == ENOENT else { throw Failure.unsafePath }
        let temporary = ".tmp-" + UUID().uuidString
        let fd = openat(directoryFD, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw Failure.storage }
        defer { close(fd); unlinkat(directoryFD, temporary, 0) }
        try data.withUnsafeBytes { bytes in
            var done = 0
            while done < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: done), bytes.count - done)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw Failure.storage }
                done += count
            }
        }
        guard fsync(fd) == 0 else { throw Failure.storage }
        try validate(name)
        guard renameat(directoryFD, temporary, directoryFD, name) == 0 else { throw Failure.storage }
    }
}

/// A short-lived ownership credential readable by the launched Windows game.
/// It is never stored in metadata, arguments, logs or a shared temporary folder.
final class EpicOwnershipTokenFile {
    enum Failure: Error { case storage, unsafePath, invalidPayload }
    static let directoryName = ".madeira-epic-ownership"
    private let directory: URL
    private let container: URL
    private let directoryFD: Int32
    private let directoryIdentity: stat
    private let fileIdentity: stat
    private let name: String
    private var removed = false
    var windowsPath: String { "C:\\" + Self.directoryName + "\\" + name }

    private static func directory(documents: URL) -> URL {
        documents.appendingPathComponent("wine/drive_c", isDirectory: true)
            .appendingPathComponent(directoryName, isDirectory: true)
    }

    init(data: Data, documents: URL, trustedContainer: URL = EpicDirectory.applicationContainer) throws {
        guard !data.isEmpty, data.count <= 64 * 1024 else { throw Failure.invalidPayload }
        let directory = Self.directory(documents: documents)
        let parent = try EpicDirectory.openDirectory(at: directory, trustedContainer: trustedContainer, create: true)
        let name = UUID().uuidString.lowercased() + ".ovt"
        let fd = openat(parent, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { close(parent); throw Failure.storage }
        var success = false
        defer {
            close(fd)
            if !success { unlinkat(parent, name, 0); close(parent) }
        }
        var parentInfo = stat(), info = stat()
        guard fstat(parent, &parentInfo) == 0, fstat(fd, &info) == 0,
              info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
              fchmod(fd, 0o600) == 0 else { throw Failure.storage }
        // Mark the new, still-empty file before writing any credential bytes.
        // A crash may leave it until the next app start; it must not enter backup.
        var fileURL = directory.appendingPathComponent(name)
        var resources = URLResourceValues(); resources.isExcludedFromBackup = true
        try fileURL.setResourceValues(resources)
        try data.withUnsafeBytes { bytes in
            var written = 0
            while written < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: written), bytes.count - written)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw Failure.storage }
                written += count
            }
        }
        guard fsync(fd) == 0 else { throw Failure.storage }
        self.directory = directory; container = trustedContainer; directoryFD = parent
        directoryIdentity = parentInfo; fileIdentity = info; self.name = name
        success = true
    }

    /// Re-open the complete path without following links before passing it to Wine.
    func validate() throws {
        guard !removed else { throw Failure.unsafePath }
        let current = try EpicDirectory.openDirectory(at: directory, trustedContainer: container, create: false)
        defer { close(current) }
        var parentInfo = stat(), info = stat()
        guard fstat(current, &parentInfo) == 0,
              parentInfo.st_dev == directoryIdentity.st_dev, parentInfo.st_ino == directoryIdentity.st_ino,
              fstatat(current, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
              info.st_dev == fileIdentity.st_dev, info.st_ino == fileIdentity.st_ino,
              info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1 else { throw Failure.unsafePath }
    }

    func remove() {
        guard !removed else { return }
        removed = true
        var info = stat()
        if fstatat(directoryFD, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
           info.st_dev == fileIdentity.st_dev, info.st_ino == fileIdentity.st_ino {
            unlinkat(directoryFD, name, 0)
        }
    }
    deinit { remove(); close(directoryFD) }

    /// Called once when the Epic model starts, before it can create a token.
    /// An app crash cannot run deinit. Only our UUID-named regular files in this
    /// dedicated directory are removed; links and unrelated names are preserved.
    static func removeStale(documents: URL, trustedContainer: URL = EpicDirectory.applicationContainer) throws {
        let directory = Self.directory(documents: documents)
        let fd: Int32
        do { fd = try EpicDirectory.openDirectory(at: directory, trustedContainer: trustedContainer, create: false) }
        catch EpicDirectory.Failure.operation(_, let code) where code == ENOENT { return }
        defer { close(fd) }
        let copy = dup(fd)
        guard copy >= 0 else { throw Failure.storage }
        guard let stream = fdopendir(copy) else { close(copy); throw Failure.storage }
        defer { closedir(stream) }
        while let entry = readdir(stream) {
            let name = withUnsafePointer(to: &entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: 1024) { String(cString: $0) }
            }
            guard name.hasSuffix(".ovt"), name.count == 40,
                  UUID(uuidString: String(name.dropLast(4))) != nil else { continue }
            var info = stat()
            guard fstatat(fd, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
                  info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
                  info.st_uid == geteuid(), info.st_mode & 0o777 == 0o600 else { continue }
            guard unlinkat(fd, name, 0) == 0 else { throw Failure.storage }
        }
    }
}
