// SPDX-License-Identifier: GPL-3.0-or-later
// Native Epic Launcher protocol implementation; see EpicCatalog.swift.

import Foundation

@MainActor final class EpicAPI {
    static let shared = EpicAPI(account: .shared)
    private let account: EpicAccount
    private let http: EpicHTTPClient

    init(account: EpicAccount, http: EpicHTTPClient? = nil) {
        self.account = account
        self.http = http ?? account.http
    }

    private func check(_ generation: UInt64) throws {
        try Task.checkCancellation()
        guard generation == account.sessionGeneration, account.signedIn else { throw EpicError.accountChanged }
    }

    private func send(_ request: URLRequest, generation: UInt64, limit: Int = 16 * 1024 * 1024) async throws -> Data {
        try check(generation)
        let token = try await account.accessToken()
        try check(generation)
        var request = request
        request.setValue("bearer " + token, forHTTPHeaderField: "Authorization")
        let data: Data
        do { data = try await http.send(request, limit: limit) }
        catch EpicError.http(401) {
            // Only an explicit unauthenticated response may trigger one refresh;
            // network failures, other status codes, and one-time auth POSTs are
            // never replayed. Rejecting the exact token preserves a concurrent
            // caller's already refreshed access token.
            try check(generation)
            account.invalidateAccessToken(token)
            let refreshed = try await account.accessToken()
            try check(generation)
            request.setValue("bearer " + refreshed, forHTTPHeaderField: "Authorization")
            data = try await http.send(request, limit: limit)
        }
        try check(generation)
        return data
    }

    func games() async throws -> [EpicGame] {
        let generation = account.sessionGeneration
        let request = try EpicEndpoints.request(host: EpicEndpoints.launcher,
                                               path: "/launcher/api/public/assets/Windows",
                                               query: [URLQueryItem(name: "label", value: "Live")])
        let assets = try EpicCatalog.decode([EpicCatalog.Asset].self, data: try await send(request, generation: generation))
        guard assets.count <= 20000 else { throw EpicError.responseTooLarge }

        // Windows assets are the authoritative owned installable set. Library
        // pages add external-store titles without a downloadable Windows asset;
        // taking an intersection would hide valid legacy assets.
        var cursor: String?
        var cursors = Set<String>()
        var records: [String: EpicCatalog.LibraryPage.Record] = [:]
        var pages = 0
        repeat {
            var query = [URLQueryItem(name: "includeMetadata", value: "true")]
            if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
            let request = try EpicEndpoints.request(host: EpicEndpoints.library,
                                                   path: "/library/api/public/items", query: query)
            let page = try EpicCatalog.decode(EpicCatalog.LibraryPage.self, data: try await send(request, generation: generation))
            for item in page.records {
                if let name = item.appName, item.sandboxType != "PRIVATE" { records[name] = item }
            }
            guard records.count <= 20000 else { throw EpicError.responseTooLarge }
            cursor = page.responseMetadata.nextCursor
            if let next = cursor {
                guard !next.isEmpty, next.utf8.count <= 4096, cursors.insert(next).inserted else { throw EpicError.invalidResponse }
            }
            pages += 1
            guard pages <= 200 else { throw EpicError.responseTooLarge }
        } while cursor != nil

        var seen = Set<String>()
        var candidates = assets.map { ($0, true) }
        let assetNames = Set(assets.map(\.appName))
        for (name, record) in records where !assetNames.contains(name) && name != "1" {
            if let namespace = record.namespace, let catalog = record.catalogItemId, namespace != "ue" {
                candidates.append((EpicCatalog.Asset(appName: name, namespace: namespace, catalogItemId: catalog, buildVersion: nil), false))
            }
        }
        candidates = candidates.filter { $0.0.namespace != "ue" && seen.insert($0.0.appName).inserted }
        let result = try await withThrowingTaskGroup(of: EpicGame?.self) { group in
            var result: [EpicGame] = []
            var next = 0
            // Metadata requests are independent. Keep a small bounded window
            // rather than one request per owned game in serial or an unbounded
            // burst that competes with game downloads.
            for index in 0..<min(4, candidates.count) {
                let candidate = candidates[index]
                group.addTask { try await self.game(candidate.0, hasWindowsAsset: candidate.1, generation: generation) }
                next += 1
            }
            for try await game in group {
                if let game { result.append(game) }
                if next < candidates.count {
                    let candidate = candidates[next]
                    next += 1
                    group.addTask { try await self.game(candidate.0, hasWindowsAsset: candidate.1, generation: generation) }
                }
            }
            return result
        }
        try check(generation)
        return result.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }

    private func game(_ asset: EpicCatalog.Asset, hasWindowsAsset: Bool, generation: UInt64) async throws -> EpicGame? {
        try check(generation)
        let namespace = try EpicEndpoints.identifier(asset.namespace)
        let item = try EpicEndpoints.identifier(asset.catalogItemId)
        let request = try EpicEndpoints.request(host: EpicEndpoints.catalog,
            path: "/catalog/api/shared/namespace/\(namespace)/bulk/items",
            query: [URLQueryItem(name: "id", value: item),
                    URLQueryItem(name: "includeDLCDetails", value: "true"),
                    URLQueryItem(name: "includeMainGameDetails", value: "true"),
                    URLQueryItem(name: "country", value: Locale.current.region?.identifier ?? "US"),
                    URLQueryItem(name: "locale", value: Locale.current.identifier.replacingOccurrences(of: "_", with: "-"))])
        let catalog = try EpicCatalog.decode([String: EpicCatalog.Item].self, data: try await send(request, generation: generation))
        guard let metadata = catalog[item] else { throw EpicError.invalidResponse }
        let game = try metadata.game(asset: asset)
        return hasWindowsAsset || game?.launchRequirement != nil ? game : nil
    }

    func manifest(game: EpicGame) async throws -> EpicManifestLocation {
        let generation = account.sessionGeneration
        let namespace = try EpicEndpoints.identifier(game.namespace)
        let catalog = try EpicEndpoints.identifier(game.catalogItemID)
        let app = try EpicEndpoints.identifier(game.appName)
        let request = try EpicEndpoints.request(host: EpicEndpoints.launcher,
            path: "/launcher/api/public/assets/v2/platform/Windows/namespace/\(namespace)/catalogItem/\(catalog)/app/\(app)/label/Live")
        let data: Data
        do { data = try await send(request, generation: generation) }
        catch EpicError.invalidResponse {
            LogStore.shared.log("[epic-manifest] reason=before-parser")
            throw EpicError.invalidResponse
        }
        return try EpicCatalog.manifest(data: data, game: game,
                                       diagnostic: { LogStore.shared.log("[epic-manifest] reason=\($0)") })
    }

    func ownershipToken(game: EpicGame) async throws -> Data {
        let generation = account.sessionGeneration
        try check(generation)
        guard let accountID = account.accountID else { throw EpicError.notSignedIn }
        let owner = try EpicEndpoints.identifier(accountID)
        let namespace = try EpicEndpoints.identifier(game.namespace)
        let catalog = try EpicEndpoints.identifier(game.catalogItemID)
        var request = try EpicEndpoints.request(host: EpicEndpoints.ecommerce,
            path: "/ecommerceintegration/api/public/platforms/EPIC/identities/\(owner)/ownershipToken")
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
        // identifier() permits only unreserved form characters. The colon is
        // protocol punctuation, encoded explicitly rather than interpolating a URL.
        request.httpBody = Data("nsCatalogItemId=\(namespace)%3A\(catalog)".utf8)
        let data = try await send(request, generation: generation, limit: 64 * 1024)
        guard !data.isEmpty else { throw EpicError.invalidResponse }
        return data
    }

    func exchangeCode() async throws -> String {
        let generation = account.sessionGeneration
        let request = try EpicEndpoints.request(host: EpicEndpoints.account, path: "/account/api/oauth/exchange")
        struct Reply: Decodable { let code: String }
        let reply = try EpicCatalog.decode(Reply.self, data: try await send(request, generation: generation, limit: 16 * 1024))
        guard !reply.code.isEmpty, reply.code.utf8.count <= 512,
              reply.code.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                  || (97...122).contains($0) || [45, 46, 95, 126].contains($0) }) else { throw EpicError.invalidResponse }
        return reply.code
    }
}
