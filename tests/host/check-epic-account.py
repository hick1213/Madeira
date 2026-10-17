#!/usr/bin/env python3
"""Compile and exercise the complete native Epic account/API sources.

Synthetic replies and an in-memory credential store only; never contacts Epic,
reads user credentials, or changes Steam. Artifacts stay on the external disk.
"""

from pathlib import Path
import argparse
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "app/Build/Products/IPAInspection/epic-privacy-login-tests"

HARNESS = r'''
import Foundation
import Combine

// Diagnostic boundary only: no production UI/device logger or credentials.
@MainActor final class LogStore {
    static let shared = LogStore()
    var messages: [String] = []
    func log(_ message: String) { messages.append(message) }
}

func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw Failure(message: message) }
}
struct Failure: Error { var message: String }
@MainActor final class MemoryStore: EpicCredentialStore {
    var data: Data?
    var saves = 0
    var removals = 0
    func load() throws -> Data? { data }
    func save(_ value: Data) throws { data = value; saves += 1 }
    func remove() throws { data = nil; removals += 1 }
}
actor Replies {
    var requests: [URLRequest] = []
    var handler: @Sendable (URLRequest) async throws -> (Int, String)
    init(_ handler: @escaping @Sendable (URLRequest) async throws -> (Int, String)) { self.handler = handler }
    func send(_ request: URLRequest, _ limit: Int) async throws -> EpicHTTPResponse {
        requests.append(request)
        let (status, body) = try await handler(request)
        return EpicHTTPResponse(status: status, data: Data(body.utf8))
    }
    var count: Int { requests.count }
    func all() -> [URLRequest] { requests }
}
actor Gate {
    var entered = false
    var opened = false
    var entryWaiters: [CheckedContinuation<Void, Never>] = []
    var blocked: [CheckedContinuation<Void, Never>] = []
    func stop() async {
        if opened { return }
        entered = true
        entryWaiters.forEach { $0.resume() }; entryWaiters.removeAll()
        await withCheckedContinuation { blocked.append($0) }
    }
    func waitForEntry() async {
        if entered { return }
        await withCheckedContinuation { entryWaiters.append($0) }
    }
    func open() { opened = true; blocked.forEach { $0.resume() }; blocked.removeAll() }
}
actor RejectedAccess {
    var tokens = 0
    var exchanges = 0
    let alwaysReject: Bool
    init(alwaysReject: Bool = false) { self.alwaysReject = alwaysReject }
    func send(_ request: URLRequest, _ limit: Int) throws -> EpicHTTPResponse {
        if request.url!.path.hasSuffix("/oauth/token") {
            tokens += 1
            let data = tokens == 1 ? tokenJSON : tokenJSON.replacingOccurrences(of: "synthetic-access", with: "refreshed-access")
            return EpicHTTPResponse(status: 200, data: Data(data.utf8))
        }
        exchanges += 1
        if alwaysReject || request.value(forHTTPHeaderField: "Authorization") == "bearer synthetic-access" {
            return EpicHTTPResponse(status: 401, data: Data())
        }
        return EpicHTTPResponse(status: 200, data: Data(#"{"code":"fresh-exchange"}"#.utf8))
    }
    func counts() -> (Int, Int) { (tokens, exchanges) }
}
actor RotatingRefresh {
    let gate: Gate
    var validRefresh = "synthetic-refresh"
    var calls = 0
    init(gate: Gate) { self.gate = gate }
    func send(_ request: URLRequest, _ limit: Int) async throws -> EpicHTTPResponse {
        calls += 1
        let body = String(data: request.httpBody!, encoding: .utf8)!
        guard body.contains("refresh_token=" + validRefresh) else {
            return EpicHTTPResponse(status: 400, data: Data())
        }
        await gate.stop()
        validRefresh = calls == 1 ? "rotated-refresh-one" : "rotated-refresh-two"
        return EpicHTTPResponse(status: 200, data: Data(tokenJSON.replacingOccurrences(of: "synthetic-refresh", with: validRefresh).utf8))
    }
}
let tokenJSON = #"{"access_token":"synthetic-access","refresh_token":"synthetic-refresh","account_id":"account-one","displayName":"Player","expires_in":3600,"refresh_expires":86400}"#
let assetJSON = #"[{"appName":"game-one","namespace":"namespace-one","catalogItemId":"catalog-one","buildVersion":"v1"},{"appName":"game-one","namespace":"namespace-one","catalogItemId":"catalog-one","buildVersion":"v1"},{"appName":"dlc-one","namespace":"namespace-one","catalogItemId":"dlc-catalog"}]"#
let catalogJSON = #"{"catalog-one":{"title":"One","categories":[{"path":"games"}],"keyImages":[{"type":"DieselGameBox","url":"https://cdn.epicgames.com/one.jpg"}]},"dlc-catalog":{"title":"DLC","mainGameItem":{"id":"catalog-one"},"categories":[{"path":"addons"}]}}"#
@main struct Run {
    @MainActor static func main() async {
        do { try await run() }
        catch let error as Failure { print("FAIL: " + error.message); exit(1) }
        catch { print("FAIL: unexpected native error type"); exit(1) }
    }
    @MainActor static func run() async throws {
        var checks = 0
        func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
            try expect(value(), message); checks += 1
        }
        let store = MemoryStore()
        let replies = Replies { request in
            if request.url!.path.hasSuffix("/oauth/token") { return (200, tokenJSON) }
            throw Failure(message: "unexpected endpoint")
        }
        let client = EpicHTTPClient(sender: { request, limit in try await replies.send(request, limit) })
        let account = EpicAccount(store: store, http: client)
        try check(!account.signedIn, "initial signed-out")
        try await account.login(code: #"{"authorizationCode":"synthetic-code"}"#)
        try check(account.signedIn && account.accountID == "account-one" && account.displayName == "Player", "login decoded")
        let saved = String(data: store.data!, encoding: .utf8)!
        try check(saved.contains("synthetic-refresh") && !saved.contains("synthetic-access") && !saved.contains("synthetic-code"), "refresh only persistence")
        // Privacy corrective-action JSON is not an authorization code. It must
        // be classified before signing out, transport, or Keychain mutation.
        let privacyJSON = #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","message":"untrusted private server message","metadata":{"correctiveAction":"PRIVACY_POLICY_ACCEPTANCE","continuation":"dummy-private-continuation","url":"https://evil.example/policy"}}"#
        let privacyGeneration = account.sessionGeneration
        let privacySaved = store.data
        let privacyRemovals = store.removals
        let privacySaves = store.saves
        do { try await account.login(code: privacyJSON); throw Failure(message: "privacy JSON accepted as a code") }
        catch let error as EpicError {
            try check(error == .privacyPolicyRequired && error.localizedDescription.lowercased().contains("privacy policy"), "privacy JSON needs actionable policy guidance")
            try check(!error.localizedDescription.contains("dummy-private") && !error.localizedDescription.contains("untrusted") && !error.localizedDescription.contains("evil.example"), "privacy details never echoed")
        }
        let privacyRequests = await replies.count
        try check(privacyRequests == 1, "privacy JSON performs no network or acceptance request")
        try check(account.signedIn && account.accountID == "account-one" && account.sessionGeneration == privacyGeneration,
                  "privacy JSON preserves the current account")
        try check(store.data == privacySaved && store.removals == privacyRemovals && store.saves == privacySaves,
                  "privacy JSON preserves the exact persisted session")
        let combinedPrivacyJSON = privacyJSON.dropLast() + #", "authorizationCode":"synthetic-code"}"#
        do { try await account.login(code: String(combinedPrivacyJSON)); throw Failure(message: "privacy JSON code field bypassed classification") }
        catch EpicError.privacyPolicyRequired { checks += 1 }
        let minimalPrivacyJSON = #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","metadata":{"correctiveAction":"PRIVACY_POLICY_ACCEPTANCE"}}"#
        do { try await account.login(code: minimalPrivacyJSON); throw Failure(message: "privacy response required an unused continuation") }
        catch EpicError.privacyPolicyRequired { checks += 1 }
        for badPrivacyJSON in [
            privacyJSON.replacingOccurrences(of: "PRIVACY_POLICY_ACCEPTANCE", with: "EMAIL_VERIFICATION"),
            privacyJSON.replacingOccurrences(of: "PRIVACY_POLICY_ACCEPTANCE", with: "privacy_policy_acceptance"),
            privacyJSON.replacingOccurrences(of: "oauth.corrective_action_required", with: "oauth.unknown_error"),
            #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","metadata":{}}"#,
            #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","metadata":{"correctiveAction":17}}"#,
            #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","metadata":[]}"#,
            privacyJSON.replacingOccurrences(of: "PRIVACY_POLICY_ACCEPTANCE", with: "UNKNOWN_ACTION").dropLast() + #", "authorizationCode":"synthetic-code"}"#,
            #"{"errorCode":17,"authorizationCode":"synthetic-code"}"#,
            "{" + String(repeating: " ", count: 4096) + privacyJSON.dropFirst()
        ] {
            do { try await account.login(code: String(badPrivacyJSON)); throw Failure(message: "unknown or malformed privacy JSON accepted") }
            catch EpicError.invalidCode { checks += 1 }
        }
        let invalidPrivacyRequests = await replies.count
        try check(invalidPrivacyRequests == 1 && account.sessionGeneration == privacyGeneration && store.data == privacySaved,
                  "all rejected page JSON preserves account and bypasses transport")

        for status in [400, 401, 403, 409] {
            let privacyReplies = Replies { _ in (status, privacyJSON) }
            let privacyHTTPAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: {
                try await privacyReplies.send($0, $1)
            }))
            do { try await privacyHTTPAccount.login(code: "synthetic-code"); throw Failure(message: "privacy HTTP response accepted") }
            catch EpicError.privacyPolicyRequired { checks += 1 }
            let privacyHTTPRequests = await privacyReplies.all()
            try check(privacyHTTPRequests.count == 1 && privacyHTTPRequests[0].httpMethod == "POST"
                      && privacyHTTPRequests[0].url!.path == "/account/api/oauth/token", "privacy HTTP performs one token POST without replay or policy acceptance")
            try check(privacyHTTPAccount.error == EpicError.privacyPolicyRequired.localizedDescription && !privacyHTTPAccount.busy,
                      "privacy HTTP presents sanitized guidance and releases busy state")
        }
        let privacyRefreshStore = MemoryStore(); privacyRefreshStore.data = privacySaved
        let privacyRefreshReplies = Replies { _ in (400, privacyJSON) }
        let privacyRefreshAccount = EpicAccount(store: privacyRefreshStore, http: EpicHTTPClient(sender: {
            try await privacyRefreshReplies.send($0, $1)
        }))
        do { _ = try await privacyRefreshAccount.accessToken(); throw Failure(message: "privacy refresh response accepted") }
        catch EpicError.privacyPolicyRequired { checks += 1 }
        try check(privacyRefreshAccount.signedIn && privacyRefreshAccount.accountID == "account-one"
                  && privacyRefreshStore.data == privacySaved && privacyRefreshStore.removals == 0,
                  "privacy refresh is not treated as a revoked refresh session")
        let privacyRefreshRequests = await privacyRefreshReplies.count
        try check(privacyRefreshRequests == 1, "privacy refresh response is not retried")

        let privacyHTTP = EpicHTTPClient(sender: { _, _ in EpicHTTPResponse(status: 400, data: Data(privacyJSON.utf8)) })
        let privacyRequest = try EpicEndpoints.request(host: EpicEndpoints.account, path: "/account/api/oauth/token")
        do { _ = try await privacyHTTP.send(privacyRequest, limit: 16); throw Failure(message: "privacy response bypassed size limit") }
        catch EpicError.responseTooLarge { checks += 1 }
        let privacySuccessAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { _, _ in
            EpicHTTPResponse(status: 200, data: Data(privacyJSON.utf8))
        }))
        do { try await privacySuccessAccount.login(code: "synthetic-code"); throw Failure(message: "successful status accepted an error body as a token") }
        catch EpicError.invalidResponse { checks += 1 }
        for invalidBody in ["{}", "not JSON", privacyJSON.replacingOccurrences(of: "PRIVACY_POLICY_ACCEPTANCE", with: "UNKNOWN_ACTION"),
                            privacyJSON.replacingOccurrences(of: "oauth.corrective_action_required", with: "oauth.other_error"),
                            #"{"errorCode":"errors.com.epicgames.oauth.corrective_action_required","metadata":{"correctiveAction":[]}}"#] {
            let unknownHTTP = EpicHTTPClient(sender: { _, _ in EpicHTTPResponse(status: 403, data: Data(invalidBody.utf8)) })
            do { _ = try await unknownHTTP.send(privacyRequest); throw Failure(message: "unknown corrective action misclassified") }
            catch EpicError.http(403) { checks += 1 }
        }
        let privacyCancelGate = Gate()
        let privacyCancelHTTP = EpicHTTPClient(sender: { _, _ in
            await privacyCancelGate.stop()
            return EpicHTTPResponse(status: 400, data: Data(privacyJSON.utf8))
        })
        let privacyCanceled = Task { try await privacyCancelHTTP.send(privacyRequest) }
        await privacyCancelGate.waitForEntry(); privacyCanceled.cancel(); await privacyCancelGate.open()
        do { _ = try await privacyCanceled.value; throw Failure(message: "privacy response suppressed caller cancellation") }
        catch is CancellationError { checks += 1 }
        let firstRequests = await replies.all()
        try check(firstRequests[0].httpMethod == "POST", "token POST")
        try check(String(data: firstRequests[0].httpBody!, encoding: .utf8)!.contains("grant_type=authorization_code"), "authorization grant")
        try check(firstRequests[0].url!.host == EpicEndpoints.account, "auth host")
        try check(firstRequests[0].value(forHTTPHeaderField: "Authorization")!.hasPrefix("Basic "), "public client Basic only")
        let firstToken = try await account.accessToken()
        try check(firstToken == "synthetic-access", "cached access")
        let generation = account.sessionGeneration
        account.signOut()
        try check(!account.signedIn && account.accountID == nil && store.data == nil && account.sessionGeneration != generation, "logout removes session")
        for invalid in ["", "https://evil.example/?code=synthetic-code", "bad&code", #"{"code":"wrong-field"}"#] {
            do { try await account.login(code: invalid); throw Failure(message: "invalid code accepted") }
            catch is EpicError { checks += 1 }
        }
        let countAfterInvalid = await replies.count
        try check(countAfterInvalid == 1, "invalid codes never sent")

        // Exercise a restored refresh-only session and concurrent callers.
        let restoredStore = MemoryStore()
        restoredStore.data = Data(saved.utf8)
        let refreshGate = Gate()
        let refreshReplies = Replies { request in
            try expect(request.url!.path.hasSuffix("/oauth/token"), "refresh route")
            let body = String(data: request.httpBody!, encoding: .utf8)!
            try expect(body.contains("grant_type=refresh_token"), "refresh grant")
            await refreshGate.stop()
            return (200, tokenJSON)
        }
        let restored = EpicAccount(store: restoredStore, http: EpicHTTPClient(sender: {
            try await refreshReplies.send($0, $1)
        }))
        try check(restored.signedIn && restored.accountID == "account-one", "session restoration")
        let a = Task { try await restored.accessToken() }
        let b = Task { try await restored.accessToken() }
        await refreshGate.waitForEntry()
        await refreshGate.open()
        let tokens = try await [a.value, b.value]
        let refreshCount = await refreshReplies.count
        try check(refreshCount == 1 && tokens == ["synthetic-access", "synthetic-access"], "refresh single flight")
        let expiredStore = MemoryStore()
        expiredStore.data = Data(#"{"accountID":"account-one","displayName":"Player","refreshToken":"synthetic-refresh","refreshExpires":0}"#.utf8)
        let expired = EpicAccount(store: expiredStore, http: client)
        try check(!expired.signedIn && expiredStore.data == nil, "expired saved session discarded")

        // A response which ignores cancellation must never revive logout.
        let loginGate = Gate()
        let slowReplies = Replies { request in await loginGate.stop(); return (200, tokenJSON) }
        let slowStore = MemoryStore()
        let slow = EpicAccount(store: slowStore, http: EpicHTTPClient(sender: { try await slowReplies.send($0, $1) }))
        let pendingLogin = Task { try await slow.login(code: "synthetic-code") }
        await loginGate.waitForEntry()
        slow.signOut()
        await loginGate.open()
        do { try await pendingLogin.value; throw Failure(message: "logged out login completed") }
        catch is CancellationError { checks += 1 }
        catch EpicError.accountChanged { checks += 1 }
        try check(!slow.signedIn && slowStore.data == nil && !slow.busy, "no logout resurrection")

        let cancelGate = Gate()
        let cancelStore = MemoryStore()
        let cancelAccount = EpicAccount(store: cancelStore, http: EpicHTTPClient(sender: { _, _ in
            await cancelGate.stop()
            return EpicHTTPResponse(status: 200, data: Data(tokenJSON.utf8))
        }))
        let cancelLogin = Task { try await cancelAccount.login(code: "synthetic-code") }
        await cancelGate.waitForEntry()
        cancelLogin.cancel(); await cancelGate.open()
        do { try await cancelLogin.value; throw Failure(message: "canceled login accepted") }
        catch is CancellationError { checks += 1 }
        try check(!cancelAccount.signedIn && cancelStore.data == nil && !cancelAccount.busy, "login caller cancellation")

        let sharedCancelGate = Gate()
        let sharedCancelStore = MemoryStore(); sharedCancelStore.data = Data(saved.utf8)
        let sharedCancelReplies = Replies { _ in
            await sharedCancelGate.stop(); return (200, tokenJSON)
        }
        let sharedCancel = EpicAccount(store: sharedCancelStore, http: EpicHTTPClient(sender: {
            try await sharedCancelReplies.send($0, $1)
        }))
        let canceledWaiter = Task { try await sharedCancel.accessToken() }
        let validWaiter = Task { try await sharedCancel.accessToken() }
        await sharedCancelGate.waitForEntry()
        canceledWaiter.cancel(); await sharedCancelGate.open()
        do { _ = try await canceledWaiter.value; throw Failure(message: "canceled refresh waiter accepted") }
        catch is CancellationError { checks += 1 }
        let sharedToken = try await validWaiter.value
        let sharedCount = await sharedCancelReplies.count
        try check(sharedToken == "synthetic-access" && sharedCount == 1 && sharedCancel.signedIn, "one refresh cancellation preserves other callers")

        // There may be no remaining caller to commit a successful OAuth
        // refresh. Epic rotates refresh tokens, so even a sole canceled waiter
        // must leave the shared session's successful rotation durably saved.
        let soleGate = Gate()
        let soleStore = MemoryStore(); soleStore.data = Data(saved.utf8)
        let rotating = RotatingRefresh(gate: soleGate)
        let rotatingHTTP = EpicHTTPClient(sender: { try await rotating.send($0, $1) })
        let soleAccount = EpicAccount(store: soleStore, http: rotatingHTTP)
        let soleWaiter = Task { try await soleAccount.accessToken() }
        await soleGate.waitForEntry()
        soleWaiter.cancel(); await soleGate.open()
        do { _ = try await soleWaiter.value; throw Failure(message: "sole canceled waiter returned access") }
        catch is CancellationError { checks += 1 }
        let savedRotation = String(data: soleStore.data!, encoding: .utf8)!
        try check(savedRotation.contains("rotated-refresh-one") && soleStore.saves == 1, "sole canceled waiter persists successful rotation once")
        let restartedSole = EpicAccount(store: soleStore, http: rotatingHTTP)
        let restartedToken = try await restartedSole.accessToken()
        try check(restartedToken == "synthetic-access" && restartedSole.signedIn && soleStore.saves == 2, "restart uses rotated refresh after caller cancellation")
        let rotationCalls = await rotating.calls
        try check(rotationCalls == 2, "canceled waiter never replays consumed refresh")

        let soleRejectGate = Gate()
        let soleRejectStore = MemoryStore(); soleRejectStore.data = Data(saved.utf8)
        let soleReject = EpicAccount(store: soleRejectStore, http: EpicHTTPClient(sender: { _, _ in
            await soleRejectGate.stop()
            return EpicHTTPResponse(status: 400, data: Data())
        }))
        let soleRejectedWaiter = Task { try await soleReject.accessToken() }
        await soleRejectGate.waitForEntry()
        soleRejectedWaiter.cancel(); await soleRejectGate.open()
        do { _ = try await soleRejectedWaiter.value; throw Failure(message: "canceled failed refresh returned") }
        catch is CancellationError { checks += 1 }
        try check(!soleReject.signedIn && soleRejectStore.data == nil, "shared revoked refresh still logs out without an active waiter")

        let refreshLogoutGate = Gate()
        let refreshLogoutStore = MemoryStore(); refreshLogoutStore.data = Data(saved.utf8)
        let refreshLogoutHTTP = EpicHTTPClient(sender: { _, _ in
            await refreshLogoutGate.stop()
            return EpicHTTPResponse(status: 200, data: Data(tokenJSON.utf8))
        })
        let refreshLogout = EpicAccount(store: refreshLogoutStore, http: refreshLogoutHTTP)
        let pendingRefresh = Task { try await refreshLogout.accessToken() }
        await refreshLogoutGate.waitForEntry()
        refreshLogout.signOut()
        await refreshLogoutGate.open()
        do { _ = try await pendingRefresh.value; throw Failure(message: "refresh survived logout") }
        catch is CancellationError { checks += 1 }
        catch EpicError.accountChanged { checks += 1 }
        try check(!refreshLogout.signedIn && refreshLogoutStore.data == nil, "refresh cannot revive logout")

        let rejectedStore = MemoryStore(); rejectedStore.data = Data(saved.utf8)
        let rejected = EpicAccount(store: rejectedStore, http: EpicHTTPClient(sender: { _, _ in
            EpicHTTPResponse(status: 400, data: Data(#"{"errorMessage":"secret server body"}"#.utf8))
        }))
        do { _ = try await rejected.accessToken(); throw Failure(message: "revoked refresh accepted") }
        catch EpicError.notSignedIn { checks += 1 }
        try check(!rejected.signedIn && rejectedStore.data == nil, "revoked refresh cleared")

        let mismatchStore = MemoryStore(); mismatchStore.data = Data(saved.utf8)
        let mismatch = EpicAccount(store: mismatchStore, http: EpicHTTPClient(sender: { _, _ in
            EpicHTTPResponse(status: 200, data: Data(tokenJSON.replacingOccurrences(of: "account-one", with: "account-two").utf8))
        }))
        do { _ = try await mismatch.accessToken(); throw Failure(message: "refresh changed identity") }
        catch EpicError.accountChanged { checks += 1 }
        try check(mismatch.accountID != "account-two", "refresh identity protected")

        for key in ["access_token", "refresh_token"] {
            var malformed = try JSONSerialization.jsonObject(with: Data(tokenJSON.utf8)) as! [String: Any]
            malformed[key] = "bad\r\nAuthorization: injected"
            let malformedData = try JSONSerialization.data(withJSONObject: malformed)
            let malformedAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { _, _ in
                EpicHTTPResponse(status: 200, data: malformedData)
            }))
            do { try await malformedAccount.login(code: "synthetic-code"); throw Failure(message: "control characters in token") }
            catch EpicError.invalidResponse { checks += 1 }
        }

        let rejectedAccess = RejectedAccess()
        let rejectedAccessAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: {
            try await rejectedAccess.send($0, $1)
        }))
        try await rejectedAccessAccount.login(code: "synthetic-code")
        let freshExchange = try await EpicAPI(account: rejectedAccessAccount).exchangeCode()
        let rejectionCounts = await rejectedAccess.counts()
        try check(freshExchange == "fresh-exchange" && rejectionCounts.0 == 2 && rejectionCounts.1 == 2, "401 refreshes access once")
        let perpetualRejection = RejectedAccess(alwaysReject: true)
        let perpetualAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: {
            try await perpetualRejection.send($0, $1)
        }))
        try await perpetualAccount.login(code: "synthetic-code")
        do { _ = try await EpicAPI(account: perpetualAccount).exchangeCode(); throw Failure(message: "401 retry unbounded") }
        catch EpicError.http(401) { checks += 1 }
        let perpetualCounts = await perpetualRejection.counts()
        try check(perpetualCounts.0 == 2 && perpetualCounts.1 == 2, "401 retry is bounded")
        let deniedReplies = Replies { request in
            request.url!.path.hasSuffix("/oauth/token") ? (200, tokenJSON) : (403, "secret response")
        }
        let deniedAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: {
            try await deniedReplies.send($0, $1)
        }))
        try await deniedAccount.login(code: "synthetic-code")
        do { _ = try await EpicAPI(account: deniedAccount).exchangeCode(); throw Failure(message: "forbidden accepted") }
        catch EpicError.http(403) { checks += 1 }
        let deniedCount = await deniedReplies.count
        try check(deniedCount == 2, "403 does not refresh or retry")

        let apiReplies = Replies { request in
            let path = request.url!.path
            if path.hasSuffix("/oauth/token") { return (200, tokenJSON) }
            try expect(request.value(forHTTPHeaderField: "Authorization") == "bearer synthetic-access", "API bearer")
            if path.hasSuffix("/assets/Windows") { return (200, assetJSON) }
            if path.hasSuffix("/items"), request.url!.host == EpicEndpoints.library {
                let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!.first { $0.name == "cursor" }?.value
                if cursor == nil { return (200, #"{"records":[{"appName":"dlc-one"}],"responseMetadata":{"nextCursor":"page&two"}}"#) }
                try expect(cursor == "page&two", "cursor encoded without query injection")
                return (200, #"{"records":[{"appName":"external-game","namespace":"namespace-one","catalogItemId":"external-catalog"}],"responseMetadata":{}}"#)
            }
            if path.hasSuffix("/bulk/items") {
                let id = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!.first { $0.name == "id" }!.value!
                if id == "external-catalog" {
                    return (200, #"{"external-catalog":{"title":"External","customAttributes":{"ThirdPartyManagedProvider":{"value":"UbisoftConnect"}}}}"#)
                }
                return (200, catalogJSON)
            }
            if path.hasSuffix("/label/Live") {
                return (200, #"{"elements":[{"appName":"game-one","hash":"0123456789abcdef0123456789abcdef01234567","manifests":[{"uri":"http://download.epicgames.com/game/build.manifest","queryParams":[{"name":"Policy","value":"value+with&sign"}]},{"uri":"https://cdn.epicgames.com/game/build.manifest"}]}]}"#)
            }
            if path.hasSuffix("/oauth/exchange") { return (200, #"{"code":"one-use-exchange"}"#) }
            throw Failure(message: "unexpected API route")
        }
        let apiAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { try await apiReplies.send($0, $1) }))
        try await apiAccount.login(code: "synthetic-code")
        let api = EpicAPI(account: apiAccount)
        let games = try await api.games()
        try check(games.map(\.appName) == ["external-game", "game-one"], "owned Windows assets + external library pagination dedup excludes DLC")
        try check(games[0].launchRequirement == "UbisoftConnect" && games[1].launchRequirement == nil, "external launcher explicit only")
        try check(games[1].artworkURL == URL(string: "https://cdn.epicgames.com/one.jpg") && games[1].buildVersion == "v1", "catalog artwork/version")
        let game = games[1]
        let manifest = try await api.manifest(game: game)
        try check(LogStore.shared.messages.isEmpty, "valid manifest emits no rejection diagnostic")
        try check(manifest.url.scheme == "https", "manifest upgraded HTTPS")
        try check(manifest.chunkBaseURLs.count == 2 && manifest.chunkBaseURLs.allSatisfy { $0.query == nil }, "distinct credential-free chunk bases")
        try check(manifest.sha1 == "0123456789abcdef0123456789abcdef01234567", "manifest API digest preserved")
        try check(manifest.alternateURLs.map(\.host) == ["cdn.epicgames.com"], "manifest response preserves alternate download origins")
        try check(URLComponents(url: manifest.url, resolvingAgainstBaseURL: false)!.queryItems!.first!.value == "value+with&sign", "manifest signed query escaped")
        // The authenticated asset response, rather than a vendor list,
        // authorizes public CDN origins for every game.
        let currentCDNs = ["egdownload.fastly-edge.com", "egs-cloudfront-chunks.epicgamescdn.com",
                           "cloudflare.epicgamescdn.com", "epicgames-download1-1251447533.file.myqcloud.com",
                           "other.fastly-edge.com", "other.epicgamescdn.com", "cdn.future-vendor.net"]
        for host in currentCDNs {
            let data = try JSONSerialization.data(withJSONObject: ["elements": [[
                "appName": game.appName, "hash": String(repeating: "a", count: 40),
                "manifests": [["uri": "http://" + host + ":80/Builds/Game/build.manifest",
                               "queryParams": [["name": "token", "value": "synthetic+value&safe"]]],
                              ["uri": "https://epicgames-download1.akamaized.net/Builds/Game/build.manifest"]]
            ]]])
            let current: EpicManifestLocation
            do { current = try EpicCatalog.manifest(data: data, game: game) }
            catch { throw Failure(message: "current Epic CDN rejected: " + host) }
            try check(current.url.host == host && current.chunkBaseURLs.count == 2,
                      "current Epic CDN accepted beside older CDN")
            try check(current.url.scheme == "https" && current.url.port == nil,
                      "authorized HTTP CDN is accessed only through HTTPS")
            try check(current.chunkBaseURLs.allSatisfy { $0.query == nil }, "CDN chunk bases omit signed credentials")
            try check(URLComponents(url: current.url, resolvingAgainstBaseURL: false)!.queryItems!.first!.value == "synthetic+value&safe",
                      "current CDN signed query preserved")
        }
        var anotherGame = game
        anotherGame.appName = "another-owned-game"
        let anotherReply = try JSONSerialization.data(withJSONObject: ["elements": [[
            "appName": anotherGame.appName, "hash": String(repeating: "a", count: 40),
            "manifests": [["uri": "https://cdn.other-vendor.net/Builds/Other/build.manifest"]]
        ]]])
        let anotherLocation = try EpicCatalog.manifest(data: anotherReply, game: anotherGame)
        try check(anotherLocation.url.host == "cdn.other-vendor.net",
                  "public CDN admission applies to another game without title-specific rules")
        for host in ["localhost", "content.localhost", "content.local", "content.internal", "router.home.arpa",
                     "intranet", "127.0.0.1", "192.168.1.2", "8.8.8.8", "127.1", "2130706433", "0x7f000001",
                     "[::1]", "[2001:4860:4860::8888]", "bad_host.vendor.net", "bad..vendor.net",
                     "-bad.vendor.net", "bad-.vendor.net", "egdownload.fastly-edge.com.evil.example"] {
            let data = try JSONSerialization.data(withJSONObject: ["elements": [[
                "hash": String(repeating: "a", count: 40), "manifests": [["uri": "https://" + host + "/build.manifest"]]
            ]]])
            do { _ = try EpicCatalog.manifest(data: data, game: game); throw Failure(message: "unapproved CDN accepted: " + host) }
            catch EpicError.invalidResponse { checks += 1 }
        }
        let exchange = try await api.exchangeCode()
        try check(exchange == "one-use-exchange", "ephemeral exchange")
        let ownershipBytes = Data([0, 255, 128, 42, 13, 10])
        let ownershipClient = EpicHTTPClient(sender: { request, limit in
            if request.url!.path.hasSuffix("/oauth/token") {
                return EpicHTTPResponse(status: 200, data: Data(tokenJSON.utf8))
            }
            try expect(request.url!.host == EpicEndpoints.ecommerce, "ownership fixed Epic host")
            try expect(request.url!.path == "/ecommerceintegration/api/public/platforms/EPIC/identities/account-one/ownershipToken", "ownership account path binding")
            try expect(request.httpMethod == "POST" && request.value(forHTTPHeaderField: "Authorization") == "bearer synthetic-access", "ownership authenticated POST")
            try expect(request.value(forHTTPHeaderField: "Content-Type") == "application/x-www-form-urlencoded", "ownership form content type")
            try expect(String(data: request.httpBody!, encoding: .utf8) == "nsCatalogItemId=namespace-one%3Acatalog-one", "ownership selected catalog form binding")
            try expect(limit == 64 * 1024, "ownership bounded payload")
            return EpicHTTPResponse(status: 200, data: ownershipBytes)
        })
        let ownershipAccount = EpicAccount(store: MemoryStore(), http: ownershipClient)
        try await ownershipAccount.login(code: "synthetic-code")
        let ownershipAPI = EpicAPI(account: ownershipAccount)
        let receivedOwnership = try await ownershipAPI.ownershipToken(game: game)
        try check(receivedOwnership == ownershipBytes, "opaque ownership bytes preserved without JSON or text conversion")
        ownershipAccount.signOut()
        do { _ = try await ownershipAPI.ownershipToken(game: game); throw Failure(message: "signed-out ownership request accepted") }
        catch EpicError.accountChanged { checks += 1 }
        for payload in [Data(), Data(repeating: 1, count: 65537)] {
            let bounded = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { request, _ in
                EpicHTTPResponse(status: 200, data: request.url!.path.hasSuffix("/oauth/token") ? Data(tokenJSON.utf8) : payload)
            }))
            try await bounded.login(code: "synthetic-code")
            do { _ = try await EpicAPI(account: bounded).ownershipToken(game: game); throw Failure(message: "invalid ownership payload accepted") }
            catch let error as EpicError { try check(error == (payload.isEmpty ? .invalidResponse : .responseTooLarge), "ownership payload bounds enforced") }
        }
        let persistedGame = String(data: try JSONEncoder().encode(game), encoding: .utf8)!
        try check(!persistedGame.contains("synthetic-access") && !persistedGame.contains(exchange), "no credentials in game metadata")
        let allAPIRequests = await apiReplies.all()
        try check(allAPIRequests.filter { $0.url!.host == EpicEndpoints.library }.count == 2, "both library pages fetched")
        try check(allAPIRequests.filter { $0.url!.path.hasSuffix("bulk/items") }.count == 3, "duplicates never request extra metadata")

        let loopReplies = Replies { request in
            if request.url!.path.hasSuffix("/oauth/token") { return (200, tokenJSON) }
            if request.url!.path.hasSuffix("/assets/Windows") { return (200, "[]") }
            return (200, #"{"records":[],"responseMetadata":{"nextCursor":"same"}}"#)
        }
        let loopAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { try await loopReplies.send($0, $1) }))
        try await loopAccount.login(code: "synthetic-code")
        do { _ = try await EpicAPI(account: loopAccount).games(); throw Failure(message: "looping cursor accepted") }
        catch EpicError.invalidResponse { checks += 1 }
        let loopCount = await loopReplies.count
        try check(loopCount == 4, "pagination loop stopped promptly")

        // Stale responses from a valid API request cannot enter a new account.
        let apiGate = Gate()
        let lateReplies = Replies { request in
            if request.url!.path.hasSuffix("/oauth/token") { return (200, tokenJSON) }
            await apiGate.stop(); return (200, #"{"code":"stale-code"}"#)
        }
        let lateAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { try await lateReplies.send($0, $1) }))
        try await lateAccount.login(code: "synthetic-code")
        let lateAPI = EpicAPI(account: lateAccount)
        let lateTask = Task { try await lateAPI.exchangeCode() }
        await apiGate.waitForEntry(); lateAccount.signOut(); await apiGate.open()
        do { _ = try await lateTask.value; throw Failure(message: "stale exchange accepted") }
        catch EpicError.accountChanged { checks += 1 }

        let boundaryReplies = Replies { _ in (200, "{}") }
        let boundary = EpicHTTPClient(sender: { try await boundaryReplies.send($0, $1) })
        for url in ["http://" + EpicEndpoints.account + "/", "https://evil.example/", "https://" + EpicEndpoints.account + ".evil.example/", "https://user:password@" + EpicEndpoints.account + "/", "https://" + EpicEndpoints.account + ":8443/"] {
            do { _ = try await boundary.send(URLRequest(url: URL(string: url)!)); throw Failure(message: "unsafe credential destination") }
            catch EpicError.invalidResponse { checks += 1 }
        }
        let boundaryCount = await boundaryReplies.count
        try check(boundaryCount == 0, "invalid destinations never reach transport")
        let large = EpicHTTPClient(sender: { _, _ in EpicHTTPResponse(status: 200, data: Data(repeating: 65, count: 9)) })
        let safe = try EpicEndpoints.request(host: EpicEndpoints.account, path: "/account/api/oauth/verify")
        do { _ = try await large.send(safe, limit: 8); throw Failure(message: "oversized response") }
        catch EpicError.responseTooLarge { checks += 1 }
        let network = EpicHTTPClient(sender: { _, _ in
            throw NSError(domain: NSURLErrorDomain, code: -1009, userInfo: [NSLocalizedDescriptionKey: "https://signed.example/?token=leak"])
        })
        do { _ = try await network.send(safe); throw Failure(message: "network accepted") }
        catch let error as EpicError { try check(error == .network && !error.localizedDescription.contains("leak"), "network errors sanitized") }
        let cancelClient = EpicHTTPClient(sender: { _, _ in throw URLError(.cancelled) })
        do { _ = try await cancelClient.send(safe); throw Failure(message: "cancellation swallowed") }
        catch is CancellationError { checks += 1 }

        let redirect = EpicNoRedirectDelegate()
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: safe)
        let response = HTTPURLResponse(url: safe.url!, statusCode: 302, httpVersion: nil, headerFields: ["Location":"https://evil.example"])!
        var refused = false
        redirect.urlSession(session, task: task, willPerformHTTPRedirection: response,
                            newRequest: URLRequest(url: URL(string: "https://evil.example")!)) { refused = $0 == nil }
        try check(refused, "credential redirects rejected by real delegate")
        session.invalidateAndCancel()

        for identifier in ["../bad", "/absolute", "x?param", "x#fragment", "x\\path", "..", "", "x%2Fbad"] {
            do { _ = try EpicEndpoints.identifier(identifier); throw Failure(message: "path injection accepted") }
            catch EpicError.invalidResponse { checks += 1 }
        }
        for uri in ["https://evil.example/a.manifest", "https://download.epicgames.com.evil.example/a.manifest", "https://user:secret@download.epicgames.com/a.manifest", "file:///a.manifest", "https://download.epicgames.com:8443/a.manifest", "https://download.epicgames.com:80/a.manifest", "http://download.epicgames.com:443/a.manifest"] {
            let data = try JSONSerialization.data(withJSONObject: ["elements": [["hash":String(repeating: "a", count: 40), "manifests":[["uri":uri]]]]])
            do { _ = try EpicCatalog.manifest(data: data, game: game); throw Failure(message: "unsafe manifest URL") }
            catch EpicError.invalidResponse { checks += 1 }
        }
        let preload = Data(#"{"elements":[{"hash":"0123456789abcdef0123456789abcdef01234567","isPreloaded":true,"manifests":[{"uri":"https://download.epicgames.com/a.manifest"}]}]}"#.utf8)
        do { _ = try EpicCatalog.manifest(data: preload, game: game); throw Failure(message: "unsupported preload") }
        catch EpicError.unsupportedManifest { checks += 1 }
        let encrypted = Data(#"{"elements":[{"hash":"0123456789abcdef0123456789abcdef01234567","secrets":{"key":"secret"},"manifests":[{"uri":"https://download.epicgames.com/a.manifest"}]}]}"#.utf8)
        do { _ = try EpicCatalog.manifest(data: encrypted, game: game); throw Failure(message: "unsupported encrypted manifest") }
        catch EpicError.unsupportedManifest { checks += 1 }
        let multi = Data(#"{"elements":[]}"#.utf8)
        do { _ = try EpicCatalog.manifest(data: multi, game: game); throw Failure(message: "missing manifest") }
        catch EpicError.invalidResponse { checks += 1 }

        func diagnosticReply(_ element: [String: Any]) throws -> Data {
            try JSONSerialization.data(withJSONObject: ["elements": [element]])
        }
        func diagnosticReject(_ data: Data, _ expected: String, unsupported: Bool = false) throws {
            var reasons: [String] = []
            do {
                _ = try EpicCatalog.manifest(data: data, game: game, diagnostic: { reasons.append($0) })
                throw Failure(message: "diagnostic fixture was accepted")
            } catch let error as EpicError {
                try check(error == (unsupported ? .unsupportedManifest : .invalidResponse), "parser error contract preserved")
            }
            try check(reasons == [expected], "specific bounded parser diagnostic")
            try check(reasons.allSatisfy { !$0.contains("SYNTHETIC") && !$0.contains("synthetic-access") },
                      "synthetic response secrets never enter diagnostics")
        }
        let validElement: [String: Any] = ["appName": "game-one", "hash": String(repeating: "a", count: 40),
            "manifests": [["uri": "https://download.epicgames.com/a.manifest"]]]
        var changed = validElement
        changed["hash"] = 42
        try diagnosticReject(diagnosticReply(changed), "decode-type:elements[].hash")
        changed = validElement
        changed["manifests"] = [["uri": "https://download.epicgames.com/SYNTHETIC_PATH",
            "queryParams": [["name": "SYNTHETIC_QUERY_NAME", "value": false]]]]
        try diagnosticReject(diagnosticReply(changed), "decode-type:elements[].manifests[].queryParams[].value")
        changed = validElement
        changed["secrets"] = ["SYNTHETIC_SECRET_KEY": ["SYNTHETIC_SECRET_VALUE": 1]]
        try diagnosticReject(diagnosticReply(changed), "decode-type:elements[].secrets.field")
        changed = validElement; changed.removeValue(forKey: "hash")
        try diagnosticReject(diagnosticReply(changed), "decode-missing:elements[].hash")
        changed = validElement; changed["hash"] = NSNull()
        try diagnosticReject(diagnosticReply(changed), "decode-null:elements[].hash")
        try diagnosticReject(Data("{SYNTHETIC_INVALID_JSON".utf8), "decode-corrupt:root")
        try diagnosticReject(multi, "elements-count")
        changed = validElement; changed["appName"] = "SYNTHETIC_OTHER_ASSET"
        try diagnosticReject(diagnosticReply(changed), "asset-name-mismatch")
        changed = validElement; changed["hash"] = "SYNTHETIC_BAD_HASH"
        try diagnosticReject(diagnosticReply(changed), "hash-format")
        changed = validElement; changed["manifests"] = [[String: String]]()
        try diagnosticReject(diagnosticReply(changed), "empty-manifests")
        changed = validElement; changed["manifests"] = Array(repeating: ["uri": "https://download.epicgames.com/a.manifest"], count: 17)
        try diagnosticReject(diagnosticReply(changed), "manifests-count")
        let badURLs = [
            ("https://[", "url-shape"),
            ("file:///SYNTHETIC_PATH", "url-scheme"),
            ("https:///SYNTHETIC_PATH", "url-host"),
            ("https://SYNTHETIC_USER:SYNTHETIC_PASSWORD@download.fixture.invalid/SYNTHETIC_PATH?sig=SYNTHETIC_QUERY", "url-userinfo"),
            ("https://download.epicgames.com/SYNTHETIC_PATH?sig=SYNTHETIC_QUERY#SYNTHETIC_FRAGMENT", "url-fragment"),
            ("https://download.epicgames.com:8443/SYNTHETIC_PATH?sig=SYNTHETIC_QUERY", "url-port"),
            ("http://download.fixture.invalid/SYNTHETIC_PATH?sig=SYNTHETIC_QUERY", "cdn-host:http://download.fixture.invalid"),
            ("https://download..fixture.invalid/SYNTHETIC_PATH?sig=SYNTHETIC_QUERY", "cdn-host:unavailable"),
        ]
        for (uri, expected) in badURLs {
            changed = validElement; changed["manifests"] = [["uri": uri]]
            try diagnosticReject(diagnosticReply(changed), expected)
        }
        try diagnosticReject(preload, "preloaded", unsupported: true)
        try diagnosticReject(encrypted, "manifest-secrets", unsupported: true)
        let malformedReplies = Replies { request in
            if request.url!.path.hasSuffix("/oauth/token") { return (200, tokenJSON) }
            return (200, #"{"elements":[{"hash":false,"manifests":[]}]}"#)
        }
        let loggedAccount = EpicAccount(store: MemoryStore(), http: EpicHTTPClient(sender: { try await malformedReplies.send($0, $1) }))
        try await loggedAccount.login(code: "synthetic-code")
        let loggedAPI = EpicAPI(account: loggedAccount)
        do { _ = try await loggedAPI.manifest(game: game); throw Failure(message: "malformed API manifest accepted") }
        catch EpicError.invalidResponse { checks += 1 }
        try check(LogStore.shared.messages == ["[epic-manifest] reason=decode-type:elements[].hash"],
                  "production API forwards only sanitized parser reason")

        let loginParts = URLComponents(url: EpicAccount.loginURL, resolvingAgainstBaseURL: false)!
        try check(loginParts.host == "www.epicgames.com" && loginParts.scheme == "https", "official browser login")
        let redirectValue = loginParts.queryItems!.first!.value!
        try check(redirectValue.contains("responseType=code") && !redirectValue.contains("synthetic-code"), "browser URL contains no user credential")
        try check(EpicAccount.accountURL.absoluteString == "https://www.epicgames.com/account/personal", "official fixed account entry without response-controlled query")
        print("PASS: \(checks) native Epic account/API checks")
    }
}
'''


def run_native(sources: list[Path], temporary: Path, swift_version: str) -> subprocess.CompletedProcess[str]:
    harness = temporary / "main.swift"
    harness.write_text(HARNESS)
    executable = temporary / "epic-check"
    compile_result = subprocess.run(
        ["xcrun", "swiftc", "-j", "1", "-parse-as-library", "-swift-version", swift_version, "-O",
         *map(str, sources), str(harness), "-o", str(executable)],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if compile_result.returncode:
        return compile_result
    return subprocess.run([str(executable)], capture_output=True, text=True,
                          timeout=60, check=False)


def main() -> int:
    global ARTIFACTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--swift-version", choices=("5", "6"), default="5")
    parser.add_argument("--report-dir", type=Path, default=ARTIFACTS)
    args = parser.parse_args()
    ARTIFACTS = args.report_dir
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / f"app/Madeira/{name}.swift" for name in
               ("EpicCatalog", "EpicAccount", "EpicAPI")]
    if any(not path.is_file() for path in sources):
        print("RED: native Epic production sources are not implemented")
        return 1
    with tempfile.TemporaryDirectory(prefix="native-", dir=ARTIFACTS) as folder:
        temporary = Path(folder)
        result = run_native(sources, temporary, args.swift_version)
        print(result.stdout + result.stderr)
        report = [result.stdout + result.stderr]
        if result.returncode:
            return result.returncode
        if args.mutations:
            mutations = [
                ("EpicCatalog", "EpicEndpoints.hosts.contains(host),", "true,", "credential destination"),
                ("EpicCatalog", "guard response.data.count <= limit else", "guard true else", "response bound"),
                ("EpicCatalog", "guard !excluded, asset.namespace", "guard true, asset.namespace", "DLC filtering"),
                ("EpicAPI", "guard generation == account.sessionGeneration, account.signedIn else", "guard true else", "stale account response"),
                ("EpicAccount", "if let active = refreshTask, let activeIdentity = refreshIdentity {", "if let active = refreshTask, let activeIdentity = refreshIdentity, false {", "single flight"),
                ("EpicAccount", "try self.accept(result)", "try Task.checkCancellation()", "shared task persists rotated refresh"),
                ("EpicAccount", "if let action = EpicError.correctiveAction(in: data) { throw action }", "if false, let action = EpicError.correctiveAction(in: data) { throw action }", "pasted privacy JSON classification before sign-out"),
                ("EpicCatalog", "throw EpicError.correctiveAction(in: response.data) ?? .http(response.status)", "throw EpicError.http(response.status)", "HTTP privacy classification"),
                ("EpicAccount", "https://www.epicgames.com/account/personal", "https://evil.example/policy", "fixed official account URL"),
                ("EpicAccount", "guard let page = try? JSONDecoder().decode(Page.self, from: data), page.errorCode == nil else", "guard let page = try? JSONDecoder().decode(Page.self, from: data) else", "unknown error page cannot supply a login code"),
            ]
            for index, (name, before, after, label) in enumerate(mutations):
                mutation_folder = temporary / f"mutation-{index}"
                mutation_folder.mkdir()
                mutation_sources = []
                for source in sources:
                    text = source.read_text()
                    if source.stem == name:
                        if text.count(before) != 1:
                            raise RuntimeError(f"Mutation anchor moved: {label}")
                        text = text.replace(before, after, 1)
                    destination = mutation_folder / source.name
                    destination.write_text(text)
                    mutation_sources.append(destination)
                mutation = run_native(mutation_sources, mutation_folder, args.swift_version)
                if mutation.returncode == 0 or "FAIL:" not in mutation.stdout:
                    print(mutation.stdout + mutation.stderr)
                    print(f"FAIL: mutation did not reach a runtime test: {label}")
                    return 1
                report.append(f"PASS: mutation rejected: {label}\n")
                print(report[-1].strip())
        (ARTIFACTS / f"result-swift{args.swift_version}.txt").write_text("".join(report))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
