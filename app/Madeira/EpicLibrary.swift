// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Combine
import CryptoKit
#if canImport(UIKit)
import UIKit
#endif

struct EpicInstallation: Codable, Identifiable, Sendable {
    var game: EpicGame
    var accountKey: String
    var relativePath: String
    var arguments: String
    var bytes: Int64
    var id: String { game.appName }
}

struct EpicDownload: Codable, Identifiable, Sendable {
    var game: EpicGame
    var accountKey: String
    var state: String = "queued"
    var received: Int64 = 0
    var total: Int64 = 0
    var error: String?
    var manifestHash: String?
    var cancellationID: UUID?
    var cleanupIdentity: EpicDownloadCleanupIdentity?
    var cdnResults: [EpicDownloader.CDNMeasurement]?
    var id: String { game.appName }
}

/// Epic owns its account, queue, receipts and installation roots. No Steam
/// connection, token, depot, appmanifest or Cloud record is read or written here.
@MainActor final class EpicLibraryModel: ObservableObject {
    static let shared = EpicLibraryModel()
    @Published private(set) var games: [EpicGame] = []
    @Published private(set) var installations: [EpicInstallation] = []
    @Published private(set) var downloads: [EpicDownload] = []
    @Published private(set) var transferSpeed: (id: String, key: String, bytesPerSecond: Double, updatedAt: TimeInterval)?
    @Published private(set) var refreshing = false
    @Published var error: String?
    private var task: Task<Void, Never>?
    private var taskIdentity: UUID?
    private var active: String?
    private var activeAccountKey: String?
    private var cleanupTask: Task<Void, Never>?
    private var lastCDNRecordAt = Date.distantPast
    private var refreshTask: Task<Void, Never>?
    private var refreshIdentity: UUID?
    private var refreshDeferred = false
    private var forceRefreshDeferred = false
    private var generation = UUID()
    private var accountObserver: AnyCancellable?
    private var launchIdentity: UUID?
    private var preparedLaunch: EpicPreparedLaunch?
    private var sessionObserver: AnyCancellable?
    private var sessionActive = false
    private var writable = true
    private var refreshedAt: Date?
    #if canImport(UIKit)
    private var background: UIBackgroundTaskIdentifier = .invalid
    #endif
    private let account = EpicAccount.shared
    private let api = EpicAPI.shared
    private var recordStore: EpicRecordStore?
    var accountKey: String? { account.accountID.map(Self.key) }
    var hasInFlightDownload: Bool { task != nil || cleanupTask != nil }

    static func key(_ value: String) -> String { SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined() }
    private init() {
        do {
            let store = try EpicRecordStore(documents: LibraryModel.documents)
            try EpicOwnershipTokenFile.removeStale(documents: LibraryModel.documents)
            recordStore = store
            if let data = try store.read(name: "installations.json") {
                guard data.count < 16 * 1024 * 1024 else { throw EpicError.responseTooLarge }
                installations = try JSONDecoder().decode([EpicInstallation].self, from: data)
            }
            if let data = try store.read(name: "downloads.json") {
                guard data.count < 16 * 1024 * 1024 else { throw EpicError.responseTooLarge }
                downloads = try JSONDecoder().decode([EpicDownload].self, from: data)
                for i in downloads.indices {
                    downloads[i].state = downloads[i].cancellationID == nil ? "paused" : "cancel-failed"
                }
            }
        } catch {
            writable = false
            let reason = (error as? EpicRecordStore.Failure)?.diagnostic ??
                (error as? EpicDirectory.Failure)?.diagnostic ??
                (error is DecodingError ? "decode-records" : "read-records")
            LogStore.shared.log("[epic-storage] records \(reason)")
            self.error = "Could not read Epic installation records. Existing files have been kept."
        }
        accountObserver = Publishers.CombineLatest(account.$sessionGeneration, account.$accountID.removeDuplicates())
            .dropFirst().sink { [weak self] _ in
                // Revoke the runtime-start permit synchronously with the
                // account transition; waiting for the Task below leaves a
                // window in which the background Wine worker could claim it.
                self?.preparedLaunch?.cancelBeforeStart()
                self?.launchIdentity = nil
                // @Published sends in willSet; read the committed account after
                // its synchronous transition, never restore the previous cache.
                Task { @MainActor in self?.accountChanged() }
        }
        restoreLibraryCache()
        sessionActive = GameSessionWork.shared.pausesBackgroundWork
        sessionObserver = GameSessionWork.shared.$pausesBackgroundWork.removeDuplicates().sink { [weak self] _ in
            Task { @MainActor in self?.sessionChanged(active: GameSessionWork.shared.pausesBackgroundWork) }
        }
    }

    var visibleInstallations: [EpicInstallation] {
        installations.filter { accountKey == nil || $0.accountKey == accountKey }
    }
    var visibleDownloads: [EpicDownload] { downloads.filter { $0.accountKey == accountKey } }
    func installed(_ name: String) -> EpicInstallation? {
        visibleInstallations.first { $0.id == name && (try? LibraryModel.executable($0.relativePath)) != nil }
    }
    func download(_ name: String) -> EpicDownload? { visibleDownloads.first { $0.id == name } }

    private func accountChanged() {
        launchIdentity = nil
        preparedLaunch?.cancelBeforeStart()
        generation = UUID(); task?.cancel(); active = nil
        refreshTask?.cancel(); refreshTask = nil; refreshIdentity = nil
        refreshDeferred = false; forceRefreshDeferred = false
        refreshing = false; games = []; refreshedAt = nil
        for i in downloads.indices where ["downloading", "queued", "waiting"].contains(downloads[i].state) { downloads[i].state = "paused" }
        persistQueue(); endBackground()
        restoreLibraryCache()
        if account.signedIn { refresh() }
    }

    private var cache: String? { accountKey.map { "library-" + $0 + ".json" } }
    private func restoreLibraryCache() {
        guard writable, let cache, let data = try? recordStore?.read(name: cache), data.count < 16 * 1024 * 1024,
              let stored = try? JSONDecoder().decode([EpicGame].self, from: data), stored.count <= 20000 else { return }
        games = stored
    }
    func refresh(force: Bool = false) {
        guard account.signedIn else { return }
        if sessionActive {
            refreshDeferred = true; forceRefreshDeferred = forceRefreshDeferred || force; return
        }
        guard refreshTask == nil else { return }
        if !force, let refreshedAt, Date().timeIntervalSince(refreshedAt) < 300 { return }
        refreshing = true
        let epoch = generation, accountEpoch = account.sessionGeneration
        let identity = UUID(); refreshIdentity = identity
        refreshTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if refreshIdentity == identity { refreshing = false; refreshTask = nil; refreshIdentity = nil }
            }
            do {
                let games = try await api.games()
                guard !Task.isCancelled, !sessionActive, refreshIdentity == identity,
                      generation == epoch, account.sessionGeneration == accountEpoch else { return }
                self.games = games
                refreshedAt = Date()
                if writable, let cache { try? recordStore?.write(JSONEncoder().encode(games), name: cache) }
            } catch {
                if !Task.isCancelled && !sessionActive && refreshIdentity == identity && generation == epoch && account.sessionGeneration == accountEpoch {
                    self.error = Self.message(error)
                }
            }
        }
    }

    func enqueue(_ game: EpicGame) {
        guard writable, let key = accountKey, account.signedIn else { error = "Sign in to Epic Games before downloading."; return }
        guard game.launchRequirement == nil else { error = "This game requires an additional launcher that is not supported yet."; return }
        if let i = downloads.firstIndex(where: { $0.id == game.id && $0.accountKey == key }) {
            guard downloads[i].state != "downloading", downloads[i].cancellationID == nil else { return }
            downloads[i].state = sessionActive ? "waiting" : "queued"; downloads[i].error = nil
        } else {
            var job = EpicDownload(game: game, accountKey: key)
            job.state = sessionActive ? "waiting" : "queued"; downloads.append(job)
        }
        persistQueue(); pump()
    }
    func pause(_ name: String) {
        guard let i = downloads.firstIndex(where: { $0.id == name && $0.accountKey == accountKey }) else { return }
        guard downloads[i].cancellationID == nil else { return }
        downloads[i].state = "paused"
        if active == name { task?.cancel() }
        persistQueue()
    }
    /// User-confirmed cancellation owns only the account/asset-derived root.
    /// Persist recovery identity before deletion, and drain the actual writer.
    func cancel(_ name: String) {
        guard writable, let key = accountKey,
              let i = downloads.firstIndex(where: { $0.id == name && $0.accountKey == key }) else { return }
        guard !sessionActive, cleanupTask == nil else {
            error = "Wait for the game, save operation or current cleanup to finish before clearing this download."
            return
        }
        guard !installations.contains(where: { $0.id == name && $0.accountKey == key }) else {
            error = "This game has an installation record. Its files have been kept."
            return
        }
        let previous = downloads[i]
        let cancellationID = previous.cancellationID ?? UUID()
        downloads[i].cancellationID = cancellationID
        downloads[i].state = "cancelling"; downloads[i].error = nil
        guard persistQueue() else { downloads[i] = previous; return }
        let writer = task
        if active == name && activeAccountKey == key { writer?.cancel() }
        let destination = Self.destination(key: key, name: name)
        cleanupTask = Task { [weak self] in
            guard let self else { return }
            defer { cleanupTask = nil; pump() }
            // Even a cancelled transfer can finish its last write asynchronously.
            await writer?.value
            guard let index = downloads.firstIndex(where: {
                $0.id == name && $0.accountKey == key && $0.cancellationID == cancellationID
            }) else { return }
            do {
                var pinned = downloads[index].cleanupIdentity
                if pinned == nil {
                    let expected = downloads[index].manifestHash
                    pinned = try await Task.detached(priority: .utility) {
                        try EpicInstallStore.inspectIncompleteDownload(destination: destination,
                            cancellationID: cancellationID, expectedManifestHash: expected)
                    }.value
                    if let pinned {
                        downloads[index].cleanupIdentity = pinned
                        guard persistQueue() else { throw EpicRecordStore.Failure.storage }
                    }
                }
                if let pinned {
                    try await Task.detached(priority: .utility) {
                        try EpicInstallStore.removeIncompleteDownload(destination: destination,
                            cancellationID: cancellationID, identity: pinned)
                    }.value
                }
                let next = downloads.filter {
                    !($0.id == name && $0.accountKey == key && $0.cancellationID == cancellationID)
                }
                guard let recordStore else { throw EpicRecordStore.Failure.storage }
                try recordStore.write(JSONEncoder().encode(next), name: "downloads.json")
                downloads = next
                LogStore.shared.log("[epic-cleanup] completed")
            } catch {
                guard let index = downloads.firstIndex(where: {
                    $0.id == name && $0.accountKey == key && $0.cancellationID == cancellationID
                }) else { return }
                if (error as? EpicDownloadCleanupError) == .completed,
                   downloads[index].cleanupIdentity == nil {
                    downloads[index].cancellationID = nil
                    downloads[index].state = "paused"
                    downloads[index].error = "Installation files are already complete and have been kept. Resume to finish registration."
                } else {
                    downloads[index].state = "cancel-failed"
                    downloads[index].error = "Could not safely clear this download. Files have been kept where possible. Retry cleanup."
                }
                persistQueue()
                LogStore.shared.log("[epic-cleanup] failed")
            }
        }
    }
    func resume(_ name: String) { if let game = download(name)?.game { enqueue(game) } }

    func sessionChanged(active running: Bool) {
        guard sessionActive != running else { return }
        sessionActive = running
        for i in downloads.indices where downloads[i].accountKey == accountKey {
            if running && ["queued", "downloading"].contains(downloads[i].state) { downloads[i].state = "waiting" }
            else if !running && downloads[i].state == "waiting" { downloads[i].state = "queued" }
        }
        if running {
            task?.cancel()
            if refreshTask != nil {
                refreshDeferred = true; forceRefreshDeferred = true
                refreshTask?.cancel(); refreshTask = nil; refreshIdentity = nil; refreshing = false
            }
        }
        persistQueue()
        if !running {
            preparedLaunch?.finish(); preparedLaunch = nil; launchIdentity = nil
            pump()
            let pending = refreshDeferred, force = forceRefreshDeferred
            refreshDeferred = false; forceRefreshDeferred = false
            if pending { refresh(force: force) }
        }
    }

    @discardableResult private func persistQueue() -> Bool {
        guard writable, let recordStore else { return false }
        do {
            try recordStore.write(JSONEncoder().encode(downloads), name: "downloads.json")
            return true
        } catch { self.error = "Could not save Epic download progress."; return false }
    }
    private func endBackground() {
        #if canImport(UIKit)
        if background != .invalid { UIApplication.shared.endBackgroundTask(background); background = .invalid }
        #endif
    }
    private func pump() {
        guard task == nil, cleanupTask == nil, !sessionActive, writable, account.signedIn, let key = accountKey,
              let job = downloads.first(where: { $0.state == "queued" && $0.accountKey == key }) else { return }
        let epoch = generation, accountEpoch = account.sessionGeneration
        let identity = UUID(); taskIdentity = identity
        transferSpeed = nil
        lastCDNRecordAt = .distantPast
        active = job.id
        activeAccountKey = key
        if let i = downloads.firstIndex(where: { $0.id == job.id && $0.accountKey == key }) { downloads[i].state = "downloading" }
        persistQueue()
        #if canImport(UIKit)
        background = UIApplication.shared.beginBackgroundTask(withName: "Epic Games download") { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                guard self.taskIdentity == identity else { return }
                if let active = self.active { self.pause(active) }
                self.endBackground()
            }
        }
        #endif
        task = Task { [weak self] in
            guard let self else { return }
            var stage = "manifest-address"
            do {
                let location = try await api.manifest(game: job.game)
                stage = "manifest-fetch"
                let data = try await Self.fetchManifest(location)
                stage = "manifest-hash"
                guard let sha = location.sha1 else { throw EpicError.invalidResponse }
                let actual = Insecure.SHA1.hash(data: data).map { String(format: "%02x", $0) }.joined()
                guard actual == sha.lowercased() else { throw EpicError.invalidResponse }
                stage = "manifest-parse"
                let manifest = try EpicManifest.parse(data)
                // BuildPatch's builder name is separate from the public asset
                // identity bound by the authenticated API route and raw digest.
                if manifest.appName != job.game.appName {
                    LogStore.shared.log("[epic-download] manifest-name-alias accepted")
                }
                try Task.checkCancellation()
                guard generation == epoch, account.sessionGeneration == accountEpoch else { throw EpicError.accountChanged }
                let ownedManifestHash = Self.keyData(data)
                if let i = downloads.firstIndex(where: { $0.id == job.id && $0.accountKey == key }) {
                    var origins = Set<String>()
                    downloads[i].cdnResults = location.chunkBaseURLs.compactMap { url in
                        guard let host = url.host, let scheme = url.scheme,
                              origins.insert(scheme + "://" + host).inserted else { return nil }
                        return EpicDownloader.CDNMeasurement(host: host, scheme: scheme,
                            bytesPerSecond: nil, failed: false)
                    }
                    persistQueue()
                }
                let relative = "Program Files/Epic Games/" + String(key.prefix(12)) + "-" + String(Self.key(job.game.appName).prefix(20))
                // Keep the raw directory chain for the installer's O_NOFOLLOW
                // checks; Steam's existing resolved drive remains unchanged.
                let destination = LibraryModel.documents.appendingPathComponent("wine/drive_c", isDirectory: true)
                    .appendingPathComponent(relative, isDirectory: true)
                stage = "install"
                let result = try await EpicDownloader.install(manifestData: data, baseURLs: location.chunkBaseURLs,
                    destination: destination, progress: { [weak self] bytes, total in
                        Task { @MainActor in
                            guard let self, self.generation == epoch, self.active == job.id,
                                  let i = self.downloads.firstIndex(where: { $0.id == job.id && $0.accountKey == key }) else { return }
                            // Progress starts only after the store accepts its
                            // ownership checkpoint. A rejected upstream update
                            // must retain the old partial download's digest.
                            if self.downloads[i].manifestHash != ownedManifestHash {
                                self.downloads[i].manifestHash = ownedManifestHash
                                self.persistQueue()
                            }
                            self.downloads[i].received = bytes; self.downloads[i].total = total
                        }
                    }, onTransferRate: { [weak self] rate in
                        Task { @MainActor in
                            self?.recordTransferRate(rate, name: job.id, key: key, epoch: epoch, identity: identity)
                        }
                    }, onCDNMeasurement: { [weak self] result in
                        Task { @MainActor in
                            self?.recordCDN(result, name: job.id, key: key, epoch: epoch, identity: identity)
                        }
                    })
                try Task.checkCancellation()
                guard generation == epoch, account.sessionGeneration == accountEpoch else { throw EpicError.accountChanged }
                let installed = EpicInstallation(game: job.game, accountKey: key,
                    relativePath: relative + "/" + result.executable, arguments: result.arguments, bytes: result.totalBytes)
                _ = try LibraryModel.executable(installed.relativePath)
                var next = installations.filter { $0.id != job.id || $0.accountKey != key }; next.append(installed)
                guard let recordStore else { throw EpicRecordStore.Failure.storage }
                try recordStore.write(JSONEncoder().encode(next), name: "installations.json")
                installations = next
                LibraryModel.shared.upsertEpic(installed)
                downloads.removeAll { $0.id == job.id && $0.accountKey == key }
            } catch {
                let category: String
                if Task.isCancelled || error is CancellationError { category = "cancelled" }
                else if (error as? EpicError) == .invalidResponse { category = "invalid-response" }
                else if error is EpicError { category = "epic" }
                else if error is EpicDownloadError { category = "download" }
                else { category = "other" }
                // Only fixed stage/category strings: never errors, signed URLs,
                // response bodies, game names, account identifiers or tokens.
                LogStore.shared.log("[epic-download] stage=\(stage) category=\(category)")
                if generation == epoch, let i = downloads.firstIndex(where: { $0.id == job.id && $0.accountKey == key }),
                   downloads[i].cancellationID == nil {
                    if Task.isCancelled || error is CancellationError {
                        // Resume may have queued this job while cancellation was
                        // finishing. Preserve that new intent, then pump it.
                        if !["queued", "waiting"].contains(downloads[i].state) { downloads[i].state = "paused" }
                    } else { downloads[i].state = "failed" }
                    downloads[i].error = downloads[i].state == "failed" ? Self.message(error) : nil
                }
            }
            if taskIdentity == identity {
                transferSpeed = nil
                task = nil; taskIdentity = nil; active = nil; activeAccountKey = nil; persistQueue(); endBackground(); pump()
            }
        }
    }

    private static func keyData(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    func downloadSpeed(_ job: EpicDownload, now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> Double? {
        guard job.state == "downloading", job.cancellationID == nil, job.accountKey == accountKey,
              active == job.id, activeAccountKey == job.accountKey,
              let speed = transferSpeed, speed.id == job.id, speed.key == job.accountKey,
              now.isFinite, now >= speed.updatedAt, now - speed.updatedAt <= 3 else { return nil }
        return speed.bytesPerSecond
    }

    private func recordTransferRate(_ rate: Double, name: String, key: String, epoch: UUID, identity: UUID) {
        guard generation == epoch, taskIdentity == identity, active == name, activeAccountKey == key,
              accountKey == key,
              let job = downloads.first(where: { $0.id == name && $0.accountKey == key }),
              job.state == "downloading", job.cancellationID == nil, rate.isFinite, rate > 0 else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let speed = transferSpeed, speed.id == name, speed.key == key, now - speed.updatedAt < 1 { return }
        // Transient UI data: never written to the queue or reused after restart.
        transferSpeed = (name, key, rate, now)
    }

    private func recordCDN(_ result: EpicDownloader.CDNMeasurement, name: String, key: String,
                           epoch: UUID, identity: UUID) {
        guard generation == epoch, taskIdentity == identity, active == name, activeAccountKey == key,
              accountKey == key,
              let index = downloads.firstIndex(where: { $0.id == name && $0.accountKey == key }),
              downloads[index].state == "downloading", downloads[index].cancellationID == nil,
              result.scheme == "https", let host = EpicCDNURLPolicy.host(result.host),
              result.bytesPerSecond == nil || (result.bytesPerSecond!.isFinite && result.bytesPerSecond! > 0) else { return }
        var safe = EpicDownloader.CDNMeasurement(host: host, scheme: result.scheme,
            bytesPerSecond: result.failed ? nil : result.bytesPerSecond, failed: result.failed)
        safe.failure = result.failed ? result.failure : nil
        var results = downloads[index].cdnResults ?? []
        let previous = results.first { $0.host == host && $0.scheme == safe.scheme }
        let now = Date()
        // Every first probe and status change is visible. Later transfer-rate
        // updates are throttled, avoiding a disk/UI update for every chunk.
        guard previous == nil || previous?.bytesPerSecond == nil || previous?.failed != safe.failed || previous?.failure != safe.failure ||
              now.timeIntervalSince(lastCDNRecordAt) >= 1 else { return }
        results.removeAll { $0.host == host && $0.scheme == safe.scheme }
        results.append(safe)
        downloads[index].cdnResults = Array(results.prefix(16))
        lastCDNRecordAt = now; persistQueue()
        let rate = Int64(min(safe.bytesPerSecond ?? 0, Double(Int64.max / 2)))
        let reason: String
        switch safe.failure {
        case .http(let status): reason = "http-\(status)"
        case .network(let code): reason = "network-\(code)"
        case .integrity: reason = "integrity"
        case .other: reason = "other"
        case nil: reason = "none"
        }
        LogStore.shared.log("[epic-cdn] host=\(host) scheme=https bytes-per-second=\(rate) failed=\(safe.failed ? 1 : 0) reason=\(reason)")
    }
    private static func destination(key: String, name: String) -> URL {
        LibraryModel.documents.appendingPathComponent("wine/drive_c", isDirectory: true)
            .appendingPathComponent("Program Files/Epic Games/" + String(key.prefix(12)) + "-" + String(Self.key(name).prefix(20)), isDirectory: true)
    }

    private static func fetchManifest(_ location: EpicManifestLocation) async throws -> Data {
        let urls = [location.url] + location.alternateURLs
        guard urls.count <= 16 else { throw EpicError.invalidResponse }
        var failure: Error = EpicDownloadError.contentServer
        // Try only the URLs supplied by the authenticated manifest response.
        // Each fetch keeps its own HTTPS origin boundary and byte limit. Digest
        // verification follows once bytes arrive; bad content never triggers a
        // retry that might hide an integrity failure.
        for url in urls {
            try Task.checkCancellation()
            do {
                let data = try await EpicDownloader.fetchManifest(url)
                try Task.checkCancellation()
                return data
            } catch {
                try Task.checkCancellation()
                if error is CancellationError { throw error }
                failure = error
            }
        }
        throw failure
    }

    func launch(_ entry: LibraryEntry) async throws -> EpicPreparedLaunch {
        guard preparedLaunch?.isClaimed != true else {
            throw EpicLaunchArguments.Invalid(reason: "An Epic game is already starting or running.")
        }
        guard let name = entry.epicAppName, let installed = installed(name),
              installed.accountKey == accountKey, entry.epicAccountKey == installed.accountKey,
              entry.relativePath == installed.relativePath, account.signedIn else { throw EpicError.notSignedIn }
        guard installed.game.launchRequirement == nil,
              entry.startServices != true, !entry.isBatch else {
            throw EpicLaunchArguments.Invalid(reason: "This Epic game needs a direct program or desktop launch without a services batch.")
        }
        let epoch = generation, accountEpoch = account.sessionGeneration
        let identity = UUID()
        preparedLaunch?.cancelBeforeStart()
        launchIdentity = identity
        // Validate user arguments before requesting a one-use code.
        _ = try EpicLaunchArguments.authenticated(command: entry.launchArguments, userArguments: "",
            exchangeCode: "validation_only_placeholder", appName: name,
            accountID: account.accountID ?? "", displayName: account.displayName)
        var ownership: EpicOwnershipTokenFile?
        if installed.game.requiresOwnershipToken {
            let token = try await api.ownershipToken(game: installed.game)
            guard !Task.isCancelled, generation == epoch, account.sessionGeneration == accountEpoch,
                  launchIdentity == identity else { throw EpicError.accountChanged }
            ownership = try EpicOwnershipTokenFile(data: token, documents: LibraryModel.documents)
        }
        let code = try await api.exchangeCode()
        guard !Task.isCancelled, generation == epoch, account.sessionGeneration == accountEpoch,
              launchIdentity == identity,
              let accountID = account.accountID else { throw EpicError.accountChanged }
        let arguments = try EpicLaunchArguments.authenticated(command: entry.launchArguments, userArguments: "",
            exchangeCode: code, appName: name, accountID: accountID, displayName: account.displayName,
            ownershipTokenPath: ownership?.windowsPath)
        try ownership?.validate()
        let result = EpicPreparedLaunch(identity: identity, arguments: arguments, secret: code, ownership: ownership)
        preparedLaunch = result
        return result
    }

    static func message(_ error: Error) -> String {
        if error is CancellationError { return "Epic operation paused." }
        if let epic = error as? EpicError { return epic.localizedDescription }
        if let launch = error as? EpicLaunchArguments.Invalid { return launch.localizedDescription }
        // Network errors may contain signed URLs; do not show/log those.
        return "Epic operation failed. Check your connection and available storage, then try again."
    }
}

extension LibraryModel {
    func epicEntry(_ installed: EpicInstallation) -> LibraryEntry {
        if var entry = entries.first(where: { $0.epicAppName == installed.id && $0.epicAccountKey == installed.accountKey }) {
            entry.relativePath = installed.relativePath; return entry
        }
        var entry = LibraryEntry(title: installed.game.title, relativePath: installed.relativePath, bits: 0)
        entry.epicAppName = installed.id; entry.epicAccountKey = installed.accountKey
        entry.arguments = installed.arguments; entry.folderBytes = installed.bytes
        return entry
    }
    func upsertEpic(_ installed: EpicInstallation) {
        var entry = epicEntry(installed); entry.folderBytes = installed.bytes
        save(entry)
    }
}
