// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 Jfishin, 125hz
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
//
// Derived from Jfishin's Madeira Steam client, used in Madeira with the
// author's permission (see docs/STEAM_SIGNIN.md, "Provenance"). Substantially
// rewritten by 125hz for the owned library and downloads (docs/STEAM_LIBRARY.md).

import Foundation
import zlib
import CommonCrypto

/// Progress for one application install, across all of its depots.
struct SteamDownloadProgress: Equatable, Sendable {
    enum Phase: String, Sendable { case preparing, checking, downloading, finishing }
    var phase: Phase = .preparing
    /// Manifest compressed size of the whole install (all selected depots).
    var totalBytes: UInt64 = 0
    /// Manifest compressed size of verified local and newly fetched chunks.
    var doneBytes: UInt64 = 0
    /// Compressed bytes whose local plaintext passed the manifest SHA-1 check.
    var reusedBytes: UInt64 = 0
    /// Compressed bytes fetched for this attempt; excludes all local reuse.
    var networkBytes: UInt64 = 0
    var bytesPerSecond: Double = 0
    /// Selected source or a reason for automatic fallback; contains no auth data.
    var sourceNotice: String?

    var fraction: Double { totalBytes > 0 ? min(1, Double(doneBytes) / Double(totalBytes)) : 0 }
}

/// Orchestrates downloading an owned application's Windows depots from the
/// Steam content network into a Steam library folder.
///
/// Follows Steam's content protocol: the account's depot key and the
/// manifest request code come from the CM connection, which issues them only
/// to an account that owns the depot; manifests and chunks come from content
/// servers over HTTPS. Nothing here alters the downloaded files.
///
/// Behaviour:
/// - All manifests are fetched first, so progress covers the whole install.
/// - Existing chunks, including journaled chunks from an interrupted install,
///   are checked against the manifest before any missing chunk is fetched.
/// - Files are sized to their manifest length, so an update that shrinks a
///   file cannot leave stale trailing bytes.
/// - Chunk writes happen on the download tasks with pwrite, never on the
///   main actor; a dedicated URLSession keeps chunks out of the URL cache.
/// - Manifest paths are validated to stay inside the install folder, and
///   directory names are folded case-insensitively (Windows semantics on a
///   case-sensitive iOS volume).
/// - Each chunk retries on other content servers before the install fails.
@MainActor
final class DepotDownloader {
    private let session: SteamCMSession
    private let sourceStore: SteamContentSourceStore
    private var depotKeys: [UInt32: Data] = [:]
    private var cdnAuthTokens: [String: String] = [:]  // "depot|host" -> "?auth=…" fragment
    private let maxConcurrentChunks = 8
    private let attemptsPerChunk = 5

    private nonisolated static let http: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCredentialStorage = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 300
        config.httpMaximumConnectionsPerHost = 8
        return URLSession(configuration: config, delegate: ContentRedirectPolicy(), delegateQueue: nil)
    }()

    private nonisolated static let probeHTTP: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.urlCredentialStorage = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 4
        config.timeoutIntervalForResource = 8
        config.httpMaximumConnectionsPerHost = 1
        return URLSession(configuration: config, delegate: ContentRedirectPolicy(), delegateQueue: nil)
    }()

    /// Replaces the content server directory (host tests only): the servers
    /// to fetch from, as `https://host` or `http://host:port` URLs.
    var contentHosts: ((_ appID: UInt32) async throws -> [String])?

    init(session: SteamCMSession, sourceStore: SteamContentSourceStore = .shared) {
        self.session = session
        self.sourceStore = sourceStore
    }

    // MARK: - Public API

    /// Download an app into `steamApps/common/<installdir>` and write its
    /// appmanifest. Returns the install folder. Throws CancellationError when
    /// the calling task is cancelled; completed chunks stay journaled.
    func install(_ app: SteamAppInfo, steamApps: URL,
                 ownedDepots: @escaping () async -> Set<UInt32>? = { nil },
                 progress report: @escaping (SteamDownloadProgress) -> Void) async throws -> URL {
        let depots = app.installDepots()
        guard !depots.isEmpty else { throw SteamError.depotNotFound(app.appID) }
        // Before the key requests, so a refused depot still has its selection logged.
        SteamLog.event("[steam-depot] selection app=\(app.appID) build=\(app.buildID) \(app.depotSelectionSummary())")

        let folderName = SteamInstallFiles.safeFolderName(app.installDir.isEmpty ? "app_\(app.appID)" : app.installDir)
        let installURL = steamApps.appendingPathComponent("common", isDirectory: true)
            .appendingPathComponent(folderName, isDirectory: true)
        let journalDir = steamApps.appendingPathComponent("downloading", isDirectory: true)
            .appendingPathComponent("\(app.appID)", isDirectory: true)
        try FileManager.default.createDirectory(at: installURL, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: journalDir, withIntermediateDirectories: true)

        var state = SteamDownloadProgress()
        report(state)

        depotCache = steamApps.appendingPathComponent("depotcache", isDirectory: true)
        let hosts: [String]
        let manualAllowed: Set<String>
        if let provider = contentHosts {
            hosts = try await provider(app.appID)
            manualAllowed = Set(hosts)
        } else {
            let discovered = try await contentServers(appID: app.appID)
            hosts = discovered.hosts
            manualAllowed = discovered.manualAllowed
        }
        guard !hosts.isEmpty else { throw SteamError.chunkDownloadFailed("No content servers are available.") }

        // 1. Keys, manifests and per-host authorization for every depot.
        let sourceCellID = session.cellID
        let selected = sourceStore.selectedHost
        let manual = SteamContentSourceDirectory.manualURL(selectedHost: selected, allowed: manualAllowed)
        let remembered = sourceStore.rememberedHost(cellID: sourceCellID, available: hosts)
        let prioritized = SteamContentSourceDirectory.prioritize(hosts, selectedHost: manual == nil
                                                                 ? remembered.flatMap { URL(string: $0)?.host } : selected)
        let pool = Self.contentHostPool(prioritized, maximum: maxConcurrentChunks)
        // Request codes and CDN tokens stay on HTTPS. HTTP partners carry only
        // encrypted chunks verified against the secure manifest and depot key.
        let secure = pool.filter { URL(string: $0)?.scheme == "https" }
        guard !secure.isEmpty else {
            throw SteamError.manifestFetchFailed("No permitted HTTPS content server is available.")
        }
        let manifestHosts = secure
        let health = ContentHostHealth(manualHost: manual, rememberedHost: remembered)
        defer {
            sourceStore.forget(hosts: health.failedHosts(), cellID: sourceCellID)
            sourceStore.remember(health.verifiedSamples(cellID: sourceCellID), cellID: sourceCellID)
        }
        if let selected {
            state.sourceNotice = manual == nil
                ? "Selected CDN \(selected) is unavailable for this game; using automatic selection."
                : "Selected CDN: \(selected)"
            SteamLog.event("[steam-cdn] manual host=\(selected) available=\(manual == nil ? 0 : 1)")
        } else if let remembered, let host = URL(string: remembered)?.host {
            state.sourceNotice = "Preparing manifests and CDN access. Previous measured node: \(host). Available nodes will be tested next."
        } else {
            state.sourceNotice = "Preparing manifests and CDN access. Available nodes will be tested next."
        }
        SteamLog.event("[steam-cdn] stage=preparing nodes=\(pool.count)")
        report(state)
        var plans: [DepotPlan] = []
        var licenseSkipped: [UInt32] = []
        // The logged-on account, for the install record. Read while the session
        // is connected: it idles out during a long download.
        var accountID: UInt64 = 0
        for depot in depots {
            try Task.checkCancellation()
            guard let gid = depot.publicManifestID else { continue }
            let key: Data
            do {
                key = try await depotKey(depotID: depot.depotID, appID: app.appID)
            } catch SteamError.depotKeyNotFound(let refused) {
                // A depot Steam refuses AND the account's licenses do not
                // include is content this account does not own (another
                // edition, extra content): it is left out. Any other refusal
                // still fails.
                if let owned = await ownedDepots(), !owned.isEmpty, !owned.contains(refused) {
                    licenseSkipped.append(refused)
                    continue
                }
                throw SteamError.depotKeyNotFound(refused)
            }
            if session.steamID != 0 { accountID = session.steamID }
            let contentAppID = !app.freeToDownload ? (depot.fromApp ?? app.appID) : app.appID
            let manifest = try await fetchManifest(depotID: depot.depotID, appID: contentAppID,
                                                   manifestGID: gid, key: key, hosts: manifestHosts, health: health)
            var auth: [String: String] = [:]
            for host in pool where URL(string: host)?.scheme == "https" {
                auth[host] = await cdnAuthFragment(depotID: depot.depotID, appID: contentAppID, host: host)
            }
            plans.append(DepotPlan(depotID: depot.depotID, manifestGID: gid, key: key, manifest: manifest,
                                   hosts: pool, auth: auth,
                                   declaredSize: depot.publicSizeBytes, health: health))
        }
        if !licenseSkipped.isEmpty {
            SteamLog.event("[steam-depot] license app=\(app.appID) skipped=\(licenseSkipped.map(String.init).joined(separator: ",")) kept=\(plans.map { String($0.depotID) }.joined(separator: ","))")
        }
        guard !plans.isEmpty else {
            if let refused = licenseSkipped.first { throw SteamError.depotKeyNotFound(refused) }
            throw SteamError.depotNotFound(app.appID)
        }

        // 2. Verify existing files off the main actor before any chunk traffic,
        // including CDN benchmarks. A journal alone cannot prove bytes remain.
        state.phase = .checking
        report(state)
        let preparation = Task.detached(priority: .userInitiated) {
            try Self.prepare(plans: plans, installURL: installURL, journalDir: journalDir)
        }
        let prepared = try await withTaskCancellationHandler {
            try await preparation.value
        } onCancel: {
            preparation.cancel()
        }
        try Task.checkCancellation()
        state.totalBytes = prepared.totalBytes
        state.doneBytes = prepared.doneBytes
        state.reusedBytes = prepared.doneBytes
        state.sourceNotice = health.manualFallbackNotice ?? state.sourceNotice
        report(state)
        SteamLog.event("[steam-depot] install begin app=\(app.appID) depots=\(plans.count) files=\(prepared.fileCount) resume=\(prepared.doneBytes > 0 ? 1 : 0)")

        let remaining = prepared.remainingUncompressed
        if remaining > 0 {
            let values = try? installURL.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            let available = UInt64(max(0, values?.volumeAvailableCapacityForImportantUsage ?? Int64.max))
            if available < remaining { throw SteamError.insufficientDiskSpace(needed: remaining, available: available) }
        }

        // 3. Only missing or mismatched chunks need network transfer.
        if state.doneBytes < state.totalBytes {
            state.phase = .downloading
            report(state)
        }
        let started = Date()
        var lastReport = Date.distantPast
        for (index, plan) in plans.enumerated() {
            let journal = try JournalWriter(url: prepared.journals[index])
            defer { journal.close() }
            var work = prepared.pending[index]
            let paths = prepared.paths[index]
            let candidate = work.first { plan.manifest.files[$0.file].chunks[$0.chunk].uncompressedSize > 0 }
            if let candidate {
                let chunk = plan.manifest.files[candidate.file].chunks[candidate.chunk]
                state.sourceNotice = "Testing \(plan.hosts.count) CDN nodes…"
                SteamLog.event("[steam-cdn] stage=benchmark nodes=\(plan.hosts.count)")
                report(state)
                let fastest = try await Self.benchmarkChunk(chunk, plan: plan, path: paths[candidate.file])
                try Task.checkCancellation()
                journal.append(candidate.key)
                work.removeAll { $0.key == candidate.key }
                state.doneBytes += UInt64(chunk.compressedSize)
                state.networkBytes += UInt64(chunk.compressedSize)
                let elapsed = Date().timeIntervalSince(started)
                if elapsed > 1 { state.bytesPerSecond = Double(state.networkBytes) / elapsed }
                sourceStore.remember(health.verifiedSamples(cellID: sourceCellID), cellID: sourceCellID)
                if let fastest, let host = URL(string: fastest)?.host {
                    if let fallback = health.manualFallbackNotice { state.sourceNotice = fallback }
                    else if let manual { state.sourceNotice = "Selected CDN: \(URL(string: manual)?.host ?? host)" }
                    else if let selected {
                        state.sourceNotice = "Selected CDN \(selected) is unavailable for this game; using automatic selection. Fastest verified CDN: \(host)"
                    } else { state.sourceNotice = "Fastest verified CDN: \(host)" }
                }
                report(state)
            }
            let maximum = maxConcurrentChunks, attempts = attemptsPerChunk
            try await withThrowingTaskGroup(of: (UInt64, UInt64).self) { group in
                var next = 0
                func enqueue() {
                    guard next < work.count else { return }
                    let item = work[next]; next += 1
                    let chunk = plan.manifest.files[item.file].chunks[item.chunk]
                    let path = paths[item.file]
                    group.addTask {
                        try Task.checkCancellation()
                        try await Self.fetchChunk(chunk, plan: plan, path: path, attempts: attempts, seed: item.file &+ item.chunk)
                        return (item.key, UInt64(chunk.compressedSize))
                    }
                }
                for _ in 0..<min(maximum, work.count) { enqueue() }
                for try await (key, bytes) in group {
                    journal.append(key)
                    state.doneBytes += bytes
                    state.networkBytes += bytes
                    let now = Date()
                    if now.timeIntervalSince(lastReport) >= 0.25 {
                        lastReport = now
                        let elapsed = now.timeIntervalSince(started)
                        if elapsed > 1 { state.bytesPerSecond = Double(state.networkBytes) / elapsed }
                        state.sourceNotice = health.manualFallbackNotice ?? state.sourceNotice
                        report(state)
                    }
                    enqueue()
                }
            }
        }

        // 4. Install record. Sizes come from the manifests; no tree walk.
        state.phase = .finishing
        state.sourceNotice = health.manualFallbackNotice ?? state.sourceNotice
        report(state)
        let installed = plans.map { plan in
            AppManifestWriter.InstalledDepot(depotID: Int(plan.depotID), manifestGID: plan.manifestGID,
                                             size: Int64(plan.manifest.totalUncompressedSize))
        }
        // Depots taken from another app are that app's content. Valve's client
        // refuses a launch until the owner app has its own record, so both
        // records are written the way the client writes them.
        var owners: [UInt32: UInt32] = [:]
        for depot in depots { if let from = depot.fromApp, from != app.appID { owners[depot.depotID] = from } }
        let own = installed.filter { owners[UInt32($0.depotID)] == nil }
        let shared = installed.filter { owners[UInt32($0.depotID)] != nil }
        // Files Valve's client must customize per user before they run, as
        // Windows-style install-relative paths for the record's CheckGuid block.
        let custom = plans.flatMap { plan in
            plan.manifest.files.filter { $0.flags & DepotManifest.customExecutableFlag != 0 && !$0.isDirectory }
                .map { $0.filename.replacingOccurrences(of: "/", with: "\\") }
        }
        if !custom.isEmpty { SteamLog.event("[steam-record] custom-executables app=\(app.appID) custom=\(custom.count)") }
        try AppManifestWriter.writeManifest(
            appID: app.appID, name: app.name, installDir: folderName, buildID: app.buildID,
            steamID: accountID, sizeOnDisk: prepared.totalUncompressed,
            steamAppsPath: steamApps.path, installedDepots: own,
            sharedDepots: shared.map { ($0.depotID, Int(owners[UInt32($0.depotID)]!)) },
            customExecutables: custom)
        if !shared.isEmpty {
            var written = 0, skipped = 0
            for ownerID in Set(owners.values).sorted() {
                let depots = shared.filter { owners[UInt32($0.depotID)] == ownerID }
                guard !depots.isEmpty else { continue }
                // Only when the owner installs to this same folder, where the files are.
                guard let owner = app.sharedOwners[ownerID],
                      SteamInstallFiles.safeFolderName(owner.installDir).caseInsensitiveCompare(folderName) == .orderedSame else {
                    skipped += 1; continue
                }
                try AppManifestWriter.mergeOwnerManifest(ownerAppID: ownerID, ownerName: owner.name, ownerBuildID: owner.buildID,
                                                         installDir: folderName,
                                                         steamID: accountID, steamAppsPath: steamApps.path,
                                                         depots: depots)
                written += 1
            }
            SteamLog.event("[steam-shared-record] app=\(app.appID) shared=\(shared.count) owners-written=\(written) owners-skipped=\(skipped)")
        }
        try? FileManager.default.removeItem(at: journalDir)
        SteamLog.event("[steam-depot] install complete app=\(app.appID) bytes=\(prepared.totalUncompressed) seconds=\(Int(Date().timeIntervalSince(started)))")
        return installURL
    }

    /// Whether a previous attempt left resumable progress for this app.
    static func hasPartialDownload(appID: UInt32, steamApps: URL) -> Bool {
        let dir = steamApps.appendingPathComponent("downloading/\(appID)", isDirectory: true)
        return ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).contains { $0.hasSuffix(".journal") }
    }

    // MARK: - Plan

    struct DepotPlan: Sendable {
        let depotID: UInt32
        let manifestGID: UInt64
        let key: Data
        let manifest: DepotManifest
        let hosts: [String]
        let auth: [String: String]
        let declaredSize: UInt64
        let health: ContentHostHealth
    }

    struct WorkItem: Sendable {
        let file: Int
        let chunk: Int
        var key: UInt64 { UInt64(file) << 32 | UInt64(chunk) }
    }

    struct Prepared: Sendable {
        var paths: [[String]] = []       // per depot, per file: absolute path ("" = skipped)
        var pending: [[WorkItem]] = []   // per depot: chunks still to fetch
        var journals: [URL] = []
        var totalBytes: UInt64 = 0
        var doneBytes: UInt64 = 0
        var totalUncompressed: UInt64 = 0
        var remainingUncompressed: UInt64 = 0
        var fileCount = 0
    }

    private nonisolated static func prepare(plans: [DepotPlan], installURL: URL, journalDir: URL) throws -> Prepared {
        let fm = FileManager.default
        var result = Prepared()
        var folded: [String: String] = [:]   // lowercased relative dir -> first spelling
        let journalNames = Set(plans.map { "depot_\($0.depotID)_\($0.manifestGID).journal" })
        // A journal for an older manifest describes different file contents.
        for name in (try? fm.contentsOfDirectory(atPath: journalDir.path)) ?? [] where !journalNames.contains(name) {
            try? fm.removeItem(at: journalDir.appendingPathComponent(name))
        }
        for plan in plans {
            try Task.checkCancellation()
            let journalURL = journalDir.appendingPathComponent("depot_\(plan.depotID)_\(plan.manifestGID).journal")
            var paths: [String] = []
            var pending: [WorkItem] = []
            paths.reserveCapacity(plan.manifest.files.count)
            for (fileIndex, file) in plan.manifest.files.enumerated() {
                try Task.checkCancellation()
                // Symlinks (flag 0x200) have no Windows meaning here; skip them.
                guard file.flags & 0x200 == 0,
                      let relative = SteamInstallFiles.safeRelativePath(file.filename, folded: &folded) else {
                    if file.flags & 0x200 == 0 { SteamLog.trace("rejected manifest path in depot \(plan.depotID)") }
                    paths.append(""); continue
                }
                let url = installURL.appendingPathComponent(relative)
                if file.isDirectory {
                    try fm.createDirectory(at: url, withIntermediateDirectories: true)
                    paths.append(""); continue
                }
                try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                var before = stat()
                let hadContent = stat(url.path, &before) == 0 && before.st_size > 0
                try sizeFile(url.path, to: file.size)
                paths.append(url.path)
                result.fileCount += 1
                result.totalUncompressed += file.size
                var pendingBytes: UInt64 = 0
                for (chunkIndex, chunk) in file.chunks.enumerated() {
                    try Task.checkCancellation()
                    let item = WorkItem(file: fileIndex, chunk: chunkIndex)
                    result.totalBytes += UInt64(chunk.compressedSize)
                    if hadContent, chunkAlreadyPresent(chunk, path: url.path) {
                        result.doneBytes += UInt64(chunk.compressedSize)
                    } else {
                        pending.append(item)
                        pendingBytes += UInt64(chunk.uncompressedSize)
                    }
                }
                // Space already allocated to an existing file is reused; only
                // its growth needs new space (sparse new files need all of it).
                let beforeSize = hadContent ? UInt64(before.st_size) : 0
                result.remainingUncompressed += hadContent
                    ? (file.size > beforeSize ? file.size - beforeSize : 0) : pendingBytes
            }
            result.paths.append(paths)
            result.pending.append(pending)
            result.journals.append(journalURL)
        }
        return result
    }

    /// Create or resize a file to its manifest length. Existing bytes below
    /// that length are kept so resumed and updated installs reuse them.
    private nonisolated static func sizeFile(_ path: String, to size: UInt64) throws {
        let fd = open(path, O_WRONLY | O_CREAT, 0o644)
        guard fd >= 0 else { throw SteamError.chunkDownloadFailed("Cannot create a game file (errno \(errno)).") }
        defer { close(fd) }
        var info = stat()
        if fstat(fd, &info) == 0, UInt64(info.st_size) == size { return }
        guard ftruncate(fd, off_t(size)) == 0 else {
            throw SteamError.chunkDownloadFailed("Cannot size a game file (errno \(errno)).")
        }
    }

    // MARK: - Chunks

    /// A chunk's ID is the SHA-1 of its uncompressed bytes. When a file
    /// already had content (an update, or a resume without a journal), bytes
    /// that already match are kept instead of downloaded again.
    private nonisolated static func chunkAlreadyPresent(_ chunk: DepotManifest.ChunkEntry, path: String) -> Bool {
        let length = Int(chunk.uncompressedSize)
        guard chunk.sha.count == Int(CC_SHA1_DIGEST_LENGTH), length > 0,
              length <= ContentDecryptor.maximumChunkBytes else { return false }
        let fd = open(path, O_RDONLY)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var buffer = [UInt8](repeating: 0, count: length)
        let read = buffer.withUnsafeMutableBytes { pread(fd, $0.baseAddress, length, off_t(chunk.offset)) }
        guard read == length else { return false }
        var digest = [UInt8](repeating: 0, count: Int(CC_SHA1_DIGEST_LENGTH))
        _ = CC_SHA1(buffer, CC_LONG(length), &digest)
        return Data(digest) == chunk.sha
    }

    private nonisolated static func fetchChunk(_ chunk: DepotManifest.ChunkEntry, plan: DepotPlan,
                                               path: String, attempts: Int, seed: Int) async throws {
        var lastError: Error = SteamError.chunkDownloadFailed("No content server responded.")
        var tried = Set<String>()
        let limit = max(1, max(attempts, plan.hosts.count))
        for attempt in 0..<limit {
            try Task.checkCancellation()
            // A re-sort after a failure must not turn the retry index into a
            // second request to that failed server. Try every pool member
            // before beginning a second round, including the final fallback.
            if tried.count == Set(plan.hosts).count { tried.removeAll() }
            guard let host = plan.health.select(plan.hosts, excluding: tried, seed: seed) else { break }
            tried.insert(host)
            let data: Data, bytes: Int, seconds: Double
            do {
                (data, bytes, seconds) = try await verifiedChunk(chunk, plan: plan, host: host, probing: false)
            } catch is CancellationError {
                plan.health.recordCancellation(host)
                throw CancellationError()
            } catch {
                if Task.isCancelled {
                    plan.health.recordCancellation(host)
                    throw CancellationError()
                }
                lastError = error
                plan.health.recordFailure(host, reason: failureReason(error))
                SteamLog.trace("chunk attempt \(attempt + 1) failed: \(failureReason(error))")
                if attempt + 1 < limit {
                    try await Task.sleep(nanoseconds: 400_000_000 << min(attempt, 3))
                }
                continue
            }
            plan.health.recordSuccess(host, bytes: bytes, seconds: seconds)
            try Task.checkCancellation()
            // Local file errors are not evidence of a bad content server.
            try write(data, to: path, offset: chunk.offset)
            return
        }
        throw lastError
    }

    private nonisolated static func benchmarkChunk(_ chunk: DepotManifest.ChunkEntry, plan: DepotPlan,
                                                   path: String) async throws -> String? {
        var successful: [String] = []
        var reused = false
        for host in plan.hosts {
            try Task.checkCancellation()
            let data: Data, bytes: Int, seconds: Double
            do { (data, bytes, seconds) = try await verifiedChunk(chunk, plan: plan, host: host, probing: true) }
            catch {
                if Task.isCancelled || error is CancellationError || (error as? URLError)?.code == .cancelled {
                    throw CancellationError()
                }
                plan.health.recordFailure(host, reason: failureReason(error))
                let serverHostname = URL(string: host)?.host ?? "unavailable"
                let scheme = URL(string: host)?.scheme ?? "unknown"
                SteamLog.event("[steam-cdn] probe host=\(serverHostname) scheme=\(scheme) result=failed reason=\(failureReason(error))")
                continue
            }
            plan.health.recordSuccess(host, bytes: bytes, seconds: seconds)
            let serverHostname = URL(string: host)?.host ?? "unavailable"
            let scheme = URL(string: host)?.scheme ?? "unknown"
            let rate = seconds > 0 ? Double(bytes) / seconds : 0
            let measuredRate = Int64(min(rate.isFinite && rate > 0 ? rate : 0, Double(Int64.max / 2)))
            SteamLog.event("[steam-cdn] probe host=\(serverHostname) scheme=\(scheme) result=verified bytes-per-second=\(measuredRate)")
            successful.append(host)
            // Every valid probe contains the same SHA1-bound plaintext. Reuse
            // the first one; no array of probe payloads is retained in memory.
            if !reused {
                try Task.checkCancellation()
                try write(data, to: path, offset: chunk.offset)
                reused = true
            }
        }
        try Task.checkCancellation()
        if !reused {
            // A short probe is only a measurement window. Slow valid servers
            // still get the normal transfer timeout and retry policy.
            try await fetchChunk(chunk, plan: plan, path: path, attempts: 5, seed: 0)
            let selected = plan.health.order(plan.hosts, seed: 0).first
            SteamLog.event("[steam-cdn] stage=selected host=\(selected.flatMap { URL(string: $0)?.host } ?? "unavailable")")
            return selected
        }
        let selected = plan.health.order(successful, seed: 0).first
        SteamLog.event("[steam-cdn] stage=selected host=\(selected.flatMap { URL(string: $0)?.host } ?? "unavailable")")
        return selected
    }

    private nonisolated static func verifiedChunk(_ chunk: DepotManifest.ChunkEntry, plan: DepotPlan,
                                                  host: String, probing: Bool) async throws -> (Data, Int, Double) {
        let auth = URL(string: host)?.scheme == "https" ? plan.auth[host] ?? "" : ""
        let url = "\(host)/depot/\(plan.depotID)/chunk/\(chunk.shaHex)\(auth)"
        let started = DispatchTime.now().uptimeNanoseconds
        let encrypted = try await download(url, maximum: min(Int(chunk.compressedSize) + 2048,
            ContentDecryptor.maximumChunkBytes + 2048), probing: probing)
        let seconds = Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000_000
        try Task.checkCancellation()
        let data = try ContentDecryptor.processChunk(encryptedData: encrypted, depotKey: plan.key,
            expectedCRC: chunk.crc, expectedSize: Int(chunk.uncompressedSize))
        guard data.count == Int(chunk.uncompressedSize), chunk.sha.count == Int(CC_SHA1_DIGEST_LENGTH) else {
            throw SteamError.checksumMismatch
        }
        var digest = [UInt8](repeating: 0, count: Int(CC_SHA1_DIGEST_LENGTH))
        _ = data.withUnsafeBytes { CC_SHA1($0.baseAddress, CC_LONG(data.count), &digest) }
        guard Data(digest) == chunk.sha else { throw SteamError.checksumMismatch }
        try Task.checkCancellation()
        return (data, encrypted.count, seconds)
    }

    private nonisolated static func failureReason(_ error: Error) -> String {
        if let url = error as? URLError { return "url\(url.code.rawValue)" }
        if case SteamError.contentHTTPStatus(let status) = error { return "http\(status)" }
        if case SteamError.chunkDecodeFailed(let format) = error { return "decode-\(format)" }
        if case SteamError.checksumMismatch = error { return "checksum" }
        return "other"
    }

    private nonisolated static func write(_ data: Data, to path: String, offset: UInt64) throws {
        let fd = open(path, O_WRONLY)
        guard fd >= 0 else { throw SteamError.chunkDownloadFailed("Cannot open a game file (errno \(errno)).") }
        defer { close(fd) }
        try data.withUnsafeBytes { raw in
            var written = 0
            while written < raw.count {
                let n = pwrite(fd, raw.baseAddress! + written, raw.count - written, off_t(offset) + off_t(written))
                guard n > 0 else { throw SteamError.chunkDownloadFailed("Cannot write a game file (errno \(errno)).") }
                written += n
            }
        }
    }

    private nonisolated static func download(_ urlString: String, maximum: Int = 64 * 1024 * 1024,
                                            probing: Bool = false) async throws -> Data {
        guard let url = URL(string: urlString), let scheme = url.scheme, ["http", "https"].contains(scheme),
              url.user == nil, url.password == nil, url.fragment == nil, maximum > 0 else {
            throw SteamError.chunkDownloadFailed("Invalid content URL.")
        }
        if url.scheme == "http" {
            let parts = url.path.split(separator: "/")
            guard url.query == nil, parts.count == 4, parts[0] == "depot", UInt32(parts[1]) != nil,
                parts[2] == "chunk", parts[3].count == 40,
                parts[3].allSatisfy({ $0.isHexDigit }) else {
                throw SteamError.chunkDownloadFailed("HTTP is available only for credential-free chunks.")
            }
        }
        var request = URLRequest(url: url)
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let (bytes, response) = try await (probing ? probeHTTP : http).bytes(for: request)
        guard let status = (response as? HTTPURLResponse)?.statusCode, (200...299).contains(status) else {
            throw SteamError.contentHTTPStatus((response as? HTTPURLResponse)?.statusCode ?? 0)
        }
        guard response.expectedContentLength <= Int64(maximum) else { throw SteamError.checksumMismatch }
        var data = Data()
        data.reserveCapacity(min(maximum, 1024 * 1024))
        for try await byte in bytes {
            try Task.checkCancellation()
            guard data.count < maximum else { throw SteamError.checksumMismatch }
            data.append(byte)
        }
        return data
    }

    // MARK: - Manifest

    private func fetchManifest(depotID: UInt32, appID: UInt32, manifestGID: UInt64,
                               key: Data, hosts: [String], health: ContentHostHealth? = nil) async throws -> DepotManifest {
        let requestCode = try await manifestRequestCode(depotID: depotID, appID: appID, manifestGID: manifestGID)
        var lastError: Error = SteamError.manifestFetchFailed("No content server returned the manifest.")
        for host in (health?.order(hosts, seed: 0) ?? hosts) where URL(string: host)?.scheme == "https" {
            try Task.checkCancellation()
            let auth = await cdnAuthFragment(depotID: depotID, appID: appID, host: host)
            let code = requestCode == 0 ? "" : "/\(requestCode)"
            do {
                let raw = try await Self.download("\(host)/depot/\(depotID)/manifest/\(manifestGID)/5\(code)\(auth)")
                let cache = depotCache
                return try await Task.detached(priority: .userInitiated) {
                    let (manifest, payload) = try Self.parseManifestKeepingPayload(raw, depotID: depotID, manifestGID: manifestGID, key: key)
                    // Valve's client reads a depot's manifest from steamapps/depotcache
                    // when it prepares a per-user custom executable. Keep it for such depots.
                    if let cache, manifest.files.contains(where: { $0.flags & DepotManifest.customExecutableFlag != 0 }) {
                        try? FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
                        let file = cache.appendingPathComponent("\(depotID)_\(manifestGID).manifest")
                        let existing = (try? file.resourceValues(forKeys: [.fileSizeKey]))?.fileSize
                        if existing != payload.count { try? payload.write(to: file, options: .atomic) }
                    }
                    return manifest
                }.value
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                if Task.isCancelled || (error as? URLError)?.code == .cancelled { throw CancellationError() }
                lastError = error
                health?.recordFailure(host, reason: Self.failureReason(error))
                SteamLog.trace("manifest from a content server failed: \(error.localizedDescription)")
            }
        }
        throw lastError
    }

    private nonisolated static func parseManifest(_ raw: Data, depotID: UInt32, manifestGID: UInt64, key: Data) throws -> DepotManifest {
        try parseManifestKeepingPayload(raw, depotID: depotID, manifestGID: manifestGID, key: key).0
    }

    /// The manifest and the binary manifest bytes it was parsed from (unzipped, decrypted).
    private nonisolated static func parseManifestKeepingPayload(_ raw: Data, depotID: UInt32, manifestGID: UInt64,
                                                                key: Data) throws -> (DepotManifest, Data) {
        // Content servers deliver the manifest as a single-entry ZIP.
        let payload = raw.starts(with: [0x50, 0x4B]) ? try unzipSingleFile(raw) : raw
        if let plain = try? DepotManifest.parse(depotID: depotID, manifestGID: manifestGID, data: payload, depotKey: key),
           !plain.files.isEmpty {
            return (plain, payload)
        }
        // Older depots encrypt the whole manifest with the depot key.
        let decrypted = try ContentDecryptor.decryptChunk(encryptedData: payload, depotKey: key)
        let inflated = (try? ContentDecryptor.decompressChunk(compressedData: decrypted, expectedSize: 0)) ?? decrypted
        return (try DepotManifest.parse(depotID: depotID, manifestGID: manifestGID, data: inflated, depotKey: key), inflated)
    }

    /// steamapps/depotcache while an install runs.
    private var depotCache: URL?

    /// Extract the single deflated entry from a manifest ZIP. No zip lib needed —
    /// parse the local file header and inflate the raw deflate stream.
    nonisolated static func unzipSingleFile(_ data: Data) throws -> Data {
        let data = Data(data)  // zero-based indices
        guard data.count > 30,
              data[0] == 0x50, data[1] == 0x4B, data[2] == 0x03, data[3] == 0x04 else {
            throw SteamError.manifestFetchFailed("Not a zip")
        }
        func u16(_ o: Int) -> Int { Int(data[o]) | Int(data[o + 1]) << 8 }
        func u32(_ o: Int) -> Int { Int(data[o]) | Int(data[o+1]) << 8 | Int(data[o+2]) << 16 | Int(data[o+3]) << 24 }
        let method = u16(8)
        let compSize = u32(18)
        let uncompSize = u32(22)
        let nameLen = u16(26), extraLen = u16(28)
        let start = 30 + nameLen + extraLen
        guard start + compSize <= data.count, method == 0 || method == 8,
              uncompSize <= ContentDecryptor.maximumChunkBytes * 4 else {
            throw SteamError.manifestFetchFailed("Bad zip entry (method \(method))")
        }
        let entry = data.subdata(in: start..<(start + compSize))
        if method == 0 { return entry }

        // Zip stores raw deflate — inflate with zlib, negative windowBits
        var strm = z_stream()
        let cap = max(uncompSize, 1024)
        var out = Data(count: cap)
        var n = -1
        entry.withUnsafeBytes { src in
            out.withUnsafeMutableBytes { dst in
                strm.next_in = UnsafeMutablePointer(mutating: src.bindMemory(to: UInt8.self).baseAddress)
                strm.avail_in = UInt32(entry.count)
                strm.next_out = dst.bindMemory(to: UInt8.self).baseAddress
                strm.avail_out = UInt32(cap)
                guard inflateInit2_(&strm, -15, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) == Z_OK else { return }
                let r = inflate(&strm, Z_FINISH)
                inflateEnd(&strm)
                if r == Z_STREAM_END { n = Int(strm.total_out) }
            }
        }
        guard n >= 0 else { throw SteamError.decompressionFailed }
        out.count = n
        return out
    }

    // MARK: - Depot Key

    private func depotKey(depotID: UInt32, appID: UInt32) async throws -> Data {
        if let cached = depotKeys[depotID] { return cached }
        try await session.ensureConnected()

        var request = CMsgClientGetDepotDecryptionKey()
        request.depotID = depotID
        request.appID = appID

        let response = try await session.sendAndWait(
            eMsg: .clientGetDepotDecryptionKey,
            body: request.serialize(),
            responseEMsg: .clientGetDepotDecryptionKeyResponse,
            timeout: 15
        )

        let keyResponse = try CMsgClientGetDepotDecryptionKeyResponse.deserialize(from: response.body)
        // Steam only issues keys for depots this account owns.
        guard EResult(rawValue: UInt32(keyResponse.eresult))?.isSuccess == true,
              keyResponse.depotEncryptionKey.count == 32 else {
            SteamLog.event("[steam-depot] depot-key refused app=\(appID) depot=\(depotID) eresult=\(keyResponse.eresult)")
            throw SteamError.depotKeyNotFound(depotID)
        }

        depotKeys[depotID] = keyResponse.depotEncryptionKey
        return keyResponse.depotEncryptionKey
    }

    // MARK: - Manifest Request Code

    private func manifestRequestCode(depotID: UInt32, appID: UInt32, manifestGID: UInt64) async throws -> UInt64 {
        try await session.ensureConnected()
        var encoder = ProtobufEncoder()
        encoder.writeUInt32(fieldNumber: 1, value: appID)
        encoder.writeUInt32(fieldNumber: 2, value: depotID)
        encoder.writeUInt64(fieldNumber: 3, value: manifestGID)

        let responseData = try await session.callServiceMethod(
            method: .getManifestRequestCode,
            body: encoder.data,
            timeout: 15
        )

        var decoder = ProtobufDecoder(responseData)
        while let tag = try decoder.readTag() {
            if tag.fieldNumber == 1 {
                // manifest_request_code is fixed64 on the wire
                return tag.wireType == .fixed64 ? try decoder.readFixed64() : try decoder.readVarint()
            }
            try decoder.skip(wireType: tag.wireType)
        }

        return 0 // No request code needed (older depots)
    }

    // MARK: - CDN Discovery + Auth

    /// Content server discovery via the public Web API
    /// (IContentServerDirectoryService/GetServersForSteamPipe). No account
    /// token is attached; the directory does not require one.
    enum ServerEligibility: Equatable { case usable, noHTTPS, other }

    /// Directory fields as served by GetServersForSteamPipe: https_support
    /// ("mandatory", "optional", "unavailable"), use_as_proxy, and an optional
    /// allowed_app_ids restriction.
    nonisolated static func serverEligibility(_ server: [String: Any], appID: UInt32) -> ServerEligibility {
        if let https = (server["https_support"] as? String)?.lowercased(), https != "mandatory", https != "optional" {
            return .noHTTPS
        }
        if (server["use_as_proxy"] as? Bool) == true || (server["use_as_proxy"] as? NSNumber)?.boolValue == true {
            return .other
        }
        if let allowed = server["allowed_app_ids"] as? [Any], !allowed.isEmpty,
           !allowed.contains(where: { ($0 as? NSNumber)?.uint32Value == appID || ($0 as? String) == String(appID) }) {
            return .other
        }
        return .usable
    }

    private func contentServers(appID: UInt32) async throws -> (hosts: [String], manualAllowed: Set<String>) {
        let directoryCellID = session.cellID
        let nodes = try await SteamContentSourceDirectory.fetch(cellID: directoryCellID, appID: appID,
            viaConnection: { [weak self] cell, ip in
                guard let self else { throw CancellationError() }
                return try await self.session.contentDirectory(cellID: cell, ipOverride: ip)
            })
        let discovered = nodes.filter(\.downloadEligible).map(\.url)
        let alternatives = SteamContentSourceDirectory.transferFallbacks(cellID: directoryCellID, appID: appID).map(\.url)
        let hosts = Self.contentHostPool(discovered + alternatives, maximum: maxConcurrentChunks)
        let fallback = nodes.contains(where: \.isFallback)
        SteamLog.event("[steam-cdn] usable=\(hosts.count) bundled-fallback=\(fallback ? 1 : 0) secure-alternatives=\(alternatives.count)")
        return (hosts, Set(hosts))
    }

    nonisolated static func usableContentHost(_ host: String) -> Bool {
        SteamContentSourceDirectory.canonicalHost(host) != nil
    }

    /// Keep discovery order, but reserve one slot for the existing HTTPS
    /// fallback instead of dropping it behind a longer directory response.
    nonisolated static func contentHostPool(_ hosts: [String], maximum: Int) -> [String] {
        var seen = Set<String>()
        let unique = hosts.filter { seen.insert($0).inserted }
        return unique
    }

    /// Unified-service GetCDNAuthToken → "?auth=…" query fragment for this
    /// depot on this host. An empty token is normal outside regional edge
    /// networks. Cached per depot and host.
    private func cdnAuthFragment(depotID: UInt32, appID: UInt32, host: String) async -> String {
        guard URL(string: host)?.scheme == "https" else { return "" }
        let cacheKey = "\(depotID)|\(host)"
        if let cached = cdnAuthTokens[cacheKey] { return cached }

        // host_name must be the bare hostname, not the https:// URL
        let bareHost = URL(string: host)?.host ?? host

        var encoder = ProtobufEncoder()
        encoder.writeUInt32(fieldNumber: 1, value: depotID)  // depot_id = 1
        encoder.writeString(fieldNumber: 2, value: bareHost) // host_name = 2
        encoder.writeUInt32(fieldNumber: 3, value: appID)    // app_id = 3

        do {
            try await session.ensureConnected()
            let responseData = try await session.callServiceMethod(
                method: .getCDNAuthToken,
                body: encoder.data,
                timeout: 10
            )
            var decoder = ProtobufDecoder(responseData)
            var token = ""
            while let tag = try decoder.readTag() {
                if tag.fieldNumber == 1 { token = try decoder.readString() } else { try decoder.skip(wireType: tag.wireType) }
            }
            let fragment = token.isEmpty ? "" : (token.hasPrefix("?") || token.hasPrefix("&") ? token : "?auth=\(token)")
            cdnAuthTokens[cacheKey] = fragment
            return fragment
        } catch {
            SteamLog.trace("content authorization request failed; continuing without a token")
            cdnAuthTokens[cacheKey] = ""
            return ""
        }
    }
}

/// CDN authorization belongs to one origin. Never forward a signed content
/// URL to another host/port or follow an HTTPS downgrade during fallback.
final class ContentRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let destination = request.url,
              original.scheme == destination.scheme,
              original.host == destination.host,
              original.port == destination.port,
              destination.user == nil, destination.password == nil, destination.fragment == nil,
              destination.scheme != "http" || (destination.query == nil && destination.path == original.path) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

/// Per-install content-server health, based on verified chunk transfers.
/// Give unmeasured servers one request, then prefer observed transfer rates
/// adjusted for requests already in flight. Failed servers move to the back.
final class ContentHostHealth: @unchecked Sendable {
    private struct Sample {
        var failures = 0
        var inFlight = 0
        var successes = 0
        var bytesPerSecond: Double = 0
    }
    private let lock = NSLock()
    private var samples: [String: Sample] = [:]
    private var reported = 0
    private var reportedTransfers = 0
    private let manualHost: String?
    private let rememberedHost: String?
    private var manualFailed = false

    init(manualHost: String? = nil, rememberedHost: String? = nil) {
        self.manualHost = manualHost
        self.rememberedHost = rememberedHost
    }

    var manualFallbackNotice: String? {
        lock.lock(); defer { lock.unlock() }
        guard manualFailed, let manualHost, let host = URL(string: manualHost)?.host else { return nil }
        return "Selected CDN \(host) failed; using automatic fallback."
    }

    func verifiedSamples(cellID: UInt32) -> [SteamContentHostRecord] {
        lock.lock(); defer { lock.unlock() }
        return samples.compactMap { host, sample in
            guard sample.failures == 0, sample.successes > 0, sample.bytesPerSecond.isFinite, sample.bytesPerSecond > 0,
                  let hostname = URL(string: host)?.host else { return nil }
            return SteamContentHostRecord(host: hostname, cellID: cellID, bytesPerSecond: sample.bytesPerSecond,
                                          successes: sample.successes, updatedAt: Date(), scheme: URL(string: host)?.scheme)
        }
    }

    func failedHosts() -> [String] {
        lock.lock(); defer { lock.unlock() }
        return samples.compactMap { host, sample in
            sample.failures > 0 ? host : nil
        }
    }

    func order(_ hosts: [String], seed: Int) -> [String] {
        lock.lock(); defer { lock.unlock() }
        return ranked(hosts, seed: seed)
    }

    func select(_ hosts: [String], excluding tried: Set<String>, seed: Int) -> String? {
        lock.lock(); defer { lock.unlock() }
        guard let host = ranked(hosts.filter { !tried.contains($0) }, seed: seed).first else { return nil }
        samples[host, default: Sample()].inFlight += 1
        return host
    }

    // Caller holds lock; ordering and reservation must be one operation.
    private func ranked(_ hosts: [String], seed: Int) -> [String] {
        guard !hosts.isEmpty else { return hosts }
        let start = (seed % hosts.count + hosts.count) % hosts.count
        let rotated = (0..<hosts.count).map { hosts[($0 + start) % hosts.count] }
        return rotated.enumerated().sorted { left, right in
            let a = samples[left.element] ?? Sample(), b = samples[right.element] ?? Sample()
            if a.failures != b.failures { return a.failures < b.failures }
            if !manualFailed {
                let aManual = left.element == manualHost, bManual = right.element == manualHost
                if aManual != bManual { return aManual }
            }
            // Do not pile new chunks onto an unknown slow server while its
            // first measurement is pending and another server has succeeded.
            let aTier = a.successes == 0 ? (a.inFlight == 0 ? 0 : 2) : 1
            let bTier = b.successes == 0 ? (b.inFlight == 0 ? 0 : 2) : 1
            if aTier != bTier { return aTier < bTier }
            if aTier == 0 {
                let aRemembered = left.element == rememberedHost, bRemembered = right.element == rememberedHost
                if aRemembered != bRemembered { return aRemembered }
            }
            if aTier == 1 {
                let aRate = a.bytesPerSecond / Double(a.inFlight + 1)
                let bRate = b.bytesPerSecond / Double(b.inFlight + 1)
                if aRate != bRate { return aRate > bRate }
            } else if a.inFlight != b.inFlight { return a.inFlight < b.inFlight }
            return left.offset < right.offset
        }.map(\.element)
    }

    func recordFailure(_ host: String, reason: String) {
        lock.lock()
        var sample = samples[host] ?? Sample()
        sample.inFlight = max(0, sample.inFlight - 1)
        sample.failures += 1
        if host == manualHost { manualFailed = true }
        samples[host] = sample
        let report = sample.failures == 3 && reported < 8
        if report { reported += 1 }
        lock.unlock()
        if report {
            let server = URL(string: host)?.host ?? "unknown"
            DispatchQueue.main.async { SteamLog.event("[steam-cdn] host demoted host=\(server) reason=\(reason)") }
        }
    }

    func recordSuccess(_ host: String, bytes: Int, seconds: Double) {
        lock.lock()
        var sample = samples[host] ?? Sample()
        sample.inFlight = max(0, sample.inFlight - 1)
        sample.failures = max(0, sample.failures - 1)
        let rate = Double(bytes) / seconds
        let measured = bytes > 0 && seconds > 0 && seconds.isFinite && rate.isFinite
        if measured {
            sample.bytesPerSecond = sample.successes == 0 ? rate : sample.bytesPerSecond * 0.7 + rate * 0.3
            sample.successes += 1
        }
        samples[host] = sample
        let successes = sample.successes
        let report = measured && [1, 8, 32, 128].contains(successes) && reportedTransfers < 24
        if report { reportedTransfers += 1 }
        lock.unlock()
        if report {
            let server = URL(string: host)?.host ?? "unknown"
            let milliseconds = Int(min(seconds, 86_400) * 1000)
            DispatchQueue.main.async {
                SteamLog.event("[steam-cdn] transfer host=\(server) bytes=\(bytes) ms=\(milliseconds) samples=\(successes)")
            }
        }
    }

    func recordCancellation(_ host: String) {
        lock.lock(); defer { lock.unlock() }
        var sample = samples[host] ?? Sample()
        sample.inFlight = max(0, sample.inFlight - 1)
        samples[host] = sample
    }
}

/// Append-only progress record for one depot manifest. Lines are hexadecimal
/// work-item keys. The next attempt verifies local bytes against the manifest;
/// neither a recorded key nor a lost tail decides whether a chunk is fetched.
final class JournalWriter {
    private let handle: FileHandle
    private var buffer = ""
    private var pending = 0

    init(url: URL) throws {
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        handle = try FileHandle(forUpdating: url)
        // A previous attempt killed mid-write can leave a partial last line;
        // start on a fresh line so the first new key is not glued onto it.
        let end = handle.seekToEndOfFile()
        if end > 0 {
            handle.seek(toFileOffset: end - 1)
            if handle.readData(ofLength: 1) != Data([0x0A]) { buffer = "\n" }
            handle.seekToEndOfFile()
        }
    }

    func append(_ key: UInt64) {
        buffer += String(key, radix: 16) + "\n"
        pending += 1
        if pending >= 64 { flush() }
    }

    func flush() {
        guard !buffer.isEmpty else { return }
        handle.write(Data(buffer.utf8))
        buffer = ""; pending = 0
    }

    func close() {
        flush()
        try? handle.close()
    }

}
