// SPDX-License-Identifier: GPL-3.0-or-later
// Native Epic Launcher protocol implementation; see EpicCatalog.swift.

import Foundation
import Combine
import Security

@MainActor protocol EpicCredentialStore {
    func load() throws -> Data?
    func save(_ data: Data) throws
    func remove() throws
}

@MainActor final class EpicKeychainStore: EpicCredentialStore {
    private let defaults: UserDefaults
    private let signedOutKey = "madeira.epic.session.signed-out"

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "madeira.epic.session",
         kSecAttrAccount as String: "epic-refresh-session",
         kSecAttrSynchronizable as String: false]
    }
    func load() throws -> Data? {
        // A locked/unavailable Keychain must not resurrect a session after a
        // failed deletion. This flag contains no identity or credential.
        if defaults.bool(forKey: signedOutKey) { return nil }
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw EpicError.keychain(Int(status)) }
        return data
    }
    func save(_ data: Data) throws {
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        ]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let result = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
            guard result == errSecSuccess else { throw EpicError.keychain(Int(result)) }
        } else if status != errSecSuccess { throw EpicError.keychain(Int(status)) }
        defaults.removeObject(forKey: signedOutKey)
    }
    func remove() throws {
        defaults.set(true, forKey: signedOutKey)
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw EpicError.keychain(Int(status)) }
    }
}

@MainActor final class EpicAccount: ObservableObject {
    static let shared = EpicAccount(store: EpicKeychainStore(), http: .shared)
    @Published private(set) var signedIn = false
    @Published private(set) var displayName = ""
    @Published private(set) var accountID: String?
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var sessionGeneration: UInt64 = 0

    static var loginURL: URL {
        var redirect = URLComponents(string: "https://www.epicgames.com/id/api/redirect")!
        redirect.queryItems = [URLQueryItem(name: "clientId", value: EpicEndpoints.launcherClientID),
                               URLQueryItem(name: "responseType", value: "code")]
        var login = URLComponents(string: "https://www.epicgames.com/id/login")!
        login.queryItems = [URLQueryItem(name: "redirectUrl", value: redirect.url!.absoluteString)]
        return login.url!
    }

    static let accountURL = URL(string: "https://www.epicgames.com/account/personal")!

    private struct SavedSession: Codable, Sendable {
        var accountID: String
        var displayName: String
        var refreshToken: String
        var refreshExpires: Date?
    }
    private struct Session: Sendable {
        var saved: SavedSession
        var accessToken: String
        var expires: Date
    }
    private let store: any EpicCredentialStore
    let http: EpicHTTPClient
    private var session: Session?
    private var refreshTask: Task<Session, Error>?
    private var refreshIdentity: UUID?
    private var loginTask: Task<Session, Error>?

    init(store: any EpicCredentialStore, http: EpicHTTPClient) {
        self.store = store
        self.http = http
        do {
            if let data = try store.load() {
                let saved = try JSONDecoder().decode(SavedSession.self, from: data)
                guard !saved.refreshToken.isEmpty,
                      saved.refreshExpires == nil || saved.refreshExpires! > Date() else {
                    try store.remove()
                    return
                }
                session = Session(saved: saved, accessToken: "", expires: .distantPast)
                signedIn = true
                accountID = saved.accountID
                displayName = saved.displayName
            }
        } catch { self.error = EpicError.keychain(0).localizedDescription }
    }

    private static func authorizationCode(_ input: String) throws -> String {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.utf8.count <= 4096 else { throw EpicError.invalidCode }
        let code: String
        if trimmed.hasPrefix("{") {
            struct Page: Decodable { let authorizationCode: String; let errorCode: String? }
            guard let data = trimmed.data(using: .utf8),
                  !data.isEmpty else { throw EpicError.invalidCode }
            if let action = EpicError.correctiveAction(in: data) { throw action }
            guard let page = try? JSONDecoder().decode(Page.self, from: data), page.errorCode == nil else { throw EpicError.invalidCode }
            code = page.authorizationCode
        } else { code = trimmed }
        guard !code.isEmpty, code.utf8.count <= 512,
              code.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                  || (97...122).contains($0) || [45, 46, 95, 126].contains($0) }) else { throw EpicError.invalidCode }
        return code
    }

    private static func tokenRequest(grant: String, name: String, value: String) throws -> URLRequest {
        var request = try EpicEndpoints.request(host: EpicEndpoints.account, path: "/account/api/oauth/token")
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let basic = Data((EpicEndpoints.launcherClientID + ":" + EpicEndpoints.launcherClientValue).utf8).base64EncodedString()
        request.setValue("Basic " + basic, forHTTPHeaderField: "Authorization")
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        let values = [("grant_type", grant), (name, value), ("token_type", "eg1")]
        request.httpBody = Data(values.map { $0.0 + "=" + ($0.1.addingPercentEncoding(withAllowedCharacters: allowed) ?? "") }.joined(separator: "&").utf8)
        return request
    }

    private static func decodeSession(_ data: Data, expectedAccount: String? = nil) throws -> Session {
        struct Reply: Decodable {
            let access_token: String
            let refresh_token: String
            let account_id: String
            let displayName: String?
            let expires_in: Double
            let refresh_expires: Double?
        }
        let reply = try EpicCatalog.decode(Reply.self, data: data)
        guard !reply.access_token.isEmpty, reply.access_token.utf8.count <= 32768,
              !reply.refresh_token.isEmpty, reply.refresh_token.utf8.count <= 32768,
              reply.access_token.utf8.allSatisfy({ (33...126).contains($0) }),
              reply.refresh_token.utf8.allSatisfy({ (33...126).contains($0) }),
              reply.expires_in.isFinite, reply.expires_in > 0, reply.expires_in <= 7 * 86400,
              reply.displayName?.utf8.count ?? 0 <= 4096 else { throw EpicError.invalidResponse }
        _ = try EpicEndpoints.identifier(reply.account_id)
        if let expectedAccount, reply.account_id != expectedAccount { throw EpicError.accountChanged }
        let now = Date()
        let refreshExpires: Date?
        if let seconds = reply.refresh_expires {
            guard seconds.isFinite, seconds > 0, seconds <= 10 * 366 * 86400 else { throw EpicError.invalidResponse }
            refreshExpires = now.addingTimeInterval(seconds)
        } else { refreshExpires = nil }
        return Session(saved: SavedSession(accountID: reply.account_id, displayName: reply.displayName ?? "Epic player",
                                           refreshToken: reply.refresh_token, refreshExpires: refreshExpires),
                       accessToken: reply.access_token, expires: now.addingTimeInterval(reply.expires_in))
    }

    private func accept(_ result: Session) throws {
        try store.save(JSONEncoder().encode(result.saved))
        session = result
        accountID = result.saved.accountID
        displayName = result.saved.displayName
        signedIn = true
        error = nil
    }

    func login(code: String) async throws {
        let code = try Self.authorizationCode(code)
        signOut()
        if error != nil { throw EpicError.keychain(0) }
        let generation = sessionGeneration
        busy = true
        let http = http
        let task = Task {
            let request = try Self.tokenRequest(grant: "authorization_code", name: "code", value: code)
            return try Self.decodeSession(try await http.send(request, limit: 128 * 1024))
        }
        loginTask = task
        defer { if generation == sessionGeneration { busy = false; loginTask = nil } }
        do {
            let result = try await withTaskCancellationHandler(operation: {
                try await task.value
            }, onCancel: { task.cancel() })
            try Task.checkCancellation()
            guard generation == sessionGeneration else { throw EpicError.accountChanged }
            try accept(result)
        } catch {
            if generation == sessionGeneration, !(error is CancellationError) { self.error = error.localizedDescription }
            throw error
        }
    }

    func signOut() {
        sessionGeneration &+= 1
        refreshTask?.cancel()
        loginTask?.cancel()
        refreshTask = nil
        refreshIdentity = nil
        loginTask = nil
        session = nil
        signedIn = false
        accountID = nil
        displayName = ""
        busy = false
        error = nil
        do { try store.remove() }
        catch { self.error = EpicError.keychain(0).localizedDescription }
    }

    func accessToken() async throws -> String {
        try Task.checkCancellation()
        guard let session, signedIn else { throw EpicError.notSignedIn }
        if session.expires > Date().addingTimeInterval(60), !session.accessToken.isEmpty { return session.accessToken }
        let generation = sessionGeneration
        let identity: UUID
        let task: Task<Session, Error>
        if let active = refreshTask, let activeIdentity = refreshIdentity {
            task = active
            identity = activeIdentity
        } else {
            if let expiry = session.saved.refreshExpires, expiry <= Date() { signOut(); throw EpicError.notSignedIn }
            let http = http
            identity = UUID()
            task = Task { [weak self] in
                guard let self else { throw EpicError.notSignedIn }
                defer {
                    if self.refreshIdentity == identity {
                        self.refreshTask = nil
                        self.refreshIdentity = nil
                    }
                }
                do {
                    let request = try Self.tokenRequest(grant: "refresh_token", name: "refresh_token", value: session.saved.refreshToken)
                    let result = try Self.decodeSession(try await http.send(request, limit: 128 * 1024), expectedAccount: session.saved.accountID)
                    guard generation == self.sessionGeneration, self.refreshIdentity == identity,
                          self.signedIn, self.accountID == session.saved.accountID else { throw EpicError.accountChanged }
                    // OAuth may consume/rotate the refresh token even when the
                    // last waiting API caller has been canceled. Commit inside
                    // the shared task, which only logout/account change cancels.
                    try self.accept(result)
                    return result
                } catch {
                    if generation == self.sessionGeneration, self.refreshIdentity == identity,
                       case EpicError.http(let status) = error, status == 400 || status == 401 {
                        self.signOut()
                        throw EpicError.notSignedIn
                    }
                    throw error
                }
            }
            refreshTask = task
            refreshIdentity = identity
        }
        let result: Session
        do { result = try await task.value }
        catch { try Task.checkCancellation(); throw error }
        try Task.checkCancellation()
        guard generation == sessionGeneration, accountID == session.saved.accountID else { throw EpicError.accountChanged }
        return result.accessToken
    }

    func invalidateAccessToken(_ rejected: String) {
        guard var session, session.accessToken == rejected else { return }
        session.expires = .distantPast
        self.session = session
    }
}
