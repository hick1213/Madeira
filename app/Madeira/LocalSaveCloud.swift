// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Combine
import CryptoKit
import Darwin

/// The folder is a capability chosen in Files; no private iCloud container is used.
enum LocalSaveCloudFiles {
    struct Record: Codable, Identifiable, Sendable, Equatable {
        let id: UUID
        let gameID: UUID
        let title: String
        let created: Date
        let files: Int
        let bytes: Int
        let fingerprint: String
        let archiveHash: String
    }
    enum Failure: LocalizedError {
        case folder, invalid, changed, unavailable, limit, configurationLimit, configurationReadOnly
        var errorDescription: String? {
            switch self {
            case .folder: return "Choose an accessible folder in iCloud Drive again."
            case .invalid: return "This save backup is incomplete or invalid. No saves were replaced."
            case .changed: return "The saves or cloud versions changed. Check them again before replacing anything."
            case .unavailable: return "The backup is not available on this device yet. Let Files download it, then try again."
            case .limit: return "There are too many versions in this folder. Choose a dedicated Madeira save folder."
            case .configurationLimit: return "Up to 256 local games can have save folders configured. Existing configurations were kept."
            case .configurationReadOnly: return "Save-folder settings could not be read safely. The original settings were kept."
            }
        }
    }
    static let directoryName = "Madeira Local Saves"
    static let maxArchive = 96 << 20
    static func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    static func isHash(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
    static func valid(_ record: Record) -> Bool {
        !record.title.isEmpty && record.title.utf8.count <= 256 && !record.title.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) &&
        record.created.timeIntervalSince1970.isFinite && (0...1024).contains(record.files) &&
        (1...maxArchive).contains(record.bytes) && isHash(record.fingerprint) && isHash(record.archiveHash)
    }
    static func versionFingerprint(_ records: [Record], gameID: UUID) -> String {
        hash(Data(records.filter { $0.gameID == gameID }.sorted { $0.id.uuidString < $1.id.uuidString }
            .map { "\($0.id.uuidString)|\($0.archiveHash)|\($0.fingerprint)|\($0.created.timeIntervalSince1970)|\($0.files)|\($0.bytes)|\(hash(Data($0.title.utf8)))" }.joined(separator: "\n").utf8))
    }

    /// Each coordinator is used for one operation on one worker thread.
    static func withFolder<T>(bookmark: Data, writing: Bool, _ body: (URL) throws -> T) throws -> T {
        guard !bookmark.isEmpty, bookmark.count <= 64 << 10 else { throw Failure.folder }
        var stale = false
        let folder = try URL(resolvingBookmarkData: bookmark, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
        guard folder.isFileURL, !stale else { throw Failure.folder }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        var result: Result<T, Error>?
        var coordinationError: NSError?
        let coordinator = NSFileCoordinator()
        if writing {
            coordinator.coordinate(writingItemAt: folder, options: [], error: &coordinationError) { actual in
                result = Result { try Task.checkCancellation(); return try body(actual) }
            }
        } else {
            coordinator.coordinate(readingItemAt: folder, options: [], error: &coordinationError) { actual in
                result = Result { try Task.checkCancellation(); return try body(actual) }
            }
        }
        if let coordinationError { throw coordinationError }
        guard let result else { throw Failure.folder }
        return try result.get()
    }

    private static func openFolder(_ folder: URL, create: Bool) throws -> Int32 {
        let rawPath = folder.path(percentEncoded: false)
        guard folder.isFileURL, rawPath.hasPrefix("/"), !rawPath.contains("\0"),
              !rawPath.split(separator: "/").contains(where: { $0 == "." || $0 == ".." }) else { throw Failure.folder }
        let root = open(rawPath, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard root >= 0 else { throw Failure.folder }
        defer { close(root) }
        if create, mkdirat(root, directoryName, 0o700) != 0, errno != EEXIST { throw Failure.folder }
        let fd = openat(root, directoryName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0, !create, errno == ENOENT { return -1 }
        guard fd >= 0 else { throw Failure.folder }
        return fd
    }
    private static func read(_ name: String, in fd: Int32, limit: Int) throws -> Data {
        let file = openat(fd, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard file >= 0 else { throw Failure.unavailable }
        defer { close(file) }
        var before = stat()
        guard fstat(file, &before) == 0, before.st_mode & S_IFMT == S_IFREG, before.st_nlink == 1,
              before.st_size > 0, before.st_size <= limit else { throw Failure.invalid }
        var data = Data(count: Int(before.st_size))
        try data.withUnsafeMutableBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                try Task.checkCancellation()
                let n = Darwin.read(file, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if n < 0, errno == EINTR { continue }
                guard n > 0 else { throw Failure.unavailable }
                offset += n
            }
        }
        var after = stat()
        guard fstat(file, &after) == 0, before.st_dev == after.st_dev, before.st_ino == after.st_ino,
              before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec, before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec, before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else { throw Failure.changed }
        return data
    }
    private static func writeNew(_ data: Data, name: String, in fd: Int32) throws {
        let file = openat(fd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard file >= 0 else { throw Failure.changed }
        var complete = false
        defer { close(file); if !complete { unlinkat(fd, name, 0) } }
        try data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                try Task.checkCancellation()
                let n = Darwin.write(file, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if n < 0, errno == EINTR { continue }
                guard n > 0 else { throw Failure.folder }
                offset += n
            }
        }
        guard fsync(file) == 0 else { throw Failure.folder }
        complete = true
    }

    private static func metadataNames(in fd: Int32, reserving entries: Int = 0) throws -> [String] {
        let copy = fcntl(fd, F_DUPFD_CLOEXEC, 0)
        guard copy >= 0, let directory = fdopendir(copy) else { if copy >= 0 { close(copy) }; throw Failure.folder }
        defer { closedir(directory) }
        var names: [String] = []
        var seen = 0
        errno = 0
        while let entry = readdir(directory) {
            let name = withUnsafePointer(to: &entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
            }
            if name == "." || name == ".." { continue }
            seen += 1; guard seen <= 512 - entries else { throw Failure.limit }
            guard name.hasSuffix(".json"), let id = UUID(uuidString: String(name.dropLast(5))),
                  name == id.uuidString + ".json" else { continue }
            names.append(name)
            errno = 0
        }
        guard errno == 0 else { throw Failure.folder }
        return names
    }
    static func records(in folder: URL) throws -> [Record] {
        let fd = try openFolder(folder, create: false)
        guard fd >= 0 else { return [] }
        defer { close(fd) }
        let names = try metadataNames(in: fd)
        var result: [Record] = []
        for name in names {
            let data = try read(name, in: fd, limit: 8 << 10)
            guard let record = try? JSONDecoder().decode(Record.self, from: data), valid(record),
                  name == record.id.uuidString + ".json" else { throw Failure.invalid }
            result.append(record)
        }
        return result.sorted { $0.created == $1.created ? $0.id.uuidString > $1.id.uuidString : $0.created > $1.created }
    }

    static func load(_ record: Record, from folder: URL) throws -> LocalSaveSnapshot.Archive {
        guard valid(record) else { throw Failure.invalid }
        let fd = try openFolder(folder, create: false)
        guard fd >= 0 else { throw Failure.unavailable }
        defer { close(fd) }
        let metadata = try read(record.id.uuidString + ".json", in: fd, limit: 8 << 10)
        guard let current = try? JSONDecoder().decode(Record.self, from: metadata), current == record else { throw Failure.changed }
        let data = try read(record.id.uuidString + ".madeirasaves", in: fd, limit: maxArchive)
        guard data.count == record.bytes, hash(data) == record.archiveHash else { throw Failure.invalid }
        let archive = try LocalSaveSnapshot.decode(data)
        guard archive.id == record.id, archive.gameID == record.gameID, archive.title == record.title,
              archive.created == record.created, archive.files.count == record.files, archive.fingerprint == record.fingerprint else { throw Failure.invalid }
        return archive
    }

    /// Metadata is hydrated independently so listing versions need not fetch
    /// every save payload. All waiting happens outside coordination accessors.
    static func ensureMetadataCopies(bookmark: Data) async throws {
        let names = try withFolder(bookmark: bookmark, writing: false) { folder -> [String] in
            let fd = try openFolder(folder, create: false)
            guard fd >= 0 else { return [] }
            defer { close(fd) }
            return try metadataNames(in: fd)
        }
        guard !names.isEmpty else { return }
        var stale = false
        let folder = try URL(resolvingBookmarkData: bookmark, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
        guard folder.isFileURL, !stale else { throw Failure.folder }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        let fd = try openFolder(folder, create: false)
        guard fd >= 0 else { throw Failure.unavailable }
        defer { close(fd) }
        let deadline = ContinuousClock.now.advanced(by: .seconds(20))
        for name in names {
            try await ensureReady(folder: folder, fd: fd, name: name, limit: 8 << 10, deadline: deadline)
        }
    }

    /// Ask iCloud to hydrate the chosen payload before taking a coordination
    /// lock. Never wait for its writer from inside a coordinator accessor.
    static func ensureLocalCopy(_ record: Record, bookmark: Data) async throws {
        guard valid(record), !bookmark.isEmpty, bookmark.count <= 64 << 10 else { throw Failure.invalid }
        var stale = false
        let folder = try URL(resolvingBookmarkData: bookmark, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
        guard folder.isFileURL, !stale else { throw Failure.folder }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }
        let fd = try openFolder(folder, create: false)
        guard fd >= 0 else { throw Failure.unavailable }
        defer { close(fd) }
        try await ensureReady(folder: folder, fd: fd, name: record.id.uuidString + ".madeirasaves",
                              limit: maxArchive, deadline: ContinuousClock.now.advanced(by: .seconds(20)))
    }
    private static func ensureReady(folder: URL, fd: Int32, name: String, limit: Int, deadline: ContinuousClock.Instant) async throws {
        try Task.checkCancellation()
        var status = stat()
        guard fstatat(fd, name, &status, AT_SYMLINK_NOFOLLOW) == 0,
              status.st_mode & S_IFMT == S_IFREG, status.st_nlink == 1,
              status.st_size >= 0, status.st_size <= limit else { throw Failure.invalid }
        var payload = folder.appendingPathComponent(directoryName, isDirectory: true).appendingPathComponent(name)
        let keys: Set<URLResourceKey> = [.isUbiquitousItemKey, .ubiquitousItemDownloadingStatusKey]
        let values = try payload.resourceValues(forKeys: keys)
        if values.isUbiquitousItem == true,
           values.ubiquitousItemDownloadingStatus != .current && values.ubiquitousItemDownloadingStatus != .downloaded {
            try FileManager.default.startDownloadingUbiquitousItem(at: payload)
            var downloaded = false
            while ContinuousClock.now < deadline {
                try Task.checkCancellation()
                try await Task.sleep(for: .milliseconds(200))
                payload.removeCachedResourceValue(forKey: .ubiquitousItemDownloadingStatusKey)
                let status = try payload.resourceValues(forKeys: keys).ubiquitousItemDownloadingStatus
                if status == .current || status == .downloaded { downloaded = true; break }
            }
            guard downloaded else { throw Failure.unavailable }
        }
        // Other document providers hydrate a specific coordinated read, not
        // necessarily a read of the containing directory.
        var coordinationError: NSError?
        var outcome: Result<Void, Error>?
        NSFileCoordinator().coordinate(readingItemAt: payload, options: [], error: &coordinationError) { _ in
            outcome = Result {
                try Task.checkCancellation()
                _ = try read(name, in: fd, limit: limit)
            }
        }
        if let coordinationError { throw coordinationError }
        guard let outcome else { throw Failure.unavailable }
        try outcome.get()
    }

    /// Immutable versions avoid competing devices overwriting the same file.
    static func upload(_ archive: LocalSaveSnapshot.Archive, to folder: URL, expectedVersions: String) throws -> URL {
        guard versionFingerprint(try records(in: folder), gameID: archive.gameID) == expectedVersions else { throw Failure.changed }
        let data = try LocalSaveSnapshot.encode(archive)
        let record = Record(id: archive.id, gameID: archive.gameID, title: archive.title, created: archive.created,
            files: archive.files.count, bytes: data.count, fingerprint: archive.fingerprint, archiveHash: hash(data))
        guard valid(record), !archive.files.isEmpty else { throw Failure.invalid }
        let fd = try openFolder(folder, create: true)
        defer { close(fd) }
        _ = try metadataNames(in: fd, reserving: 2)
        try writeNew(data, name: archive.id.uuidString + ".madeirasaves", in: fd)
        // Publish metadata last. An incomplete payload can never be restored.
        try writeNew(try JSONEncoder().encode(record), name: archive.id.uuidString + ".json", in: fd)
        return folder.appendingPathComponent(directoryName, isDirectory: true).appendingPathComponent(archive.id.uuidString + ".madeirasaves")
    }
}

enum LocalSaveDirectoryRules {
    static func relative(windowsPath: String, program: String?) throws -> String {
        var path = windowsPath.replacingOccurrences(of: "\\", with: "/")
        if path.lowercased().hasPrefix("c:/") { path = String(path.dropFirst(3)) }
        guard !path.hasPrefix("/"), !path.contains(":"), !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { throw LocalSaveCloudFiles.Failure.invalid }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." || $0.hasSuffix(".") || $0.hasSuffix(" ") }), parts.count <= 32 else { throw LocalSaveCloudFiles.Failure.invalid }
        let folded = parts.map(LocalSaveSnapshot.pathKey)
        if let appData = folded.firstIndex(of: "appdata"), folded.count > appData + 2,
           ["local", "locallow", "roaming"].contains(folded[appData + 1]),
           ["steam", "epicgameslauncher", "madeira", "htmlcache"].contains(folded[appData + 2]) {
            throw LocalSaveCloudFiles.Failure.invalid
        }
        if folded.contains("epicgameslauncher") { throw LocalSaveCloudFiles.Failure.invalid }
        if folded.count >= 3, ["program files", "program files (x86)"].contains(folded[0]),
           folded[1] == "epic games", folded[2] == "launcher" { throw LocalSaveCloudFiles.Failure.invalid }
        if folded.count >= 2, ["program files", "program files (x86)"].contains(folded[0]), folded[1] == "steam" {
            guard folded.count >= 5, folded[2] == "steamapps", folded[3] == "common" else { throw LocalSaveCloudFiles.Failure.invalid }
        }
        var allowed = false
        if folded.count >= 4, folded[0] == "users" {
            allowed = ["documents", "saved games"].contains(folded[2]) ||
                (folded.count >= 5 && folded[2] == "appdata" && ["local", "locallow", "roaming"].contains(folded[3]))
        }
        let parent = (program ?? "").replacingOccurrences(of: "\\", with: "/").split(separator: "/").dropLast().map { LocalSaveSnapshot.pathKey(String($0)) }
        if !parent.isEmpty, !["windows", "users"].contains(parent[0]),
           !(parent.count == 1 && ["program files", "program files (x86)", "steam", "epic games"].contains(parent[0])),
           folded.starts(with: parent) { allowed = true }
        guard allowed else { throw LibraryError.message("Choose this game's own save folder, inside its installation or a Windows user's Documents, Saved Games or AppData. Whole Windows/user folders cannot be synchronized.") }
        return "wine/drive_c/" + parts.joined(separator: "/")
    }
}

@MainActor final class LocalSaveCloudModel: ObservableObject {
    struct Configuration: Codable, Sendable { var game: UUID; var directory: String; var cloudGame: UUID }
    enum Pending {
        case upload(LocalSaveSnapshot.Archive, Configuration, String)
        case download(LocalSaveSnapshot.Archive, Configuration, LocalSaveCloudFiles.Record, String, Int)
    }
    static let shared = LocalSaveCloudModel()
    @Published private(set) var configurations: [Configuration] = []
    @Published private(set) var records: [LocalSaveCloudFiles.Record] = []
    @Published private(set) var busy = false
    @Published private(set) var folderName: String?
    @Published var message: String?
    @Published private(set) var pending: Pending?
    private var bookmark: Data?
    private var token: UUID?
    private var task: Task<Void, Never>?
    private var configurationReadOnly = false
    private let defaults: UserDefaults
    private static let folderKey = "madeiraLocalSaveCloudFolder"
    private static let configKey = "madeiraLocalSaveCloudConfigurations"
    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let data = defaults.data(forKey: Self.folderKey), data.count <= 64 << 10 { bookmark = data; folderName = "Selected Files folder" }
        if let data = defaults.data(forKey: Self.configKey) {
            if data.count <= 64 << 10,
               let configs = try? JSONDecoder().decode([Configuration].self, from: data), configs.count <= 256,
               Set(configs.map(\.game)).count == configs.count {
                configurations = configs
            } else {
                configurationReadOnly = true
                message = LocalSaveCloudFiles.Failure.configurationReadOnly.localizedDescription
            }
        }
    }
    var hasFolder: Bool { bookmark != nil }
    func configuration(_ game: UUID) -> Configuration? { configurations.first { $0.game == game } }
    private func validateConfiguration(_ config: Configuration, for entry: LibraryEntry) throws {
        guard entry.externalLibraryID != nil else { return }
        let prefix = "wine/drive_c/"
        do {
            guard config.directory.hasPrefix(prefix),
                  try LocalSaveDirectoryRules.relative(windowsPath: String(config.directory.dropFirst(prefix.count)), program: nil) == config.directory else {
                throw LocalSaveCloudFiles.Failure.invalid
            }
        } catch {
            throw LibraryError.message("For this external game, reselect its save folder in Madeira's Windows user profile (Documents, Saved Games or AppData). The existing save files and configuration were preserved.")
        }
    }
    private func saveConfigurations() throws {
        guard !configurationReadOnly else { throw LocalSaveCloudFiles.Failure.configurationReadOnly }
        guard configurations.count <= 256 else { throw LocalSaveCloudFiles.Failure.configurationLimit }
        let data = try JSONEncoder().encode(configurations)
        guard data.count <= 64 << 10 else { throw LocalSaveCloudFiles.Failure.limit }
        defaults.set(data, forKey: Self.configKey)
    }
    func setFolder(_ url: URL) throws {
        guard !busy else { throw GameSessionWork.Failure.saveAlreadyRunning }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard url.isFileURL, (try url.resourceValues(forKeys: [.isDirectoryKey])).isDirectory == true else { throw LocalSaveCloudFiles.Failure.folder }
        let data = try url.bookmarkData(options: [.minimalBookmark], includingResourceValuesForKeys: nil, relativeTo: nil)
        guard data.count <= 64 << 10 else { throw LocalSaveCloudFiles.Failure.folder }
        defaults.set(data, forKey: Self.folderKey); bookmark = data; folderName = url.lastPathComponent; records = []
        message = "Folder selected. Choose iCloud Drive in Files for iCloud synchronization."
    }
    func configure(_ entry: LibraryEntry, windowsPath: String) throws {
        guard !busy, entry.steamAppID == nil, entry.epicAppName == nil, entry.desktop != true else { throw GameSessionWork.Failure.saveAlreadyRunning }
        // An external entry's path is relative to its USB root, so it must never
        // grant permission to the same-named installation folder inside drive_c.
        let relative = try LocalSaveDirectoryRules.relative(windowsPath: windowsPath, program: entry.externalLibraryID == nil ? entry.relativePath : nil)
        try LocalSaveSnapshot.validateDirectory(documents: LibraryModel.documents, relativeDirectory: relative)
        let configuration = Configuration(game: entry.id, directory: relative, cloudGame: configuration(entry.id)?.cloudGame ?? entry.id)
        let old = configurations
        configurations.removeAll { $0.game == entry.id }; configurations.append(configuration)
        do { try saveConfigurations() } catch { configurations = old; throw error }
        message = "Save folder configured. Upload and download are manual."
    }
    private func begin() throws -> (UUID, Data) {
        guard !busy, let bookmark else { throw LocalSaveCloudFiles.Failure.folder }
        let token = try GameSessionWork.shared.beginSaveOperation()
        self.token = token; busy = true; message = nil
        return (token, bookmark)
    }
    private func finish(_ owned: UUID) {
        guard token == owned else { return }
        pending = nil; task = nil; token = nil; busy = false
        GameSessionWork.shared.endSaveOperation(owned)
    }
    private func run<T: Sendable>(_ body: @escaping @Sendable () throws -> T) async throws -> T {
        let worker = Task.detached(priority: .utility) { try body() }
        return try await withTaskCancellationHandler(operation: { try await worker.value }, onCancel: { worker.cancel() })
    }
    private func runAsync<T: Sendable>(_ body: @escaping @Sendable () async throws -> T) async throws -> T {
        let worker = Task.detached(priority: .utility) { try await body() }
        return try await withTaskCancellationHandler(operation: { try await worker.value }, onCancel: { worker.cancel() })
    }
    private func report(_ error: Error) {
        if error is CancellationError { message = "Save synchronization cancelled." }
        else if let error = error as? LocalizedError, let description = error.errorDescription { message = description }
        else { message = "Could not access or synchronize the selected save folder. Check Files and try again." }
    }
    func refresh() {
        do {
            let (owned, bookmark) = try begin()
            task = Task {
                defer { finish(owned) }
                do {
                    try await GameSessionWork.shared.waitForSaveOperation(owned)
                    try await runAsync { try await LocalSaveCloudFiles.ensureMetadataCopies(bookmark: bookmark) }
                    let list = try await run { try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: false) { try LocalSaveCloudFiles.records(in: $0) } }
                    try Task.checkCancellation(); records = list
                    if list.isEmpty { message = "No Madeira save backups in this folder yet." }
                } catch { report(error) }
            }
        } catch { report(error) }
    }
    func prepareUpload(_ entry: LibraryEntry) {
        do {
            guard entry.steamAppID == nil, entry.epicAppName == nil, entry.desktop != true else { throw LibraryError.message("Choose a local game for this save operation.") }
            guard let config = configuration(entry.id) else { throw LibraryError.message("Configure this game's save folder first.") }
            try validateConfiguration(config, for: entry)
            let (owned, bookmark) = try begin()
            let documents = LibraryModel.documents
            let title = entry.title
            task = Task {
                do {
                    try await GameSessionWork.shared.waitForSaveOperation(owned)
                    try await runAsync { try await LocalSaveCloudFiles.ensureMetadataCopies(bookmark: bookmark) }
                    let (archive, list) = try await run {
                        let archive = try LocalSaveSnapshot.capture(documents: documents, relativeDirectory: config.directory, gameID: config.cloudGame, title: title)
                        guard !archive.files.isEmpty else { throw LibraryError.message("There are no saves in the configured folder yet.") }
                        let list = try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: false) { try LocalSaveCloudFiles.records(in: $0) }
                        if let current = list.first(where: { $0.gameID == config.cloudGame }), current.fingerprint == archive.fingerprint {
                            _ = try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: false) { try LocalSaveCloudFiles.load(current, from: $0) }
                        }
                        return (archive, list)
                    }
                    try Task.checkCancellation(); records = list
                    let versions = LocalSaveCloudFiles.versionFingerprint(list, gameID: config.cloudGame)
                    let current = list.first { $0.gameID == config.cloudGame }
                    if current?.fingerprint == archive.fingerprint { message = "The current cloud version already has these saves."; finish(owned); return }
                    pending = .upload(archive, config, versions); task = nil
                    // The first upload also presents its scope before writing.
                } catch { report(error); finish(owned) }
            }
        } catch { report(error) }
    }
    func prepareDownload(_ record: LocalSaveCloudFiles.Record, into entry: LibraryEntry) {
        do {
            guard entry.steamAppID == nil, entry.epicAppName == nil, entry.desktop != true else { throw LibraryError.message("Choose a local game for this save operation.") }
            guard let config = configuration(entry.id) else { throw LibraryError.message("Configure this game's save folder first.") }
            try validateConfiguration(config, for: entry)
            let (owned, bookmark) = try begin()
            let documents = LibraryModel.documents
            let title = entry.title
            task = Task {
                do {
                    try await GameSessionWork.shared.waitForSaveOperation(owned)
                    try await runAsync { try await LocalSaveCloudFiles.ensureLocalCopy(record, bookmark: bookmark) }
                    let (archive, local) = try await run {
                        let archive = try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: false) { try LocalSaveCloudFiles.load(record, from: $0) }
                        let local = try LocalSaveSnapshot.capture(documents: documents, relativeDirectory: config.directory, gameID: config.cloudGame, title: title)
                        return (archive, local)
                    }
                    try Task.checkCancellation()
                    let existing = Dictionary(uniqueKeysWithValues: local.files.map { (LocalSaveSnapshot.pathKey($0.path), $0.sha256) })
                    let replaced = archive.files.filter {
                        let key = LocalSaveSnapshot.pathKey($0.path)
                        return existing[key] != nil && existing[key] != $0.sha256
                    }.count
                    pending = .download(archive, config, record, local.fingerprint, replaced); task = nil
                } catch { report(error); finish(owned) }
            }
        } catch { report(error) }
    }
    func confirm() {
        guard let pending, let owned = token, let bookmark, task == nil else { return }
        self.pending = nil
        let documents = LibraryModel.documents
        task = Task {
            defer { finish(owned) }
            do {
                try await GameSessionWork.shared.waitForSaveOperation(owned)
                switch pending {
                case .upload(let archive, let config, let expected):
                    try await runAsync { try await LocalSaveCloudFiles.ensureMetadataCopies(bookmark: bookmark) }
                    let uploaded = try await run {
                        let current = try LocalSaveSnapshot.capture(documents: documents, relativeDirectory: config.directory, gameID: config.cloudGame, title: archive.title)
                        guard current.fingerprint == archive.fingerprint else { throw LocalSaveCloudFiles.Failure.changed }
                        let url = try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: true) {
                            try LocalSaveCloudFiles.upload(archive, to: $0, expectedVersions: expected)
                        }
                        // Status is read after coordination; never wait inside its accessor.
                        return (try? url.resourceValues(forKeys: [.ubiquitousItemIsUploadedKey]))?.ubiquitousItemIsUploaded == true
                    }
                    message = uploaded ? "iCloud reports this save backup is uploaded." : "Save backup written to the selected folder. Cloud upload has not been confirmed; check its status in Files."
                case .download(let archive, var config, let record, let fingerprint, _):
                    let capturedDirectory = config.directory
                    try await runAsync { try await LocalSaveCloudFiles.ensureLocalCopy(record, bookmark: bookmark) }
                    let receipt = try await run {
                        let current = try LocalSaveCloudFiles.withFolder(bookmark: bookmark, writing: false) { try LocalSaveCloudFiles.load(record, from: $0) }
                        guard current.fingerprint == archive.fingerprint else { throw LocalSaveCloudFiles.Failure.changed }
                        try Task.checkCancellation()
                        return try LocalSaveSnapshot.restore(current, documents: documents, relativeDirectory: capturedDirectory, expectedFingerprint: fingerprint)
                    }
                    config.cloudGame = archive.gameID
                    configurations.removeAll { $0.game == config.game }; configurations.append(config)
                    try saveConfigurations()
                    message = "\(receipt.changedFiles) save files restored. Original saves kept in Files → Madeira → iCloud Save Backups."
                }
            } catch { report(error) }
        }
    }
    func cancel() {
        if let task { task.cancel() } // Keep the guard until active IO has actually stopped.
        else if let owned = token { finish(owned); message = "Save synchronization cancelled." }
    }
}
