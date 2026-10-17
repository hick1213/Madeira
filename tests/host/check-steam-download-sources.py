#!/usr/bin/env python3
"""Run actual Swift CDN policy/store with isolated preferences, never credentials.

Ignoring the user's manual host or pruning it before pool selection must fail.
Expired history must not override live measurements. Every official directory row
is retained; unsupported rows stay disabled and request origins match membership.
"""
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "app/Madeira/SwiftSteam/Content"


def block(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if not depth:
            return source[start:index + 1]
    raise ValueError("unterminated production declaration")


CHECKS = r'''
import Foundation
enum SteamLog {
    private static let lock = NSLock()
    private static var stored: [String] = []
    static var lines: [String] { lock.lock(); defer { lock.unlock() }; return stored }
    static func event(_ line: String) { lock.lock(); defer { lock.unlock() }; stored.append(line) }
}
struct CheckFailure: Error { let message: String }
func require(_ value: Bool, _ message: String) throws {
    guard value else { throw CheckFailure(message: "FAIL: " + message) }
    print("PASS: " + message)
}
final class DirectoryFailureProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var requestCount = 0
    private static var holdNext = false
    private static var holdMainland = false
    private static var held: DirectoryFailureProtocol?
    static var requests: Int { lock.lock(); defer { lock.unlock() }; return requestCount }
    static var payload: Data?
    static var cancellation = false
    static var failuresRemaining = 0
    static var statusCode = 200
    static var finalURL: URL?
    static var mainlandPayload = Data(#"{"response":{"servers":[]}}"#.utf8)
    static var mainlandFailure = false
    static var timeoutFailure = false
    static var mainlandRequests = 0
    static func holdNextRequest(mainland: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        if mainland { holdMainland = true } else { holdNext = true }
    }
    static func takeHeldRequest() -> DirectoryFailureProtocol? {
        lock.lock(); defer { held = nil; lock.unlock() }
        return held
    }
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "api.steampowered.com" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        let mainland = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.contains {
            $0.name == "ip_override" && $0.value == "114.114.114.114"
        } == true
        if mainland {
            Self.mainlandRequests += 1
            if Self.holdMainland {
                Self.holdMainland = false
                Self.held = self
                Self.lock.unlock()
                return
            }
            Self.lock.unlock()
            if Self.mainlandFailure { client?.urlProtocol(self, didFailWithError: URLError(Self.timeoutFailure ? .timedOut : .cannotConnectToHost)) }
            else { deliver(Self.mainlandPayload) }
            return
        }
        Self.requestCount += 1
        if Self.holdNext {
            Self.holdNext = false
            Self.held = self
            Self.lock.unlock()
            return
        }
        Self.lock.unlock()
        if Self.failuresRemaining > 0 {
            Self.failuresRemaining -= 1
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
        } else if let payload = Self.payload {
            deliver(payload, statusCode: Self.statusCode)
        } else {
            client?.urlProtocol(self, didFailWithError: URLError(Self.cancellation ? .cancelled : (Self.timeoutFailure ? .timedOut : .cannotConnectToHost),
                userInfo: [NSURLErrorFailingURLStringErrorKey: "https://api.steampowered.com/?token=synthetic-secret"]))
        }
    }
    func deliver(_ payload: Data, statusCode: Int = 200) {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: Self.finalURL ?? request.url!, statusCode: statusCode,
            httpVersion: "HTTP/1.1", headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
func fetchIsolated(cellID: UInt32, appID: UInt32?, using session: URLSession) async throws -> [SteamContentServer] {
    let fileURL = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent(UUID().uuidString + ".json")
    return try await SteamContentSourceDirectory.fetch(cellID: cellID, appID: appID, using: session,
        cache: SteamContentSourceDirectory.Cache(fileURL: fileURL))
}
func fixture(_ entries: [[String: Any]]) throws -> Data {
    try JSONSerialization.data(withJSONObject: ["response": ["servers": entries]])
}
func node(_ host: String, allowed: [UInt32] = []) -> [String: Any] {
    ["host": host, "type": "CDN", "https_support": "mandatory", "allowed_app_ids": allowed]
}
func heldRequest() async throws -> DirectoryFailureProtocol {
    let deadline = Date(timeIntervalSinceNow: 5)
    while Date() < deadline {
        if let held = DirectoryFailureProtocol.takeHeldRequest() { return held }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
    throw CheckFailure(message: "directory request never reached its transport barrier")
}
func independentCacheChecks(using session: URLSession, root: URL) async throws {
    let cacheURL = root.appendingPathComponent("independent-authority.json")
    let cache = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
    let good = try fixture([node("saved.vendor-cdn.com"), node("app43.vendor-cdn.com", allowed: [43])])
    DirectoryFailureProtocol.payload = good
    _ = try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: cache)
    let persisted = try Data(contentsOf: cacheURL)
    DirectoryFailureProtocol.payload = Data("invalid-json".utf8)
    let malformed = try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: cache)
    try require(malformed.map(\.host) == ["saved.vendor-cdn.com", "app43.vendor-cdn.com"] &&
        malformed.allSatisfy { $0.refreshFailure == .invalidResponse } && (try Data(contentsOf: cacheURL)) == persisted,
        "malformed refresh retains the complete persisted authority")
    DirectoryFailureProtocol.payload = good
    DirectoryFailureProtocol.statusCode = 503
    let status = try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: cache)
    try require(status.map(\.host) == malformed.map(\.host) && status.allSatisfy { $0.refreshFailure == .httpStatus(503) } &&
        (try Data(contentsOf: cacheURL)) == persisted,
        "HTTP 503 refresh preserves persisted authority and its precise failure reason")
    DirectoryFailureProtocol.statusCode = 200
    DirectoryFailureProtocol.holdNextRequest()
    let cancelling = Task { try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: cache) }
    defer { cancelling.cancel() }
    _ = try await heldRequest()
    cancelling.cancel()
    do { _ = try await cancelling.value; throw CheckFailure(message: "pending cancellation returned cached rows") }
    catch is CancellationError {}
    try require((try Data(contentsOf: cacheURL)) == persisted,
                "cancelling an actual pending URLSession directory request protects the persisted file")
    let orderingURL = root.appendingPathComponent("independent-ordering.json")
    let ordering = SteamContentSourceDirectory.Cache(fileURL: orderingURL)
    DirectoryFailureProtocol.holdNextRequest()
    let older = Task { try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: ordering) }
    defer { older.cancel() }
    let olderRequest = try await heldRequest()
    DirectoryFailureProtocol.payload = try fixture([node("winning.vendor-cdn.com")])
    _ = try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: ordering)
    olderRequest.deliver(good)
    let returned = try await older.value
    let restoredWinner = SteamContentSourceDirectory.cachedServers(cellID: 77, appID: 42,
        cache: SteamContentSourceDirectory.Cache(fileURL: orderingURL))
    try require(returned.map(\.host) == ["winning.vendor-cdn.com"] && restoredWinner?.map(\.host) == returned.map(\.host),
        "a newer successful winner survives older concurrent completion and cache restart")
    let usefulURL = root.appendingPathComponent("independent-useful.json")
    let usefulCache = SteamContentSourceDirectory.Cache(fileURL: usefulURL)
    DirectoryFailureProtocol.holdNextRequest()
    let useful = Task { try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: usefulCache) }
    defer { useful.cancel() }
    let usefulRequest = try await heldRequest()
    DirectoryFailureProtocol.payload = nil
    _ = try await SteamContentSourceDirectory.fetch(cellID: 77, appID: 42, using: session, cache: usefulCache)
    usefulRequest.deliver(good)
    _ = try await useful.value
    try require(SteamContentSourceDirectory.cachedServers(cellID: 77, appID: 42,
        cache: SteamContentSourceDirectory.Cache(fileURL: usefulURL))?.map(\.host) == ["saved.vendor-cdn.com", "app43.vendor-cdn.com"],
        "a newer failed refresh cannot suppress an older successful authority")
    var futureAuthority = try JSONSerialization.jsonObject(with: persisted) as! [String: Any]
    futureAuthority["latestAuthority"] = good.base64EncodedString()
    futureAuthority["latestAuthorityAt"] = Date().timeIntervalSinceReferenceDate + 3600
    let futureURL = root.appendingPathComponent("future-authority.json")
    try JSONSerialization.data(withJSONObject: futureAuthority).write(to: futureURL, options: .atomic)
    try require(SteamContentSourceDirectory.cachedServers(cellID: 77,
        cache: SteamContentSourceDirectory.Cache(fileURL: futureURL)) == nil,
        "a future latest-authority timestamp is rejected before cache admission")
    for (name, bytes) in [("truncated", Data(persisted.prefix(40))), ("oversized", Data(repeating: 32, count: 6 * 1024 * 1024 + 1))] {
        let invalidURL = root.appendingPathComponent(name + ".json")
        try bytes.write(to: invalidURL, options: .atomic)
        try require(SteamContentSourceDirectory.cachedServers(cellID: 77,
            cache: SteamContentSourceDirectory.Cache(fileURL: invalidURL)) == nil,
            "\(name) cache is rejected before use")
    }
}
func mainlandSupplementChecks(using session: URLSession, root: URL) async throws {
    let empty = try fixture([])
    let mainland = try fixture(["st.dl.eccdnx.com", "dl.steam.clngaa.com", "xz.pphimalayanrt.com"].map {
        ["host": $0, "type": "CDN", "https_support": "unavailable"]
    })
    defer {
        DirectoryFailureProtocol.mainlandPayload = empty
        DirectoryFailureProtocol.mainlandFailure = false
        DirectoryFailureProtocol.payload = nil
    }
    let url = SteamContentSourceDirectory.directoryURL(cellID: 198, mainlandSupplement: true)
    let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
    try require(url.scheme == "https" && url.host == "api.steampowered.com" && query == [
        URLQueryItem(name: "cell_id", value: "0"), URLQueryItem(name: "max_servers", value: "100"),
        URLQueryItem(name: "ip_override", value: "114.114.114.114")],
        "mainland supplement uses the same official HTTPS API with an explicit public geolocation override")
    let cacheURL = root.appendingPathComponent("mainland-regions.json")
    let cache = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
    DirectoryFailureProtocol.payload = try fixture([node("old-session.vendor-cdn.com")])
    DirectoryFailureProtocol.mainlandPayload = mainland
    let initial = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: cache)
    try require(initial.count == 4 && initial.filter { $0.directorySource == .mainlandSupplement }.count == 3 &&
        initial.allSatisfy { $0.cachedAt == nil },
        "merged live rows retain independent primary and mainland recommendation provenance")
    try require(SteamContentSourceDirectory.transferFallbacks(cellID: 198, appID: 41, cache: cache).map(\.url) ==
        SteamContentSourceDirectory.fallbackServers.map(\.url),
        "successful regional discovery retains all four secure chunk alternatives when local origins reject content")
    try require(SteamContentSourceDirectory.transferFallbacks(cellID: 31, appID: 41, cache: cache).isEmpty,
        "cell drift or replacement cannot turn unmatched transfer authority into unrestricted alternatives")
    DirectoryFailureProtocol.payload = try fixture([node("fresh-session.vendor-cdn.com")])
    DirectoryFailureProtocol.mainlandFailure = true
    let failedSupplement = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: cache)
    try require(failedSupplement.map(\.host) == ["fresh-session.vendor-cdn.com", "st.dl.eccdnx.com", "dl.steam.clngaa.com", "xz.pphimalayanrt.com"] &&
        failedSupplement.first?.cachedAt == nil && failedSupplement.dropFirst().allSatisfy {
            $0.cachedAt != nil && $0.refreshFailure == .urlError(-1004)
        }, "failed supplement retains cached mainland nodes while primary recommendations refresh")
    DirectoryFailureProtocol.mainlandFailure = false
    DirectoryFailureProtocol.payload = nil
    DirectoryFailureProtocol.mainlandPayload = try fixture([["host": "st.dl.eccdnx.com", "type": "CDN", "https_support": "unavailable"]])
    let failedPrimary = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
        cache: SteamContentSourceDirectory.Cache(fileURL: cacheURL))
    try require(failedPrimary.map(\.host) == ["fresh-session.vendor-cdn.com", "st.dl.eccdnx.com"] &&
        failedPrimary.first?.cachedAt != nil && failedPrimary.last?.cachedAt == nil,
        "primary outage preserves its previous list while fresh mainland authority replaces only its own rows")
    let atomicCache = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
    let beforeCancel = try Data(contentsOf: cacheURL)
    DirectoryFailureProtocol.payload = try fixture([node("uncommitted-session.vendor-cdn.com")])
    DirectoryFailureProtocol.holdNextRequest(mainland: true)
    let cancelling = Task { try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: atomicCache) }
    defer { cancelling.cancel() }
    _ = try await heldRequest()
    cancelling.cancel()
    do { _ = try await cancelling.value; throw CheckFailure(message: "cancelled mainland query returned partial primary authority") }
    catch is CancellationError {}
    try require((try Data(contentsOf: cacheURL)) == beforeCancel,
                "cancellation between primary and supplemental queries commits no partial directory")
    DirectoryFailureProtocol.payload = try fixture([node("restricted-session.vendor-cdn.com", allowed: [42])])
    DirectoryFailureProtocol.mainlandPayload = empty
    let restricted = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: atomicCache)
    try require(restricted.count == 1 && !restricted[0].downloadEligible && restricted[0].directorySource == .session,
                "supplementary empty authority clears only mainland rows and retains disabled primary app restrictions")
    try require(SteamContentSourceDirectory.transferFallbacks(cellID: 198, appID: 41, cache: atomicCache).isEmpty,
        "secure chunk alternatives cannot bypass primary app restrictions")
    DirectoryFailureProtocol.payload = try fixture([])
    DirectoryFailureProtocol.mainlandPayload = mainland
    let primaryEmpty = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: atomicCache)
    try require(primaryEmpty.count == 3 && primaryEmpty.allSatisfy { $0.directorySource == .mainlandSupplement && $0.downloadEligible },
                "an empty primary source does not discard independently valid mainland recommendations")
    try require(SteamContentSourceDirectory.transferFallbacks(cellID: 198, appID: 41, cache: atomicCache).isEmpty,
        "secure chunk alternatives cannot bypass explicitly empty primary authority")
    DirectoryFailureProtocol.mainlandPayload = empty
    let orderingURL = root.appendingPathComponent("mainland-ordering.json")
    let ordering = SteamContentSourceDirectory.Cache(fileURL: orderingURL)
    DirectoryFailureProtocol.holdNextRequest(mainland: true)
    DirectoryFailureProtocol.payload = try fixture([node("older-session.vendor-cdn.com")])
    let older = Task { try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: ordering) }
    defer { older.cancel() }
    let olderSupplement = try await heldRequest()
    DirectoryFailureProtocol.payload = try fixture([node("winner-session.vendor-cdn.com")])
    DirectoryFailureProtocol.mainlandPayload = try fixture([["host": "xz.pphimalayanrt.com", "type": "CDN", "https_support": "unavailable"]])
    _ = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: ordering)
    olderSupplement.deliver(mainland)
    let returned = try await older.value
    let restored = SteamContentSourceDirectory.cachedServers(cellID: 198, appID: 41,
        cache: SteamContentSourceDirectory.Cache(fileURL: orderingURL))
    try require(returned.map(\.host) == ["winner-session.vendor-cdn.com", "xz.pphimalayanrt.com"] &&
        restored?.map(\.host) == returned.map(\.host),
        "a newer merged winner survives older supplemental completion and archive restart")
}
func regionalTimeoutFallbackChecks(using session: URLSession, root: URL) async throws {
    let empty = try fixture([])
    defer {
        DirectoryFailureProtocol.timeoutFailure = false
        DirectoryFailureProtocol.mainlandFailure = false
        DirectoryFailureProtocol.mainlandPayload = empty
        DirectoryFailureProtocol.payload = nil
    }
    DirectoryFailureProtocol.payload = nil
    DirectoryFailureProtocol.mainlandFailure = true
    DirectoryFailureProtocol.timeoutFailure = true
    let noCache = try await fetchIsolated(cellID: 198, appID: 41, using: session)
    let mainlandOrigins: Set<String> = ["http://st.dl.eccdnx.com", "http://dl.steam.clngaa.com", "http://xz.pphimalayanrt.com"]
    try require(noCache.count == 7 && Set(noCache.filter(\.downloadEligible).map(\.url)).isSuperset(of: mainlandOrigins),
                "both official API queries timing out without cache retain three mainland HTTP and four HTTPS fallback nodes")
    let cacheURL = root.appendingPathComponent("timeout-cached31.json")
    let cache = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
    DirectoryFailureProtocol.timeoutFailure = false
    DirectoryFailureProtocol.payload = try fixture((0..<31).map { node("cached\($0).vendor-cdn.com") })
    _ = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: cache)
    let persisted = try Data(contentsOf: cacheURL)
    DirectoryFailureProtocol.payload = nil
    DirectoryFailureProtocol.timeoutFailure = true
    let cached = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
        cache: SteamContentSourceDirectory.Cache(fileURL: cacheURL))
    try require(cached.count == 38 && Set(cached.filter(\.downloadEligible).map(\.url)).isSuperset(of: mainlandOrigins) &&
        cached.filter { !$0.isFallback }.count == 31 && (try Data(contentsOf: cacheURL)) == persisted,
        "dual API timeout retains cached 31 nodes plus all seven bundled alternatives without overwriting raw authority")
    DirectoryFailureProtocol.timeoutFailure = false
    DirectoryFailureProtocol.mainlandFailure = false
    DirectoryFailureProtocol.payload = empty
    DirectoryFailureProtocol.mainlandPayload = empty
    let emptyCache = SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("timeout-empty-authorities.json"))
    _ = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: emptyCache)
    DirectoryFailureProtocol.payload = nil
    DirectoryFailureProtocol.mainlandFailure = true
    DirectoryFailureProtocol.timeoutFailure = true
    try require(try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: emptyCache).isEmpty,
                "dual outage cannot bypass independently authoritative empty source directories")
    DirectoryFailureProtocol.timeoutFailure = false
    DirectoryFailureProtocol.mainlandFailure = false
    DirectoryFailureProtocol.payload = try fixture([node("cache2-hkg1.steamcontent.com", allowed: [42])])
    DirectoryFailureProtocol.mainlandPayload = try fixture([["host": "st.dl.eccdnx.com", "type": "CDN", "https_support": "unavailable", "allowed_app_ids": [42]]])
    let restrictedCache = SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("timeout-restricted-authorities.json"))
    _ = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: restrictedCache)
    DirectoryFailureProtocol.payload = nil
    DirectoryFailureProtocol.mainlandFailure = true
    DirectoryFailureProtocol.timeoutFailure = true
    let denied = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session, cache: restrictedCache)
    try require(denied.count == 2 && denied.allSatisfy { !$0.downloadEligible && !$0.isFallback },
                "cached app-restricted source authority remains displayed and cannot be bypassed with bundled requests")
}
@main struct Check {
    static func main() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [DirectoryFailureProtocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let mainland = try fixture(["st.dl.eccdnx.com", "dl.steam.clngaa.com", "xz.pphimalayanrt.com"].map {
            ["host": $0, "type": "CDN", "https_support": "unavailable"]
        })
        let retained = try SteamContentSourceDirectory.parse(mainland, appID: 42)
        try require(retained.map(\.host) == ["st.dl.eccdnx.com", "dl.steam.clngaa.com", "xz.pphimalayanrt.com"] &&
                    retained.allSatisfy { $0.url.hasPrefix("http://") },
                    "all three official HTTP-only mainland partners remain HTTP download candidates")
        try require(retained.allSatisfy { $0.downloadEligible && $0.cannotDownloadReason == nil },
                    "official public partner DNS is eligible without a vendor suffix allowlist")
        let mixedEntries: [[String: Any]] = [
            node("https.vendor-cdn.com"),
            ["host": "http.vendor-cdn.com", "type": "CDN", "https_support": "unavailable"],
            node("restricted.vendor-cdn.com", allowed: [43]),
            ["host": "proxy.vendor-cdn.com", "type": "CDN", "https_support": "mandatory", "use_as_proxy": true],
            ["host": "unsupported.vendor-cdn.com", "type": "Proxy", "https_support": "mandatory"],
            ["host": "future.vendor-cdn.com", "type": "CDN", "https_support": "future"],
            ["host": "china.vendor-cdn.com", "type": "CDN", "https_support": "mandatory", "steam_china_only": true],
            ["host": "invalid.vendor-cdn.com", "type": "CDN", "https_support": "mandatory", "allowed_app_ids": ["invalid"]],
            node("127.0.0.1"), node("192.168.1.5"), node("0x7f.0.0.0x1"), node("public.local"), node("public.test"), node("missing..host.com"),
            ["type": "CDN", "https_support": "mandatory"], node("https.vendor-cdn.com")]
        let mixed = try SteamContentSourceDirectory.parse(fixture(mixedEntries), appID: 42)
        try require(mixed.count == mixedEntries.count && mixed.filter(\.downloadEligible).map(\.url) == [
                    "https://https.vendor-cdn.com", "http://http.vendor-cdn.com", "https://https.vendor-cdn.com"],
                    "all official rows and duplicate rows remain visible while only supported public origins are eligible")
        try require(mixed.filter { !$0.downloadEligible }.allSatisfy { $0.cannotDownloadReason?.isEmpty == false } &&
                    Set(mixed.map(\.id)).count == mixed.count,
                    "unusable rows have explicit reasons and independent display identities")
        try require(try SteamContentSourceDirectory.parse(fixture(mixedEntries), appID: 43)[2].downloadEligible,
                    "app restrictions are reapplied without deleting directory rows")
        let many = try SteamContentSourceDirectory.parse(fixture((0..<1101).map { node("node\($0).vendor-cdn.com") }), appID: 42)
        try require(many.count == 1101 && many.last?.host == "node1100.vendor-cdn.com",
                    "neither inspection nor output silently truncates the official directory")
        do {
            _ = try SteamContentSourceDirectory.parse(Data(repeating: 32, count: 2 * 1024 * 1024 + 1), appID: 42)
            throw CheckFailure(message: "oversized response accepted")
        } catch let error as URLError { try require(error.code == .cannotParseResponse, "directory retains its 2 MiB parser bound") }
        DirectoryFailureProtocol.payload = try fixture((0..<31).map { node("session\($0).vendor-cdn.com") })
        DirectoryFailureProtocol.mainlandPayload = mainland
        let supplemented = try await fetchIsolated(cellID: 198, appID: 41, using: session)
        try require(supplemented.count == 34 && Set(supplemented.map(\.url)).isSuperset(of: [
            "http://st.dl.eccdnx.com", "http://dl.steam.clngaa.com", "http://xz.pphimalayanrt.com"]),
            "session directory's 31 nodes merge with all three official mainland HTTP recommendations")
        DirectoryFailureProtocol.mainlandPayload = Data(#"{"response":{"servers":[]}}"#.utf8)
        DirectoryFailureProtocol.payload = try fixture([])
        let webBeforeProvider = DirectoryFailureProtocol.requests
        let supplementBeforeProvider = DirectoryFailureProtocol.mainlandRequests
        let primaryCM = try fixture((0..<31).map { node("cm\($0).vendor-cdn.com") })
        let viaCM = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("cm-provider.json")),
            viaConnection: { cell, override in
                if cell == 198, override == nil { return primaryCM }
                if cell == 0, override == "114.114.114.114" { return mainland }
                throw URLError(.badServerResponse)
            })
        try require(viaCM.count == 34 && DirectoryFailureProtocol.requests == webBeforeProvider &&
            DirectoryFailureProtocol.mainlandRequests == supplementBeforeProvider,
            "official Steam connection supplies primary and mainland recommendations without any Web API requests")
        let beforeCMFallback = DirectoryFailureProtocol.requests
        let beforeSupplementFallback = DirectoryFailureProtocol.mainlandRequests
        DirectoryFailureProtocol.payload = mainland
        let cmFailureWebSuccess = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("cm-failed-web-good.json")),
            viaConnection: { _, _ in throw URLError(.cannotConnectToHost) })
        try require(cmFailureWebSuccess.count == 3 && DirectoryFailureProtocol.requests == beforeCMFallback + 1 &&
            DirectoryFailureProtocol.mainlandRequests == beforeSupplementFallback + 1,
            "failed official connection provider falls back to the same HTTPS API for both sources")
        DirectoryFailureProtocol.payload = nil
        DirectoryFailureProtocol.mainlandFailure = true
        DirectoryFailureProtocol.timeoutFailure = true
        let beforeCMOutage = try Data(contentsOf: root.appendingPathComponent("cm-provider.json"))
        let cmFailureWebFailure = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("cm-provider.json")),
            viaConnection: { _, _ in throw URLError(.cannotConnectToHost) })
        try require(cmFailureWebFailure.count == 38 && Set(cmFailureWebFailure.map(\.url)).isSuperset(of: [
            "http://st.dl.eccdnx.com", "http://dl.steam.clngaa.com", "http://xz.pphimalayanrt.com"]) &&
            (try Data(contentsOf: root.appendingPathComponent("cm-provider.json"))) == beforeCMOutage,
            "connection and Web API failures retain both cached sources plus bundled alternatives without raw-cache changes")
        let cmNoCache = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("cm-api-no-cache.json")),
            viaConnection: { _, _ in throw URLError(.cannotConnectToHost) })
        try require(cmNoCache.count == 7 && cmNoCache.filter { $0.directorySource == .bundledMainland }.count == 3,
                    "connection and Web API outages without cache still offer three bundled mainland HTTP candidates")
        let beforeCMCancel = DirectoryFailureProtocol.requests
        do {
            _ = try await SteamContentSourceDirectory.fetch(cellID: 198, appID: 41, using: session,
                cache: SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("cm-provider.json")),
                viaConnection: { _, _ in throw CancellationError() })
            throw CheckFailure(message: "cancelled CM provider became Web or bundled fallback")
        } catch is CancellationError {}
        try require(DirectoryFailureProtocol.requests == beforeCMCancel &&
            (try Data(contentsOf: root.appendingPathComponent("cm-provider.json"))) == beforeCMOutage,
            "official connection cancellation propagates without Web requests or cache mutation")
        DirectoryFailureProtocol.mainlandFailure = false
        DirectoryFailureProtocol.timeoutFailure = false
        try await regionalTimeoutFallbackChecks(using: session, root: root)
        DirectoryFailureProtocol.payload = mainland
        DirectoryFailureProtocol.finalURL = URL(string: "https://thirdparty.vendor-cdn.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/")!
        let redirectedDirectory = try await fetchIsolated(cellID: 201, appID: 42, using: session)
        try require(redirectedDirectory.allSatisfy(\.isFallback) && redirectedDirectory.allSatisfy { $0.refreshFailure == .invalidResponse },
                    "an untrusted final directory response URL cannot authorize returned CDN nodes")
        for finalURL in ["http://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/",
                         "https://api.steampowered.com/other-directory/",
                         "https://api.steampowered.com:8443/IContentServerDirectoryService/GetServersForSteamPipe/v1/"] {
            DirectoryFailureProtocol.finalURL = URL(string: finalURL)!
            let untrusted = try await fetchIsolated(cellID: 201, appID: 42, using: session)
            try require(untrusted.allSatisfy { $0.isFallback && $0.refreshFailure == .invalidResponse },
                        "directory trust rejects HTTP, wrong paths and nondefault ports: " + finalURL)
        }
        let policy = SteamContentSourceDirectory.RedirectPolicy()
        let directoryTask = session.dataTask(with: SteamContentSourceDirectory.directoryURL(cellID: 201))
        defer { directoryTask.cancel() }
        let redirectResponse = HTTPURLResponse(url: SteamContentSourceDirectory.directoryURL(cellID: 201),
            statusCode: 302, httpVersion: "HTTP/1.1", headerFields: nil)!
        for target in ["http://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/",
                       "https://thirdparty.vendor-cdn.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/",
                       "https://api.steampowered.com/other-directory/",
                       SteamContentSourceDirectory.directoryURL(cellID: 202).absoluteString] {
            let request = URLRequest(url: URL(string: target)!)
            var accepted: URLRequest?
            policy.urlSession(session, task: directoryTask, willPerformHTTPRedirection: redirectResponse,
                newRequest: request) { accepted = $0 }
            try require((accepted != nil) == SteamContentSourceDirectory.isTrustedDirectoryURL(request.url!),
                        "directory session redirect policy permits only its HTTPS API origin and exact path")
        }
        DirectoryFailureProtocol.finalURL = nil
        let cacheURL = root.appendingPathComponent("last-good.json")
        let cache = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
        DirectoryFailureProtocol.payload = mainland
        let live = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: cache)
        try require(live == retained && live.allSatisfy { !$0.isFallback && $0.cachedAt == nil },
                    "valid HTTP-only authority is returned directly without obsolete HTTPS fallback")
        let persisted = try Data(contentsOf: cacheURL)
        let restarted = SteamContentSourceDirectory.Cache(fileURL: cacheURL)
        let preload = SteamContentSourceDirectory.cachedServers(cellID: 201, appID: 42, cache: restarted)
        try require(preload?.map(\.url) == retained.map(\.url) && preload?.allSatisfy { $0.cachedAt != nil } == true,
                    "full HTTP directory survives cache restart and preload")
        let legacyURL = root.appendingPathComponent("legacy-v1.json")
        let legacy: [String: Any] = ["version": 1, "cellID": 201,
            "lastGood": try fixture([node("older.vendor-cdn.com")]).base64EncodedString(),
            "savedAt": Date().timeIntervalSinceReferenceDate - 120,
            "latestAuthority": mainland.base64EncodedString(),
            "latestAuthorityAt": Date().timeIntervalSinceReferenceDate - 60,
            "invalidated": false, "vetoedHosts": ["older.vendor-cdn.com"]]
        let legacyData = try JSONSerialization.data(withJSONObject: legacy)
        try legacyData.write(to: legacyURL, options: .atomic)
        let migrated = SteamContentSourceDirectory.cachedServers(cellID: 201, appID: 42,
            cache: SteamContentSourceDirectory.Cache(fileURL: legacyURL))
        try require(migrated?.map(\.url) == retained.map(\.url) && (try Data(contentsOf: legacyURL)) == legacyData,
                    "v1 migration uses the latest complete authority and preserves the user's original cache file")
        for mutation in ["version", "savedAt"] {
            var invalidArchive = legacy
            invalidArchive[mutation] = mutation == "version" ? 99 : Date().timeIntervalSinceReferenceDate + 3600
            let invalidURL = root.appendingPathComponent("invalid-\(mutation).json")
            try JSONSerialization.data(withJSONObject: invalidArchive).write(to: invalidURL, options: .atomic)
            try require(SteamContentSourceDirectory.cachedServers(cellID: 201,
                cache: SteamContentSourceDirectory.Cache(fileURL: invalidURL)) == nil,
                "invalid cached \(mutation) is rejected before use")
        }
        let requested = URLComponents(url: SteamContentSourceDirectory.directoryURL(cellID: 201), resolvingAgainstBaseURL: false)
        try require(requested?.scheme == "https" && requested?.host == "api.steampowered.com" &&
            requested?.queryItems?.contains(URLQueryItem(name: "cell_id", value: "201")) == true,
            "directory discovery preserves the user's recommended Steam cell over HTTPS")
        DirectoryFailureProtocol.payload = nil
        let beforeOutage = DirectoryFailureProtocol.requests
        let outage = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: restarted)
        try require(outage.map(\.url) == retained.map(\.url) && outage.allSatisfy { $0.refreshFailure == .urlError(-1004) } &&
                    DirectoryFailureProtocol.requests == beforeOutage + 2 && (try Data(contentsOf: cacheURL)) == persisted,
                    "bounded outage retries preserve the exact last directory and its safe failure reason")
        let fallback = try await fetchIsolated(cellID: 777, appID: 42, using: session)
        try require(fallback.count == 4 && fallback.allSatisfy { $0.isFallback && $0.url.hasPrefix("https://") },
                    "without a compatible authority an outage retains bundled secure alternatives")
        DirectoryFailureProtocol.payload = try fixture(mixedEntries)
        _ = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: restarted)
        DirectoryFailureProtocol.payload = nil
        let restricted = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: cacheURL))
        try require(restricted.count == mixedEntries.count && !restricted[2].downloadEligible,
                    "cached rejected rows remain disabled and cannot lead requests after restart")
        let allowedApp = SteamContentSourceDirectory.cachedServers(cellID: 201, appID: 43, cache: restarted)
        try require(allowedApp?[2].downloadEligible == true && SteamContentSourceDirectory.cachedServers(cellID: 202, cache: restarted) == nil,
                    "cache reapplies each app's restriction and separates Steam cells")
        DirectoryFailureProtocol.payload = try fixture([])
        _ = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: restarted)
        DirectoryFailureProtocol.payload = nil
        try require(try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session,
            cache: SteamContentSourceDirectory.Cache(fileURL: cacheURL)).isEmpty,
            "a successful empty authority cannot resurrect stale or bundled rows")
        DirectoryFailureProtocol.cancellation = true
        let beforeCancel = DirectoryFailureProtocol.requests
        do {
            _ = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: restarted)
            throw CheckFailure(message: "cancelled directory returned cache")
        } catch is CancellationError {}
        try require(DirectoryFailureProtocol.requests == beforeCancel + 1, "cancellation propagates without retry or fallback")
        DirectoryFailureProtocol.cancellation = false
        let ordering = SteamContentSourceDirectory.Cache(fileURL: root.appendingPathComponent("ordering.json"))
        DirectoryFailureProtocol.holdNextRequest()
        let old = Task { try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: ordering) }
        defer { old.cancel() }
        let held = try await heldRequest()
        DirectoryFailureProtocol.payload = try fixture([])
        _ = try await SteamContentSourceDirectory.fetch(cellID: 201, appID: 42, using: session, cache: ordering)
        held.deliver(mainland)
        try require(try await old.value.isEmpty, "newer successful authority wins over an older response completing late")
        try await independentCacheChecks(using: session, root: root)
        try await mainlandSupplementChecks(using: session, root: root)
        let allowed: Set<String> = ["http://st.dl.eccdnx.com", "https://https.vendor-cdn.com"]
        try require(SteamContentSourceDirectory.manualURL(selectedHost: "st.dl.eccdnx.com", allowed: allowed) == "http://st.dl.eccdnx.com" &&
                    SteamContentSourceDirectory.manualURL(selectedHost: "absent.vendor-cdn.com", allowed: allowed) == nil,
                    "manual choice resolves only its current directory origin including HTTP")
        for unsafe in ["http://127.0.0.1", "https://public.local", "http://vendor-cdn.com:8080", "https://user@vendor-cdn.com", "http://vendor-cdn.com/path"] {
            try require(SteamContentSourceDirectory.canonicalOrigin(unsafe) == nil, "unsafe manual request origin rejected: " + unsafe)
        }
        try require(SteamContentSourceDirectory.manualURL(selectedHost: "st.dl.eccdnx.com",
            allowed: ["http://st.dl.eccdnx.com", "https://st.dl.eccdnx.com"]) == "https://st.dl.eccdnx.com",
            "legacy bare-host preference prefers HTTPS when both transports are current")
        let ordered = SteamContentSourceDirectory.prioritize(["https://https.vendor-cdn.com", "http://st.dl.eccdnx.com"], selectedHost: "st.dl.eccdnx.com")
        try require(ordered.first == "http://st.dl.eccdnx.com", "manual HTTP source survives prioritization")
        let suite = "madeira-source-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SteamContentSourceStore(defaults: defaults)
        try require(store.select("ST.DL.ECCDNX.COM") && !store.select("127.0.0.1"), "partner preference normalizes while unsafe names cannot be selected")
        try require(SteamContentSourceStore(defaults: defaults).selectedHost == "st.dl.eccdnx.com",
                    "manual partner selection survives a new preference-store instance")
        let now = Date()
        store.remember([
            SteamContentHostRecord(host: "st.dl.eccdnx.com", cellID: 201, bytesPerSecond: 2000, successes: 1, updatedAt: now, scheme: "http"),
            SteamContentHostRecord(host: "st.dl.eccdnx.com", cellID: 201, bytesPerSecond: 20, successes: 1, updatedAt: now),
            SteamContentHostRecord(host: "expired.vendor-cdn.com", cellID: 201, bytesPerSecond: 9000, successes: 1, updatedAt: now.addingTimeInterval(-8 * 86400))
        ], cellID: 201)
        let restored = SteamContentSourceStore(defaults: defaults)
        try require(restored.history(for: "st.dl.eccdnx.com", cellID: 201, scheme: "http")?.bytesPerSecond == 2000 &&
                    restored.history(for: "st.dl.eccdnx.com", cellID: 201)?.bytesPerSecond == 20,
                    "verified HTTP and HTTPS histories remain separate with legacy HTTPS compatibility")
        try require(restored.rememberedHost(cellID: 201, available: Array(allowed)) == "http://st.dl.eccdnx.com" &&
                    restored.rememberedHost(cellID: 202, available: Array(allowed)) == nil &&
                    restored.rememberedHost(cellID: 201, available: ["https://st.dl.eccdnx.com"]) == "https://st.dl.eccdnx.com",
                    "remembered fastest origin includes transport and remains bound to current origins and cell")
        restored.forget(hosts: ["http://st.dl.eccdnx.com"], cellID: 201)
        try require(restored.history(for: "st.dl.eccdnx.com", cellID: 201, scheme: "http") == nil &&
                    restored.history(for: "st.dl.eccdnx.com", cellID: 201) != nil &&
                    restored.history(for: "expired.vendor-cdn.com", cellID: 201) == nil,
                    "observed transport failure removes only its origin and expired history is ignored")
        let hundred = (0..<100).map { index in
            SteamContentHostRecord(host: "measured\(index).vendor-cdn.com", cellID: 201,
                bytesPerSecond: Double(index + 1), successes: 1, updatedAt: now,
                scheme: index % 2 == 0 ? "http" : "https")
        }
        restored.remember(hundred, cellID: 201)
        let hundredReloaded = SteamContentSourceStore(defaults: defaults)
        try require(hundred.allSatisfy {
            hundredReloaded.history(for: $0.host, cellID: $0.cellID, scheme: $0.scheme ?? "https") == $0
        }, "all 100 official-node measurements survive remember and preference reload")
        let oversizedPreferences = Data(repeating: 32, count: 512 * 1024 + 1)
        defaults.set(oversizedPreferences, forKey: "madeira.steam.download-sources.v1")
        try require(SteamContentSourceStore(defaults: defaults).selectedHost == nil,
                    "oversized speed-history archives remain bounded before decode")
        let historyKey = "madeira.steam.download-sources.v1"
        defaults.set(Data(#"{"version":99,"selectedHost":"st.dl.eccdnx.com","records":[]}"#.utf8), forKey: historyKey)
        try require(SteamContentSourceStore(defaults: defaults).selectedHost == nil,
                    "unknown preference archive versions cannot restore selected nodes")
        defaults.set(Data(#"{"version":1,"selectedHost":"127.0.0.1","records":[]}"#.utf8), forKey: historyKey)
        try require(SteamContentSourceStore(defaults: defaults).selectedHost == nil,
                    "injected unsafe persisted selection cannot become a manual CDN preference")
        defaults.set(Data("corrupt-json".utf8), forKey: historyKey)
        try require(SteamContentSourceStore(defaults: defaults).selectedHost == nil,
                    "malformed saved preferences safely restore automatic mode")
        let invalidStore = SteamContentSourceStore(defaults: defaults)
        invalidStore.remember([
            SteamContentHostRecord(host: "injected.local", cellID: 201, bytesPerSecond: 9999, successes: 1, updatedAt: now),
            SteamContentHostRecord(host: "nonfinite.vendor-cdn.com", cellID: 201, bytesPerSecond: .nan, successes: 1, updatedAt: now),
            SteamContentHostRecord(host: "infinite.vendor-cdn.com", cellID: 201, bytesPerSecond: .infinity, successes: 1, updatedAt: now)
        ], cellID: 201)
        let invalidReloaded = SteamContentSourceStore(defaults: defaults)
        try require(["injected.local", "nonfinite.vendor-cdn.com", "infinite.vendor-cdn.com"].allSatisfy {
            invalidReloaded.history(for: $0, cellID: 201) == nil
        }, "injected and nonfinite speed history cannot survive preference reload")
        let chosen = "http://st.dl.eccdnx.com", automatic = "https://https.vendor-cdn.com"
        let health = ContentHostHealth(manualHost: chosen)
        health.recordSuccess(automatic, bytes: 2000, seconds: 0.1)
        health.recordSuccess(chosen, bytes: 2000, seconds: 2)
        try require(health.select([automatic, chosen], excluding: [], seed: 0) == chosen,
                    "manual source overrides faster automatic history")
        health.recordFailure(chosen, reason: "HTTP-503")
        try require(health.select([automatic, chosen], excluding: [], seed: 0) == automatic && health.manualFallbackNotice != nil,
                    "failed manual source falls back with an observable notice")
        let warmed = ContentHostHealth(rememberedHost: automatic)
        try require(warmed.select([chosen, automatic], excluding: [], seed: 0) == automatic,
                    "next install starts from a remembered verified origin")
        try require(DepotDownloader.contentHostPool(ordered, maximum: 2).contains(chosen),
                    "manual HTTP candidate survives downloader pool selection")
        try require(!SteamLog.lines.contains { $0.contains("synthetic-secret") }, "directory diagnostics never expose URL tokens")
    }
}
'''

def main() -> None:
    source = (CONTENT / "DepotDownloader.swift").read_text()
    core_path = CONTENT / "SteamDownloadSources.swift"
    has_core = core_path.exists()
    checks = CHECKS
    with tempfile.TemporaryDirectory(prefix="madeira-steam-source-") as temporary:
        work = Path(temporary)
        health = block(source, "final class ContentHostHealth:")
        pool = block(source, "nonisolated static func contentHostPool(")
        (work / "download.swift").write_text("import Foundation\nfinal class DepotDownloader {\n" + pool + "\n}\n" + health)
        (work / "checks.swift").write_text(checks)
        files = [str(work / "download.swift"), str(work / "checks.swift")]
        if has_core:
            files.append(str(core_path))
        executable = work / "check"
        compiled = subprocess.run(["xcrun", "swiftc", "-j", "1", "-parse-as-library", "-swift-version", "5",
                                   "-sanitize=address", "-g", *files, "-o", str(executable)],
                                  capture_output=True, text=True, timeout=120)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
        result = subprocess.run([str(executable), str(work / "cache")], capture_output=True, text=True, env=env, timeout=30)
        print(result.stdout, end="")
        if result.returncode:
            raise RuntimeError(result.stderr[-5000:])


if __name__ == "__main__":
    main()
