// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Darwin

/// A Files authorization is always resolved from its bookmark. The saved mount
/// target records ownership intent only, never an authorization fallback.
struct ExternalGameRoot: Codable, Identifiable {
    var id: UUID
    var name: String
    var bookmark: Data
    var ownedMountTarget: String?
}

final class ExternalGameLease {
    let root: URL
    let libraryID: UUID?
    private let lock = NSLock()
    private var stop: (() -> Void)?
    var isActive: Bool { lock.lock(); defer { lock.unlock() }; return stop != nil }
    init(root: URL, libraryID: UUID? = nil, stop: @escaping () -> Void) { self.root = root; self.libraryID = libraryID; self.stop = stop }
    func release() {
        lock.lock(); let action = stop; stop = nil; lock.unlock()
        action?()
    }
    deinit { release() }
}

/// Immutable authorization and paths captured before deferred JIT/worker work.
final class ExternalGameLaunchContext {
    let rootID: UUID
    let lease: ExternalGameLease
    let executable: URL
    let relativePath: String
    private let mount: URL
    private let retain: (ExternalGameLease) -> Void
    private let lock = NSLock()
    private var finished = false
    init(rootID: UUID, lease: ExternalGameLease, executable: URL, relativePath: String, mount: URL,
         retain: @escaping (ExternalGameLease) -> Void) {
        self.rootID = rootID; self.lease = lease; self.executable = executable
        self.relativePath = relativePath; self.retain = retain
        self.mount = mount
    }
    func validate() throws -> URL {
        guard lease.isActive, lease.root.resolvingSymlinksInPath().path == lease.root.path,
              mount.deletingLastPathComponent().resolvingSymlinksInPath().path == mount.deletingLastPathComponent().path,
              try FileManager.default.destinationOfSymbolicLink(atPath: mount.path) == lease.root.path else {
            throw ExternalGameLibrary.Failure.unavailable
        }
        let url = try ExternalGameLibrary.resolve(relativePath, inside: lease.root, directory: false)
        guard url.path == executable.path else { throw ExternalGameLibrary.Failure.path }
        return url
    }
    func validateWorkingDirectory(_ windowsPath: String?) throws {
        guard let chosen = windowsPath?.trimmingCharacters(in: .whitespaces), !chosen.isEmpty else { return }
        let prefix = "C:\\madeira-external\\" + rootID.uuidString
        guard chosen.lowercased() == prefix.lowercased() || chosen.lowercased().hasPrefix(prefix.lowercased() + "\\") else {
            throw ExternalGameLibrary.Failure.path
        }
        let relative = chosen.count == prefix.count ? "" : String(chosen.dropFirst(prefix.count + 1)).replacingOccurrences(of: "\\", with: "/")
        _ = try ExternalGameLibrary.resolve(relative, inside: lease.root, directory: true)
    }
    /// Only a successful native wine_process_start may retain this scope. Wine
    /// children are detached host threads; retain until app termination.
    func finish(started: Bool) {
        lock.lock(); guard !finished else { lock.unlock(); return }; finished = true; lock.unlock()
        if started { retain(lease) } else { lease.release() }
    }
}

final class ExternalGameLibrary {
    static let shared = ExternalGameLibrary(
        configuration: FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("madeira-external-libraries.json"),
        drive: FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("wine/drive_c", isDirectory: true))
    enum Failure: LocalizedError {
        case unavailable, path, collision, configuration, busy, limit
        var errorDescription: String? {
            switch self {
            case .unavailable: return "The external game folder is unavailable. Reconnect the drive, or reselect its folder in External game library."
            case .path: return "Choose an existing game file or folder inside the selected external library. Its path must be unambiguous on Windows."
            case .collision: return "The external library's Windows mount is occupied by another file or link. Its contents were preserved."
            case .configuration: return "The saved external library configuration is invalid. Its original file was preserved."
            case .busy: return "Wait for the game and launch preparation to stop before changing libraries. A library used by Wine cannot be relinked until Madeira restarts."
            case .limit: return "Up to 64 external game folders are supported."
            }
        }
    }
    private struct Configuration: Codable { var version = 1; var roots: [ExternalGameRoot] }
    private let configuration: URL
    private let drive: URL
    private let startScope: (URL) -> Bool
    private let stopScope: (URL) -> Void
    private let lock = NSRecursiveLock()
    private var registered: [ExternalGameRoot] = []
    private var retained: [UUID: [ExternalGameLease]] = [:]
    private(set) var loadError: String?
    var roots: [ExternalGameRoot] { lock.lock(); defer { lock.unlock() }; return registered }
    init(configuration: URL, drive: URL,
         startScope: @escaping (URL) -> Bool = { $0.startAccessingSecurityScopedResource() },
         stopScope: @escaping (URL) -> Void = { $0.stopAccessingSecurityScopedResource() }) {
        self.configuration = configuration; self.drive = drive
        self.startScope = startScope; self.stopScope = stopScope
        guard FileManager.default.fileExists(atPath: configuration.path) else { return }
        do {
            let handle = try FileHandle(forReadingFrom: configuration); defer { try? handle.close() }
            let data = try handle.read(upToCount: 4 * 1024 * 1024 + 1) ?? Data()
            registered = try Self.decodeRoots(data)
        } catch { loadError = Failure.configuration.localizedDescription }
    }
    static func decodeRoots(_ data: Data) throws -> [ExternalGameRoot] {
        guard data.count <= 4 * 1024 * 1024 else { throw Failure.configuration }
        let saved = try JSONDecoder().decode(Configuration.self, from: data)
        guard saved.version == 1, saved.roots.count <= 64,
              Set(saved.roots.map(\.id)).count == saved.roots.count,
              saved.roots.allSatisfy({ !$0.name.isEmpty && $0.name.utf8.count <= 256 &&
                  !$0.name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) &&
                  !$0.bookmark.isEmpty && $0.bookmark.count <= 64 * 1024 &&
                  ($0.ownedMountTarget == nil || ($0.ownedMountTarget!.hasPrefix("/") &&
                   $0.ownedMountTarget!.utf8.count <= 4096 && !$0.ownedMountTarget!.contains("\0"))) }) else {
            throw Failure.configuration
        }
        return saved.roots
    }
    private func save(_ roots: [ExternalGameRoot]) throws {
        guard loadError == nil else { throw Failure.configuration }
        let data = try JSONEncoder().encode(Configuration(roots: roots))
        _ = try Self.decodeRoots(data)
        try data.write(to: configuration, options: .atomic)
        registered = roots
    }
    private func lease(url: URL, libraryID: UUID? = nil) throws -> ExternalGameLease {
        guard url.isFileURL, startScope(url) else { throw Failure.unavailable }
        let lease = ExternalGameLease(root: url.resolvingSymlinksInPath().standardizedFileURL, libraryID: libraryID) { [stopScope] in stopScope(url) }
        let values = try url.resourceValues(forKeys: [.isDirectoryKey])
        guard values.isDirectory == true else { throw Failure.unavailable }
        return lease
    }
    func acquire(_ id: UUID) throws -> ExternalGameLease {
        lock.lock(); defer { lock.unlock() }
        guard let root = registered.first(where: { $0.id == id }) else { throw Failure.unavailable }
        var stale = false
        do {
            let url = try URL(resolvingBookmarkData: root.bookmark, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
            guard !stale else { throw Failure.unavailable }
            return try lease(url: url, libraryID: id)
        } catch { throw Failure.unavailable }
    }
    @discardableResult
    func register(_ url: URL, relinking id: UUID? = nil, requiredPaths: [String] = [], busy: Bool = false) throws -> ExternalGameRoot {
        lock.lock(); defer { lock.unlock() }
        guard !busy, id.map({ retained[$0] == nil }) ?? true else { throw Failure.busy }
        guard loadError == nil else { throw Failure.configuration }
        guard id != nil || registered.count < 64 else { throw Failure.limit }
        let lease = try lease(url: url); defer { lease.release() }
        for path in requiredPaths { _ = try Self.resolve(path, inside: lease.root, directory: false) }
        let bookmark = try url.bookmarkData(options: [.minimalBookmark], includingResourceValuesForKeys: nil, relativeTo: nil)
        let previous = id.flatMap { value in registered.first { $0.id == value } }
        if id != nil && previous == nil { throw Failure.unavailable }
        var root = ExternalGameRoot(id: previous?.id ?? UUID(), name: String(url.lastPathComponent.prefix(64)),
                                    bookmark: bookmark, ownedMountTarget: previous?.ownedMountTarget)
        func commit() throws {
            var next = registered; next.removeAll { $0.id == root.id }; next.append(root)
            try save(next)
        }
        if let previous, previous.ownedMountTarget != nil {
            // Relink the existing owned alias before committing its bookmark.
            // Any collision leaves both the old bookmark and config unchanged.
            try updateMount(previous, lease: lease, publishAbsent: false) { target in root.ownedMountTarget = target; try commit() }
        } else { try commit() }
        return root
    }
    static func validateRelative(_ relative: String, allowEmpty: Bool = false) throws {
        if allowEmpty && relative.isEmpty { return }
        let parts = relative.split(separator: "/", omittingEmptySubsequences: false)
        let invalid = CharacterSet.controlCharacters.union(CharacterSet(charactersIn: "\\:\"<>|?*"))
        guard relative.utf8.count < 1024, parts.count <= 64,
              !parts.contains(where: { part in
                  part.isEmpty || part == "." || part == ".." || part.hasSuffix(".") || part.hasSuffix(" ") ||
                  part.unicodeScalars.contains(where: { invalid.contains($0) }) ||
                  ["con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"].contains(part.split(separator: ".").first?.lowercased() ?? "")
              }) else { throw Failure.path }
    }
    static func contains(_ file: URL, in root: URL) -> Bool {
        file.path == root.path || file.path.hasPrefix(root.path + "/")
    }
    static func resolve(_ relative: String, inside root: URL, directory: Bool) throws -> URL {
        try validateRelative(relative, allowEmpty: directory)
        let base = root.resolvingSymlinksInPath().standardizedFileURL
        let candidate = base.appendingPathComponent(relative).resolvingSymlinksInPath().standardizedFileURL
        var isDirectory: ObjCBool = false
        guard contains(candidate, in: base), FileManager.default.fileExists(atPath: candidate.path, isDirectory: &isDirectory),
              isDirectory.boolValue == directory else { throw Failure.path }
        if !directory {
            let values = try candidate.resourceValues(forKeys: [.isRegularFileKey])
            guard values.isRegularFile == true, ["exe", "bat", "cmd"].contains(candidate.pathExtension.lowercased()) else { throw Failure.path }
        }
        return candidate
    }
    static func relative(_ file: URL, inside root: URL) throws -> String {
        let base = root.resolvingSymlinksInPath().standardizedFileURL
        let canonical = file.resolvingSymlinksInPath().standardizedFileURL
        guard canonical.path.hasPrefix(base.path + "/") else { throw Failure.path }
        let path = String(canonical.path.dropFirst(base.path.count + 1)); try validateRelative(path); return path
    }
    struct BrowseItem: Identifiable { var relative: String; var name: String; var directory: Bool; var id: String { relative } }
    struct Listing { var items: [BrowseItem]; var truncated: Bool }
    static func browse(_ relative: String, lease: ExternalGameLease, limit: Int = 512) throws -> Listing {
        guard lease.isActive, (1...512).contains(limit) else { throw Failure.unavailable }
        let folder = try resolve(relative, inside: lease.root, directory: true)
        var error: Error?
        guard let iterator = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsSubdirectoryDescendants, .skipsHiddenFiles], errorHandler: { _, failure in error = failure; return false }) else { throw Failure.unavailable }
        var items: [BrowseItem] = [], count = 0, truncated = false
        while let file = iterator.nextObject() as? URL {
            count += 1
            if count > limit { truncated = true; break }
            guard let path = try? Self.relative(file, inside: lease.root),
                  let values = try? file.resourceValues(forKeys: [.isDirectoryKey]),
                  let safe = try? resolve(path, inside: lease.root, directory: values.isDirectory == true) else { continue }
            items.append(BrowseItem(relative: path, name: file.lastPathComponent, directory: safe.hasDirectoryPath || values.isDirectory == true))
        }
        if let error { throw error }
        items.sort { if $0.directory != $1.directory { return $0.directory }; return $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        return Listing(items: items, truncated: truncated)
    }
    static func windowsPath(root: UUID, relative: String) -> String {
        "C:\\madeira-external\\" + root.uuidString + "\\" + relative.replacingOccurrences(of: "/", with: "\\")
    }
    private static func sameInode(_ left: stat, _ right: stat) -> Bool {
        left.st_dev == right.st_dev && left.st_ino == right.st_ino
    }
    /// Bind every operation to directory inodes. A path check followed by rename
    /// could follow a replaced parent or overwrite a newly arrived file.
    private func withMountDirectory<T>(_ body: (Int32, () throws -> Void) throws -> T) throws -> T {
        // Foundation normalizes /private aliases to /var or /tmp on Darwin;
        // realpath supplies the physical components for a no-follow FD walk.
        guard let resolved = realpath(drive.path, nil) else { throw Failure.collision }
        let basePath = String(cString: resolved); free(resolved)
        var driveFD = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard driveFD >= 0 else { throw Failure.collision }
        defer { close(driveFD) }
        for component in basePath.split(separator: "/") {
            let child = openat(driveFD, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard child >= 0 else { throw Failure.collision }
            close(driveFD); driveFD = child
        }
        var baseIdentity = stat()
        guard fstat(driveFD, &baseIdentity) == 0 else { throw Failure.collision }
        let parentName = "madeira-external"
        var parentFD = openat(driveFD, parentName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        if parentFD < 0, errno == ENOENT {
            guard mkdirat(driveFD, parentName, mode_t(0o700)) == 0 || errno == EEXIST else { throw Failure.collision }
            parentFD = openat(driveFD, parentName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        }
        guard parentFD >= 0 else { throw Failure.collision }
        defer { close(parentFD) }
        var parentIdentity = stat()
        guard fstat(parentFD, &parentIdentity) == 0 else { throw Failure.collision }
        func validateParent() throws {
            var currentBase = stat(), currentParent = stat()
            guard lstat(basePath, &currentBase) == 0, (currentBase.st_mode & S_IFMT) == S_IFDIR,
                  Self.sameInode(currentBase, baseIdentity),
                  fstatat(driveFD, parentName, &currentParent, AT_SYMLINK_NOFOLLOW) == 0,
                  (currentParent.st_mode & S_IFMT) == S_IFDIR, Self.sameInode(currentParent, parentIdentity) else {
                throw Failure.collision
            }
        }
        try validateParent()
        return try body(parentFD, validateParent)
    }
    private static func mountEntry(_ name: String, fd: Int32) throws -> (identity: stat, target: String?)? {
        var identity = stat()
        guard fstatat(fd, name, &identity, AT_SYMLINK_NOFOLLOW) == 0 else {
            if errno == ENOENT { return nil }; throw Failure.collision
        }
        guard (identity.st_mode & S_IFMT) == S_IFLNK else { return (identity, nil) }
        var bytes = [Int8](repeating: 0, count: 4097)
        let count = bytes.withUnsafeMutableBufferPointer { readlinkat(fd, name, $0.baseAddress!, $0.count) }
        guard count > 0, count <= 4096,
              let target = String(bytes: bytes.prefix(Int(count)).map { UInt8(bitPattern: $0) }, encoding: .utf8) else {
            throw Failure.collision
        }
        return (identity, target)
    }
    private func updateMount(_ root: ExternalGameRoot, lease: ExternalGameLease, publishAbsent: Bool = true,
                             commit: (String) throws -> Void) throws {
        guard lease.isActive else { throw Failure.unavailable }
        try withMountDirectory { fd, validateParent in
            let name = root.id.uuidString, target = lease.root.path
            let existing = try Self.mountEntry(name, fd: fd)
            if let existing {
                guard let owned = root.ownedMountTarget, existing.target == owned else { throw Failure.collision }
                if owned == target {
                    // Reusing the owned target needs no filesystem mutation.
                    try validateParent()
                    guard let current = try Self.mountEntry(name, fd: fd),
                          Self.sameInode(current.identity, existing.identity), current.target == target else { throw Failure.collision }
                    try commit(target); return
                }
                // Atomic exchange never unlinks an intervening item. Keep the
                // exchanged-out item at this unique name; even a successful
                // relink retains the old link. No path-based/blind cleanup.
                let recovery = ".mount-relink-" + UUID().uuidString
                guard symlinkat(target, fd, recovery) == 0,
                      let incoming = try Self.mountEntry(recovery, fd: fd), incoming.target == target else { throw Failure.collision }
                try validateParent()
                guard renameatx_np(fd, recovery, fd, name, UInt32(RENAME_SWAP)) == 0 else { throw Failure.collision }
                do {
                    try validateParent()
                    guard let previous = try Self.mountEntry(recovery, fd: fd),
                          Self.sameInode(previous.identity, existing.identity), previous.target == owned,
                          let current = try Self.mountEntry(name, fd: fd),
                          Self.sameInode(current.identity, incoming.identity), current.target == target else { throw Failure.collision }
                    try commit(target)
                } catch {
                    // Recovery exchanges the two slots; it cannot overwrite or
                    // delete a writer's newer item. If one slot has disappeared,
                    // SWAP fails without mutation and surviving items stay put.
                    _ = renameatx_np(fd, recovery, fd, name, UInt32(RENAME_SWAP))
                    throw error
                }
            } else {
                try validateParent()
                if !publishAbsent {
                    // Relink commits the new bookmark without publishing an
                    // absent alias. Play will resolve that bookmark and persist
                    // its authorized target before exclusive creation.
                    try commit(root.ownedMountTarget ?? target); return
                }
                // Persist ownership intent for this already registered and
                // granted root first. A save failure publishes nothing; a later
                // publication failure leaves a reusable intent, never a grant
                // fallback and never permission to overwrite a collided item.
                try commit(target)
                try validateParent()
                // symlinkat is an atomic exclusive creation: EEXIST preserves
                // the destination, including a file arriving at this boundary.
                guard symlinkat(target, fd, name) == 0 else { throw Failure.collision }
                try validateParent()
                guard let current = try Self.mountEntry(name, fd: fd), current.target == target else { throw Failure.collision }
            }
        }
    }
    private func prepareMount(_ id: UUID, lease: ExternalGameLease) throws {
        lock.lock(); defer { lock.unlock() }
        guard let index = registered.firstIndex(where: { $0.id == id }) else { throw Failure.unavailable }
        let root = registered[index]
        try updateMount(root, lease: lease) { target in
            if root.ownedMountTarget != target {
                var next = registered; next[index].ownedMountTarget = target; try save(next)
            }
        }
    }
    func prepare(_ rootID: UUID, relative: String) throws -> ExternalGameLaunchContext {
        let lease = try acquire(rootID)
        do {
            let file = try Self.resolve(relative, inside: lease.root, directory: false)
            try prepareMount(rootID, lease: lease)
            let mount = drive.resolvingSymlinksInPath().appendingPathComponent("madeira-external/" + rootID.uuidString)
            return ExternalGameLaunchContext(rootID: rootID, lease: lease, executable: file, relativePath: relative, mount: mount) { [self] lease in
                lock.lock(); retained[rootID, default: []].append(lease); lock.unlock()
            }
        } catch { lease.release(); throw error }
    }
}
