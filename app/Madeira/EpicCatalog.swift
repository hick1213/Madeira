// SPDX-License-Identifier: GPL-3.0-or-later
// Native implementation of the Launcher protocol documented by Legendary:
// https://github.com/legendary-gl/legendary/blob/master/legendary/api/egs.py
// Protocol facts and Epic's public Launcher OAuth client are referenced; no
// Legendary Python code is embedded and no Converter Exception is asserted.

import Foundation

struct EpicGame: Codable, Identifiable, Hashable, Sendable {
    var appName: String
    var namespace: String
    var catalogItemID: String
    var title: String
    var artworkURL: URL?
    var buildVersion: String?
    var launchRequirement: String?
    var requiresOwnershipToken: Bool
    var id: String { appName }

    init(appName: String, namespace: String, catalogItemID: String, title: String,
         artworkURL: URL? = nil, buildVersion: String? = nil,
         launchRequirement: String? = nil, requiresOwnershipToken: Bool = false) {
        self.appName = appName
        self.namespace = namespace
        self.catalogItemID = catalogItemID
        self.title = title
        self.artworkURL = artworkURL
        self.buildVersion = buildVersion
        self.launchRequirement = launchRequirement
        self.requiresOwnershipToken = requiresOwnershipToken
    }
}

struct EpicManifestLocation: Sendable {
    let url: URL
    let chunkBaseURLs: [URL]
    var sha1: String? = nil
    var alternateURLs: [URL] = []
}

/// The authenticated Launcher asset response supplies the allowed origins.
/// This checks URL structure and explicit local names, not DNS resolution.
enum EpicCDNURLPolicy {
    static func host(_ value: String) -> String? {
        var host = value.lowercased()
        if host.hasSuffix(".") { host.removeLast() }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard host.utf8.count <= 253, labels.count >= 2, let top = labels.last,
              top.count >= 2,
              top.utf8.allSatisfy({ (97...122).contains($0) }) || top.hasPrefix("xn--"),
              labels.allSatisfy({ label in
                  !label.isEmpty && label.utf8.count <= 63 && label.first != "-" && label.last != "-" &&
                  label.utf8.allSatisfy({ (97...122).contains($0) || (48...57).contains($0) || $0 == 45 })
              }) else { return nil }
        let localNames: Set<String> = ["localhost", "local", "internal", "localdomain", "lan", "home",
                                       "corp", "intranet", "arpa", "onion", "test", "invalid", "example"]
        guard !localNames.contains(String(top)) else { return nil }
        return host
    }

    static func isHTTPS(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https" && url.host.flatMap(host) != nil &&
        url.user == nil && url.password == nil && url.fragment == nil &&
        (url.port == nil || url.port == 443)
    }
}

enum EpicError: Error, LocalizedError, Equatable {
    case invalidCode, privacyPolicyRequired, notSignedIn, accountChanged, invalidResponse, network
    case responseTooLarge, unsupportedManifest, http(Int), keychain(Int)

    var errorDescription: String? {
        switch self {
        case .invalidCode: return "Paste the one-time authorization code from Epic's login page."
        case .privacyPolicyRequired: return "Epic requires privacy policy acceptance before sign-in. Open Epic account, review the policy and complete any prompts yourself, then open Epic login again for a new authorization code."
        case .notSignedIn: return "Sign in to Epic Games again."
        case .accountChanged: return "The Epic account changed. Try again with the current account."
        case .invalidResponse: return "Epic returned an unsupported or invalid response."
        case .network: return "Could not connect to Epic Games. Check your connection and try again."
        case .responseTooLarge: return "The Epic response exceeded the supported size."
        case .unsupportedManifest: return "This Epic download uses an encrypted or prerelease manifest that Madeira does not support yet."
        case .http(let status): return "Epic Games request failed (HTTP \(status))."
        case .keychain: return "Could not access the Epic Games session in this device's Keychain."
        }
    }

    static func correctiveAction(in data: Data) -> EpicError? {
        struct Reply: Decodable {
            struct Metadata: Decodable { let correctiveAction: String }
            let errorCode: String
            let metadata: Metadata
        }
        guard let reply = try? JSONDecoder().decode(Reply.self, from: data),
              reply.errorCode == "errors.com.epicgames.oauth.corrective_action_required",
              reply.metadata.correctiveAction == "PRIVACY_POLICY_ACCEPTANCE" else { return nil }
        // Ignore server messages, URLs and continuation values. Policy consent
        // remains an explicit interaction on Epic's own website.
        return .privacyPolicyRequired
    }
}

enum EpicEndpoints {
    static let account = "account-public-service-prod03.ol.epicgames.com"
    static let launcher = "launcher-public-service-prod06.ol.epicgames.com"
    static let catalog = "catalog-public-service-prod06.ol.epicgames.com"
    static let library = "library-service.live.use1a.on.epicgames.com"
    static let ecommerce = "ecommerceintegration-public-service-ecomprod02.ol.epicgames.com"
    static let hosts: Set<String> = [account, launcher, catalog, library, ecommerce]

    // These identify Epic's publicly distributed Launcher OAuth client. They
    // are not a Madeira app secret, password, or user's credential. Epic can
    // change this third-party integration protocol independently of Madeira.
    static let launcherClientID = "34a02cf8f4414e29b15921876da36f9a"
    static let launcherClientValue = "daafbccc737745039dffe53d94fc76cf"

    static func request(host: String, path: String,
                        query: [URLQueryItem] = []) throws -> URLRequest {
        guard hosts.contains(host), path.hasPrefix("/"), !path.contains("?"),
              !path.contains("#"), !path.contains("\\") else { throw EpicError.invalidResponse }
        var parts = URLComponents()
        parts.scheme = "https"
        parts.host = host
        parts.path = path
        if !query.isEmpty { parts.queryItems = query }
        guard let url = parts.url else { throw EpicError.invalidResponse }
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit",
                         forHTTPHeaderField: "User-Agent")
        return request
    }

    static func identifier(_ value: String) throws -> String {
        guard !value.isEmpty, value.utf8.count <= 256,
              value != ".", value != "..",
              value.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                  || (97...122).contains($0) || [45, 46, 95].contains($0) }) else {
            throw EpicError.invalidResponse
        }
        return value
    }
}

struct EpicHTTPResponse: Sendable {
    let status: Int
    let data: Data
}

/// Reject every redirect, including same-host ones: no bearer/Basic credential
/// is replayed to a URL selected by a response. CDN uses a separate client.
final class EpicNoRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

struct EpicHTTPClient: Sendable {
    typealias Sender = @Sendable (URLRequest, Int) async throws -> EpicHTTPResponse
    private let sender: Sender
    static let shared = EpicHTTPClient()

    init(sender: @escaping Sender) { self.sender = sender }

    init() {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 60
        let session = URLSession(configuration: config, delegate: EpicNoRedirectDelegate(), delegateQueue: nil)
        sender = { request, limit in
            let (bytes, response) = try await session.bytes(for: request)
            guard let response = response as? HTTPURLResponse else { throw EpicError.invalidResponse }
            guard response.expectedContentLength <= Int64(limit) else { throw EpicError.responseTooLarge }
            var data = Data()
            data.reserveCapacity(min(limit, 64 * 1024))
            for try await byte in bytes {
                if data.count & 4095 == 0 { try Task.checkCancellation() }
                guard data.count < limit else { throw EpicError.responseTooLarge }
                data.append(byte)
            }
            return EpicHTTPResponse(status: response.statusCode, data: data)
        }
    }

    func send(_ request: URLRequest, limit: Int = 16 * 1024 * 1024) async throws -> Data {
        try Task.checkCancellation()
        guard let url = request.url, url.scheme == "https", let host = url.host,
              EpicEndpoints.hosts.contains(host), url.user == nil, url.password == nil,
              url.port == nil || url.port == 443, limit > 0 else { throw EpicError.invalidResponse }
        do {
            let response = try await sender(request, limit)
            try Task.checkCancellation()
            guard response.data.count <= limit else { throw EpicError.responseTooLarge }
            guard (200..<300).contains(response.status) else {
                throw EpicError.correctiveAction(in: response.data) ?? .http(response.status)
            }
            return response.data
        } catch is CancellationError { throw CancellationError() }
        catch let error as EpicError { throw error }
        catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled { throw CancellationError() }
            // URLSession errors may include signed URLs or request details.
            throw EpicError.network
        }
    }
}

enum EpicCatalog {
    struct Asset: Decodable, Sendable {
        let appName: String
        let namespace: String
        let catalogItemId: String
        let buildVersion: String?
    }
    struct LibraryPage: Decodable {
        struct Record: Decodable {
            let appName: String?
            let namespace: String?
            let catalogItemId: String?
            let sandboxType: String?
        }
        struct Metadata: Decodable { let nextCursor: String? }
        let records: [Record]
        let responseMetadata: Metadata
    }
    struct Item: Decodable {
        struct Image: Decodable { let type: String; let url: String }
        struct Category: Decodable { let path: String }
        struct MainGame: Decodable { let id: String? }
        struct Attribute: Decodable { let value: String? }
        let title: String
        let keyImages: [Image]?
        let categories: [Category]?
        let mainGameItem: MainGame?
        let customAttributes: [String: Attribute]?

        var excluded: Bool {
            if mainGameItem != nil { return true }
            return (categories ?? []).contains {
                let path = $0.path.lowercased()
                return ["mods", "addons", "plugins", "engines"].contains { path == $0 || path.hasPrefix($0 + "/") }
            }
        }

        func game(asset: Asset) throws -> EpicGame? {
            guard !excluded, asset.namespace != "ue" else { return nil }
            _ = try EpicEndpoints.identifier(asset.appName)
            _ = try EpicEndpoints.identifier(asset.namespace)
            _ = try EpicEndpoints.identifier(asset.catalogItemId)
            guard !title.isEmpty, title.utf8.count <= 4096 else { throw EpicError.invalidResponse }
            let images = keyImages ?? []
            let image = images.first { ["DieselGameBox", "DieselGameBoxTall", "Thumbnail"].contains($0.type) } ?? images.first
            let artwork = image.flatMap { image -> URL? in
                guard let url = URL(string: image.url), url.scheme == "https", url.user == nil,
                      url.password == nil, let host = url.host,
                      host == "epicgames.com" || host.hasSuffix(".epicgames.com"),
                      url.port == nil || url.port == 443 else { return nil }
                return url
            }
            let attributes = customAttributes ?? [:]
            let provider = attributes["ThirdPartyManagedApp"]?.value ?? attributes["ThirdPartyManagedProvider"]?.value
            let thirdParty = provider.map { !$0.isEmpty && $0.lowercased() != "false" } ?? false
            let partner = attributes["partnerLinkType"]?.value ?? provider
            return EpicGame(appName: asset.appName, namespace: asset.namespace,
                            catalogItemID: asset.catalogItemId, title: title, artworkURL: artwork,
                            buildVersion: asset.buildVersion,
                            launchRequirement: thirdParty ? (partner ?? "another launcher") : nil,
                            requiresOwnershipToken: attributes["OwnershipToken"]?.value?.lowercased() == "true")
        }
    }

    static func decode<T: Decodable>(_ type: T.Type, data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw EpicError.invalidResponse }
    }

    private static func manifestDecodeReason(_ error: Error) -> String {
        let kind: String
        let path: [any CodingKey]
        switch error {
        case DecodingError.typeMismatch(_, let context): kind = "decode-type"; path = context.codingPath
        case DecodingError.valueNotFound(_, let context): kind = "decode-null"; path = context.codingPath
        case DecodingError.keyNotFound(let key, let context): kind = "decode-missing"; path = context.codingPath + [key]
        case DecodingError.dataCorrupted(let context): kind = "decode-corrupt"; path = context.codingPath
        default: return "decode-other:root"
        }
        let fields: Set<String> = ["elements", "appName", "hash", "isPreloaded", "secrets",
                                   "manifests", "uri", "queryParams", "name", "value"]
        var location = ""
        for key in path.prefix(12) {
            if key.intValue != nil { location += "[]" }
            else {
                if !location.isEmpty { location += "." }
                location += fields.contains(key.stringValue) ? key.stringValue : "field"
            }
        }
        return kind + ":" + (location.isEmpty ? "root" : location)
    }

    private static func manifestDiagnosticHost(_ host: String) -> String? {
        let value = host.lowercased()
        guard !value.isEmpty, value.utf8.count <= 253,
              value.utf8.allSatisfy({ (48...57).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 46 }) else { return nil }
        let labels = value.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 63 && $0.first != "-" && $0.last != "-" }) else { return nil }
        return value
    }

    static func manifest(data: Data, game: EpicGame, diagnostic: ((String) -> Void)? = nil) throws -> EpicManifestLocation {
        struct Reply: Decodable {
            struct Element: Decodable {
                struct Manifest: Decodable {
                    struct Parameter: Decodable { let name: String; let value: String }
                    let uri: String
                    let queryParams: [Parameter]?
                }
                let appName: String?
                let hash: String
                let isPreloaded: Bool?
                let secrets: [String: String]?
                let manifests: [Manifest]
            }
            let elements: [Element]
        }
        func invalid(_ reason: String) -> EpicError { diagnostic?(reason); return .invalidResponse }
        let reply: Reply
        do { reply = try JSONDecoder().decode(Reply.self, from: data) }
        catch {
            diagnostic?(manifestDecodeReason(error))
            throw EpicError.invalidResponse
        }
        guard reply.elements.count == 1, let element = reply.elements.first else { throw invalid("elements-count") }
        guard element.appName == nil || element.appName == game.appName else { throw invalid("asset-name-mismatch") }
        guard element.hash.utf8.count == 40, element.hash.utf8.allSatisfy({
                  (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0)
              }) else { throw invalid("hash-format") }
        guard element.isPreloaded != true else { diagnostic?("preloaded"); throw EpicError.unsupportedManifest }
        guard element.secrets?.isEmpty != false else { diagnostic?("manifest-secrets"); throw EpicError.unsupportedManifest }
        guard !element.manifests.isEmpty else { throw invalid("empty-manifests") }
        guard element.manifests.count <= 16 else { throw invalid("manifests-count") }
        var urls: [URL] = []
        for manifest in element.manifests {
                guard var parts = URLComponents(string: manifest.uri) else { throw invalid("url-shape") }
                guard let scheme = parts.scheme?.lowercased(), scheme == "https" || scheme == "http" else { throw invalid("url-scheme") }
                guard let host = parts.host, !host.isEmpty else { throw invalid("url-host") }
                guard parts.user == nil, parts.password == nil else { throw invalid("url-userinfo") }
                guard parts.fragment == nil else { throw invalid("url-fragment") }
                guard parts.port == nil || parts.port == (scheme == "http" ? 80 : 443) else { throw invalid("url-port") }
                // Production input comes only from EpicAPI.manifest's fixed,
                // authenticated HTTPS endpoint, already bound to this game and
                // its manifest digest. New vendors need no per-game host rule.
                guard let canonicalHost = EpicCDNURLPolicy.host(host) else {
                    let endpoint = manifestDiagnosticHost(host).map { scheme + "://" + $0 } ?? "unavailable"
                    throw invalid("cdn-host:" + endpoint)
                }
                parts.host = canonicalHost
                parts.scheme = "https"
                if parts.port == 80 { parts.port = nil }
                if let parameters = manifest.queryParams {
                    parts.queryItems = (parts.queryItems ?? []) + parameters.map { URLQueryItem(name: $0.name, value: $0.value) }
                }
                guard let url = parts.url else { throw invalid("manifest-url") }
                urls.append(url)
        }
        guard let url = urls.first, !urls.isEmpty else { throw invalid("empty-manifests") }
        var bases: [URL] = []
        for url in urls {
            var parts = URLComponents(url: url.deletingLastPathComponent(), resolvingAgainstBaseURL: false)!
            parts.query = nil
            guard let base = parts.url else { throw invalid("chunk-base") }
            if !bases.contains(base) { bases.append(base) }
        }
        return EpicManifestLocation(url: url, chunkBaseURLs: bases, sha1: element.hash.lowercased(),
                                    alternateURLs: Array(urls.dropFirst()))
    }
}
