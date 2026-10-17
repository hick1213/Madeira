// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 125hz
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
//
import Foundation
import CoreFoundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

struct SteamContentServer: Identifiable, Equatable, Sendable {
    enum DirectorySource: String, Sendable {
        case session
        case mainlandSupplement
        case bundledMainland
        case bundledHTTPS
    }
    enum FallbackReason: Sendable {
        case directoryUnavailable
        case noHTTPSCandidates
    }
    enum RefreshFailure: Equatable, Sendable {
        case httpStatus(Int)
        case urlError(Int)
        case invalidResponse
        case noHTTPSCandidates

        var message: String {
            switch self {
            case .httpStatus(let status): return "Steam directory returned HTTP \(status)."
            case .urlError(let code): return "Steam directory network error \(code)."
            case .invalidResponse: return "Steam returned an invalid directory response."
            case .noHTTPSCandidates: return "Steam's directory provided no eligible HTTPS nodes."
            }
        }
    }
    let host: String
    let type: String
    let cellID: UInt32
    let allowedAppIDs: [UInt32]
    var fallbackReason: FallbackReason?
    var cachedAt: Date?
    var refreshFailure: RefreshFailure?
    var httpsSupport = "mandatory"
    var downloadEligible = true
    var cannotDownloadReason: String?
    var directoryRow: Int?
    var directorySource: DirectorySource = .session
    var isFallback: Bool { fallbackReason != nil }
    var scheme: String { httpsSupport == "unavailable" ? "http" : "https" }
    var url: String { scheme + "://" + host }
    var id: String { directorySource.rawValue + ":" + (directoryRow.map { "\($0):\(url)" } ?? url) }
}

/// Every official directory row remains visible. Request eligibility is separate
/// from presentation, and manual choices can only reorder current eligible origins.
enum SteamContentSourceDirectory {
    static func canonicalHost(_ value: String) -> String? {
        let host = value.lowercased()
        guard host.count <= 253, host.utf8.allSatisfy({
            (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 46
        }) else { return nil }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2, labels.last?.utf8.first.map({ (97...122).contains($0) }) == true,
              labels.allSatisfy({ !$0.isEmpty && $0.count <= 63 && $0.first != "-" && $0.last != "-" }),
              !["localhost", "local", "localdomain", "internal", "corp", "private", "lan", "home", "test", "invalid", "example", "onion", "arpa"].contains(String(labels.last!)),
              host != "home.arpa", !host.hasSuffix(".home.arpa"),
              host != "example.com", !host.hasSuffix(".example.com"),
              host != "example.net", !host.hasSuffix(".example.net"),
              host != "example.org", !host.hasSuffix(".example.org") else { return nil }
        return host
    }

    static func canonicalOrigin(_ value: String) -> String? {
        guard let parts = URLComponents(string: value), let scheme = parts.scheme?.lowercased(),
              ["http", "https"].contains(scheme), let rawHost = parts.host, let host = canonicalHost(rawHost),
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty, parts.port == nil || parts.port == (scheme == "http" ? 80 : 443) else { return nil }
        return scheme + "://" + host
    }

    static func directoryURL(cellID: UInt32, mainlandSupplement: Bool = false) -> URL {
        var parts = URLComponents(string: "https://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/")!
        parts.queryItems = [URLQueryItem(name: "cell_id", value: mainlandSupplement ? "0" : String(cellID)),
                            URLQueryItem(name: "max_servers", value: "100")]
        if mainlandSupplement {
            // A public mainland geolocation override requests recommendations;
            // the addresses remain official Steam directory results.
            parts.queryItems?.append(URLQueryItem(name: "ip_override", value: "114.114.114.114"))
        }
        return parts.url!
    }

    static func isTrustedDirectoryURL(_ url: URL) -> Bool {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        return parts.scheme?.lowercased() == "https" && parts.host?.lowercased() == "api.steampowered.com" &&
            (parts.port == nil || parts.port == 443) && parts.user == nil && parts.password == nil &&
            parts.fragment == nil && parts.path == "/IContentServerDirectoryService/GetServersForSteamPipe/v1/"
    }

    final class RedirectPolicy: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
        func urlSession(_ session: URLSession, task: URLSessionTask,
                        willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                        completionHandler: @escaping (URLRequest?) -> Void) {
            guard let origin = task.originalRequest?.url, SteamContentSourceDirectory.isTrustedDirectoryURL(origin),
                  let destination = request.url, SteamContentSourceDirectory.isTrustedDirectoryURL(destination) else {
                completionHandler(nil)
                return
            }
            completionHandler(request)
        }
    }

    private static let http: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 8
        config.timeoutIntervalForResource = 10
        return URLSession(configuration: config, delegate: RedirectPolicy(), delegateQueue: nil)
    }()

    // Unrestricted HTTPS alternatives for directory outages and secure manifests.
    static let fallbackServers: [SteamContentServer] = [
        SteamContentServer(host: "cache2-hkg1.steamcontent.com", type: "SteamCache", cellID: 33, allowedAppIDs: [], fallbackReason: .directoryUnavailable, directorySource: .bundledHTTPS),
        SteamContentServer(host: "cache4-hkg1.steamcontent.com", type: "SteamCache", cellID: 33, allowedAppIDs: [], fallbackReason: .directoryUnavailable, directorySource: .bundledHTTPS),
        SteamContentServer(host: "fastly.cdn.steampipe.steamcontent.com", type: "CDN", cellID: 0, allowedAppIDs: [], fallbackReason: .directoryUnavailable, directorySource: .bundledHTTPS),
        SteamContentServer(host: "steampipe.akamaized.net", type: "CDN", cellID: 0, allowedAppIDs: [], fallbackReason: .directoryUnavailable, directorySource: .bundledHTTPS)
    ]

    // Previously verified official public mainland recommendations. These are
    // emergency candidates, not a claim about this device's region or speed.
    static let bundledMainlandServers: [SteamContentServer] = [
        SteamContentServer(host: "st.dl.eccdnx.com", type: "CDN", cellID: 0, allowedAppIDs: [],
            fallbackReason: .directoryUnavailable, httpsSupport: "unavailable", directorySource: .bundledMainland),
        SteamContentServer(host: "dl.steam.clngaa.com", type: "CDN", cellID: 0, allowedAppIDs: [],
            fallbackReason: .directoryUnavailable, httpsSupport: "unavailable", directorySource: .bundledMainland),
        SteamContentServer(host: "xz.pphimalayanrt.com", type: "CDN", cellID: 0, allowedAppIDs: [],
            fallbackReason: .directoryUnavailable, httpsSupport: "unavailable", directorySource: .bundledMainland)
    ]

    static func cachedServers(cellID: UInt32, appID: UInt32? = nil, cache: Cache = .shared) -> [SteamContentServer]? {
        cache.resolve(cellID: cellID, appID: appID, failure: nil)
    }

    static func transferFallbacks(cellID: UInt32, appID: UInt32, cache: Cache = .shared) -> [SteamContentServer] {
        // A reachable directory can recommend only regional HTTP partners.
        // Secure alternatives must also serve chunks, not just manifests.
        // Preserve the cached directory's empty/restricted authority and vetoes.
        cache.bundledAlternatives(cellID: cellID, appID: appID, requireMatchingAuthority: true)
            .filter { $0.url.hasPrefix("https://") }
    }

    static func fetch(cellID: UInt32, appID: UInt32? = nil, using session: URLSession? = nil,
                      cache: Cache = .shared,
                      viaConnection: (@MainActor @Sendable (UInt32, String?) async throws -> Data)? = nil) async throws -> [SteamContentServer] {
        let generation = cache.beginRefresh()
        let primary = try await requestDirectory(cellID: cellID, mainlandSupplement: false, using: session ?? http, viaConnection: viaConnection)
        try Task.checkCancellation()
        let mainland = try await requestDirectory(cellID: cellID, mainlandSupplement: true, using: session ?? http, viaConnection: viaConnection)
        try Task.checkCancellation()
        // No source is persisted until both requests have finished and cancellation
        // has been checked. Failures retain only that source's previous authority.
        try cache.accept(primary.data, mainland: mainland.data, cellID: cellID, generation: generation)
        let current = cache.resolve(cellID: cellID, appID: appID, failure: primary.failure,
                                    mainlandFailure: mainland.failure, generation: generation)
        if primary.failure != nil, mainland.failure != nil {
            var rows = current ?? []
            var seen = Set(rows.map(\.url))
            for server in cache.bundledAlternatives(cellID: cellID, appID: appID) where seen.insert(server.url).inserted {
                var fallback = server
                fallback.refreshFailure = server.directorySource == .bundledMainland ? mainland.failure : primary.failure
                rows.append(fallback)
            }
            try Task.checkCancellation()
            SteamLog.event("[steam-cdn-directory] resolved-rows=\(rows.count) bundled=\(rows.filter(\.isFallback).count)")
            return rows
        }
        if let current {
            try Task.checkCancellation()
            SteamLog.event("[steam-cdn-directory] resolved-rows=\(current.count) eligible=\(current.filter(\.downloadEligible).count)")
            return current
        }
        try Task.checkCancellation()
        return fallbackServers.map { server in
            var fallback = server
            fallback.refreshFailure = primary.failure ?? mainland.failure
            return fallback
        }
    }

    private static func requestDirectory(cellID: UInt32, mainlandSupplement: Bool, using session: URLSession,
        viaConnection: (@MainActor @Sendable (UInt32, String?) async throws -> Data)?)
        async throws -> (data: Data?, failure: SteamContentServer.RefreshFailure?) {
        var failure = SteamContentServer.RefreshFailure.invalidResponse
        let source = mainlandSupplement ? "mainland-supplement" : "session"
        if let viaConnection {
            try Task.checkCancellation()
            do {
                let data = try await viaConnection(mainlandSupplement ? 0 : cellID,
                    mainlandSupplement ? "114.114.114.114" : nil)
                try Task.checkCancellation()
                let rows = try parse(data, appID: nil)
                SteamLog.event("[steam-cdn-directory] source=\(source) transport=steam-cm rows=\(rows.count)")
                return (data, nil)
            } catch {
                if error is CancellationError || Task.isCancelled || (error as? URLError)?.code == .cancelled {
                    throw CancellationError()
                }
                SteamLog.event("[steam-cdn-directory] source=\(source) transport=steam-cm fallback=web-api")
            }
        }
        for attempt in 0..<2 {
            try Task.checkCancellation()
            do {
                let (data, response) = try await session.data(from: directoryURL(cellID: cellID, mainlandSupplement: mainlandSupplement))
                try Task.checkCancellation()
                SteamLog.event("[steam-cdn-directory] source=\(source) attempt=\(attempt + 1)/2 http-status=\((response as? HTTPURLResponse)?.statusCode ?? 0)")
                guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                    failure = (response as? HTTPURLResponse).map { .httpStatus($0.statusCode) } ?? .invalidResponse
                    throw URLError(.badServerResponse)
                }
                failure = .invalidResponse
                guard let finalURL = http.url, isTrustedDirectoryURL(finalURL) else { throw URLError(.cannotParseResponse) }
                _ = try parse(data, appID: nil)
                try Task.checkCancellation()
                return (data, nil)
            } catch {
                if error is CancellationError || Task.isCancelled || (error as? URLError)?.code == .cancelled {
                    throw CancellationError()
                }
                if let networkError = error as? URLError, networkError.code != .badServerResponse,
                   networkError.code != .cannotParseResponse { failure = .urlError(networkError.code.rawValue) }
                SteamLog.event("[steam-cdn-directory] source=\(source) attempt=\(attempt + 1)/2 url-error=\((error as? URLError)?.code.rawValue ?? 0)")
                if attempt == 0 { try await Task.sleep(nanoseconds: 200_000_000) }
            }
        }
        return (nil, failure)
    }

    static func parse(_ data: Data, appID: UInt32?) throws -> [SteamContentServer] {
        guard data.count <= 2 * 1024 * 1024,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let response = object["response"] as? [String: Any],
              let entries = response["servers"] as? [[String: Any]] else { throw URLError(.cannotParseResponse) }
        return entries.enumerated().map { index, entry in
            let rawHost = (entry["vhost"] as? String) ?? (entry["host"] as? String) ?? "(missing host)"
            let host = rawHost.lowercased()
            let type = (entry["type"] as? String) ?? "Unknown"
            let support = (entry["https_support"] as? String)?.lowercased() ?? "unknown"
            var reason: String?
            if canonicalHost(host) == nil { reason = "Unsafe or invalid server hostname." }
            else if !["SteamCache", "CDN"].contains(type) { reason = "Unsupported server type: \(type)." }
            else if !["mandatory", "optional", "unavailable"].contains(support) { reason = "Unsupported transport: \(support)." }
            else if (entry["use_as_proxy"] as? NSNumber)?.boolValue == true { reason = "Proxy servers are not supported." }
            else if (entry["steam_china_only"] as? NSNumber)?.boolValue == true { reason = "Restricted to Steam China." }
            var allowed: [UInt32] = []
            if let raw = entry["allowed_app_ids"] {
                if let ids = raw as? [Any] {
                    for id in ids {
                        let value: UInt32?
                        if let number = id as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                           number.doubleValue >= 0, number.doubleValue <= Double(UInt32.max),
                           number.doubleValue.rounded() == number.doubleValue { value = UInt32(number.doubleValue) }
                        else if let string = id as? String { value = UInt32(string) }
                        else { value = nil }
                        if let value { allowed.append(value) }
                        else { reason = reason ?? "Invalid app restriction metadata." }
                    }
                } else { reason = reason ?? "Invalid app restriction metadata." }
            }
            if let appID, !allowed.isEmpty, !allowed.contains(appID) { reason = reason ?? "Not available for app \(appID)." }
            let cell = (entry["cell_id"] as? NSNumber)?.uint32Value ?? 0
            return SteamContentServer(host: host, type: type, cellID: cell, allowedAppIDs: allowed,
                httpsSupport: support, downloadEligible: reason == nil, cannotDownloadReason: reason, directoryRow: index)
        }
    }

    /// v1 archives remain readable. New successful responses replace the complete
    /// authority, including HTTP, restricted, malformed and empty rows.
    final class Cache: @unchecked Sendable {
        static let shared = Cache(fileURL: FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Madeira/SteamContentDirectory/last-good.json"))
        private struct Archive: Codable {
            var version = 1
            var cellID: UInt32
            var lastGood: Data?
            var savedAt: Date?
            var latestAuthority: Data?
            var latestAuthorityAt: Date?
            var invalidated = false
            var vetoedHosts: [String] = []
            var mainlandDirectory: Data?
            var mainlandSavedAt: Date?
        }
        private let fileURL: URL
        private let lock = NSLock()
        private var archive: Archive?
        private var generation: UInt64 = 0
        private var acceptedPrimaryGeneration: UInt64 = 0
        private var acceptedMainlandGeneration: UInt64 = 0
        private var acceptedCellGeneration: UInt64 = 0

        init(fileURL: URL) {
            self.fileURL = fileURL
            if let size = try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 6 * 1024 * 1024,
               let data = try? Data(contentsOf: fileURL), data.count <= 6 * 1024 * 1024,
               let saved = try? JSONDecoder().decode(Archive.self, from: data), Self.valid(saved) { archive = saved }
        }

        private static func valid(_ saved: Archive) -> Bool {
            guard saved.version == 1, saved.vetoedHosts.allSatisfy({ canonicalHost($0) == $0 }) else { return false }
            if let raw = saved.lastGood {
                guard let date = saved.savedAt, validDate(date), (try? parse(raw, appID: nil)) != nil else { return false }
            } else if saved.savedAt != nil { return false }
            if let raw = saved.latestAuthority {
                guard let date = saved.latestAuthorityAt, validDate(date), (try? parse(raw, appID: nil)) != nil else { return false }
            } else if saved.latestAuthorityAt != nil || saved.invalidated { return false }
            if let raw = saved.mainlandDirectory {
                guard let date = saved.mainlandSavedAt, validDate(date), (try? parse(raw, appID: nil)) != nil else { return false }
            } else if saved.mainlandSavedAt != nil { return false }
            return saved.lastGood != nil || saved.latestAuthority != nil || saved.mainlandDirectory != nil
        }

        private static func validDate(_ date: Date) -> Bool {
            date.timeIntervalSince1970.isFinite && date.timeIntervalSince1970 >= 0 && date.timeIntervalSinceNow <= 300
        }

        fileprivate func beginRefresh() -> UInt64 {
            lock.lock(); defer { lock.unlock() }
            generation &+= 1
            return generation
        }

        fileprivate func accept(_ primary: Data?, mainland: Data?, cellID: UInt32, generation: UInt64) throws {
            lock.lock(); defer { lock.unlock() }
            try Task.checkCancellation()
            guard primary != nil || mainland != nil,
                  archive?.cellID == cellID || generation >= acceptedCellGeneration else { return }
            let sameCell = archive?.cellID == cellID
            var saved = sameCell ? archive! : Archive(cellID: cellID)
            if saved.latestAuthority != nil {
                saved.lastGood = saved.latestAuthority
                saved.savedAt = saved.latestAuthorityAt
                saved.latestAuthority = nil
                saved.latestAuthorityAt = nil
                saved.invalidated = false
                saved.vetoedHosts = []
            }
            var changed = !sameCell
            var nextPrimaryGeneration = sameCell ? acceptedPrimaryGeneration : 0
            var nextMainlandGeneration = sameCell ? acceptedMainlandGeneration : 0
            if let primary, !sameCell || generation >= acceptedPrimaryGeneration {
                changed = changed || saved.lastGood != primary || !saved.vetoedHosts.isEmpty
                if saved.lastGood != primary { saved.savedAt = Date() }
                saved.lastGood = primary
                saved.vetoedHosts = []
                nextPrimaryGeneration = generation
            }
            if let mainland, !sameCell || generation >= acceptedMainlandGeneration {
                changed = changed || saved.mainlandDirectory != mainland
                if saved.mainlandDirectory != mainland { saved.mainlandSavedAt = Date() }
                saved.mainlandDirectory = mainland
                nextMainlandGeneration = generation
            }
            try Task.checkCancellation()
            archive = saved
            acceptedPrimaryGeneration = nextPrimaryGeneration
            acceptedMainlandGeneration = nextMainlandGeneration
            acceptedCellGeneration = max(acceptedCellGeneration, generation)
            guard changed else { return }
            do {
                let encoded = try JSONEncoder().encode(saved)
                guard encoded.count <= 6 * 1024 * 1024 else { return }
                try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                try encoded.write(to: fileURL, options: .atomic)
            } catch { SteamLog.event("[steam-cdn-directory] cache-write=failed") }
        }

        fileprivate func bundledAlternatives(cellID: UInt32, appID: UInt32?, requireMatchingAuthority: Bool = false) -> [SteamContentServer] {
            lock.lock(); defer { lock.unlock() }
            // Transfer augmentation follows one completed discovery. A newer
            // refresh for another cell must not turn its lost authority into
            // permission to append unrestricted nodes.
            if requireMatchingAuthority, archive?.cellID != cellID { return [] }
            let saved = archive?.cellID == cellID ? archive : nil
            let primary = saved?.latestAuthority ?? saved?.lastGood
            let mainland = saved?.mainlandDirectory
            let primaryRows = primary.flatMap { try? parse($0, appID: appID) }
            let mainlandRows = mainland.flatMap { try? parse($0, appID: appID) }
            var denied = Set(((primaryRows ?? []) + (mainlandRows ?? [])).filter { !$0.downloadEligible }.map(\.host))
            denied.formUnion(saved?.vetoedHosts ?? [])
            func permitted(_ pool: [SteamContentServer], rows: [SteamContentServer]?) -> [SteamContentServer] {
                if let rows, !rows.contains(where: \.downloadEligible) { return [] }
                return pool.filter { !denied.contains($0.host) }
            }
            return permitted(bundledMainlandServers, rows: mainlandRows) + permitted(fallbackServers, rows: primaryRows)
        }

        fileprivate func resolve(cellID: UInt32, appID: UInt32?, failure: SteamContentServer.RefreshFailure?,
                                 mainlandFailure: SteamContentServer.RefreshFailure? = nil,
                                 generation: UInt64? = nil) -> [SteamContentServer]? {
            lock.lock(); defer { lock.unlock() }
            guard let saved = archive, saved.cellID == cellID else { return nil }
            var rows: [SteamContentServer] = []
            let primary = saved.latestAuthority ?? saved.lastGood
            if let primary, let parsed = try? parse(primary, appID: appID) {
                rows += parsed.map { server in
                    var row = server
                    if saved.latestAuthority == nil, saved.vetoedHosts.contains(server.host) {
                        row.downloadEligible = false
                        row.cannotDownloadReason = "Previously rejected by Steam's directory."
                    }
                    row.cachedAt = generation != nil && generation == acceptedPrimaryGeneration && failure == nil
                        ? nil : saved.latestAuthorityAt ?? saved.savedAt
                    row.refreshFailure = failure
                    return row
                }
            }
            if let mainland = saved.mainlandDirectory, let parsed = try? parse(mainland, appID: appID) {
                rows += parsed.map { server in
                    var row = server
                    row.directorySource = .mainlandSupplement
                    row.cachedAt = generation != nil && generation == acceptedMainlandGeneration && mainlandFailure == nil
                        ? nil : saved.mainlandSavedAt
                    row.refreshFailure = mainlandFailure
                    return row
                }
            }
            // An empty supplementary recommendation alone cannot declare an
            // unavailable primary directory empty or hide its bundled fallback.
            return primary == nil && rows.isEmpty ? nil : rows
        }
    }

    static func prioritize(_ hosts: [String], selectedHost: String?) -> [String] {
        guard let selected = manualURL(selectedHost: selectedHost, allowed: Set(hosts)),
              let index = hosts.firstIndex(of: selected) else { return hosts }
        var result = hosts
        let moved = result.remove(at: index)
        result.insert(moved, at: 0)
        return result
    }

    static func manualURL(selectedHost: String?, allowed: Set<String>) -> String? {
        guard let selectedHost, let host = canonicalHost(selectedHost) else { return nil }
        let matching = allowed.filter { origin in
            canonicalOrigin(origin) == origin && URLComponents(string: origin)?.host == host
        }
        return matching.contains("https://" + host) ? "https://" + host : matching.sorted().first
    }
}

struct SteamContentHostRecord: Codable, Equatable, Sendable {
    let host: String
    let cellID: UInt32
    let bytesPerSecond: Double
    let successes: Int
    let updatedAt: Date
    var scheme: String? = nil
    var url: String { (scheme ?? "https") + "://" + host }
}

/// Stores public origins and verified rates, never account/depot credentials.
/// Automatic history expires and is separated by Steam's recommended cell.
final class SteamContentSourceStore: @unchecked Sendable {
    static let shared = SteamContentSourceStore()
    private static let key = "madeira.steam.download-sources.v1"
    private static let maximumRecords = 512
    private static let maximumArchiveBytes = 512 * 1024
    private struct Archive: Codable {
        var version = 1
        var selectedHost: String?
        var records: [SteamContentHostRecord] = []
    }
    private let defaults: UserDefaults
    private let lock = NSLock()
    private var archive: Archive

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let data = defaults.data(forKey: Self.key), data.count <= Self.maximumArchiveBytes,
           let saved = try? JSONDecoder().decode(Archive.self, from: data), saved.version == 1 {
            archive = saved
            archive.selectedHost = saved.selectedHost.flatMap(SteamContentSourceDirectory.canonicalHost)
            archive.records = Array(saved.records.filter { Self.valid($0, now: Date()) }.prefix(Self.maximumRecords))
        } else { archive = Archive() }
    }

    var selectedHost: String? { lock.lock(); defer { lock.unlock() }; return archive.selectedHost }

    @discardableResult func select(_ host: String?) -> Bool {
        let normalized = host.flatMap(SteamContentSourceDirectory.canonicalHost)
        guard host == nil || normalized != nil else { return false }
        lock.lock(); defer { lock.unlock() }
        archive.selectedHost = normalized
        save()
        return true
    }

    func history(for host: String, cellID: UInt32, scheme: String = "https") -> SteamContentHostRecord? {
        lock.lock(); defer { lock.unlock() }
        return archive.records.first { $0.host == host && ($0.scheme ?? "https") == scheme && $0.cellID == cellID && Self.valid($0, now: Date()) }
    }

    func rememberedHost(cellID: UInt32, available: [String]) -> String? {
        lock.lock(); defer { lock.unlock() }
        return archive.records.filter {
            $0.cellID == cellID && available.contains($0.url) && Self.valid($0, now: Date())
        }.max { $0.bytesPerSecond < $1.bytesPerSecond }?.url
    }

    func remember(_ records: [SteamContentHostRecord], cellID: UInt32) {
        lock.lock(); defer { lock.unlock() }
        let now = Date()
        archive.records.removeAll { !Self.valid($0, now: now) }
        for record in records where record.cellID == cellID && Self.valid(record, now: now) {
            archive.records.removeAll { $0.cellID == cellID && $0.url == record.url }
            archive.records.append(record)
        }
        archive.records.sort { $0.updatedAt > $1.updatedAt }
        archive.records = Array(archive.records.prefix(Self.maximumRecords))
        save()
    }

    func forget(hosts: [String], cellID: UInt32) {
        lock.lock(); defer { lock.unlock() }
        archive.records.removeAll { $0.cellID == cellID && (hosts.contains($0.url) || hosts.contains($0.host)) }
        save()
    }

    private static func valid(_ record: SteamContentHostRecord, now: Date) -> Bool {
        let age = now.timeIntervalSince(record.updatedAt)
        return SteamContentSourceDirectory.canonicalOrigin(record.url) == record.url &&
            record.bytesPerSecond.isFinite && record.bytesPerSecond > 0 && record.successes > 0 &&
            age >= -300 && age <= 7 * 86400
    }

    private func save() {
        if let data = try? JSONEncoder().encode(archive), data.count <= Self.maximumArchiveBytes { defaults.set(data, forKey: Self.key) }
    }
}
