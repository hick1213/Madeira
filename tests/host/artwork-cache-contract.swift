// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 125hz
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
// Compile with app/Madeira/ArtworkCache.swift on macOS; requests stay in URLProtocol.
import Foundation
import ImageIO
import UniformTypeIdentifiers

private final class ArtworkFixture: URLProtocol, @unchecked Sendable {
    static let png = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")!
    static let largePNG: Data = {
        let context = CGContext(data: nil, width: 1200, height: 1800, bitsPerComponent: 8,
                                bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        context.setFillColor(CGColor(red: 1, green: 0, blue: 0, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: 1200, height: 1800))
        let bytes = NSMutableData()
        let destination = CGImageDestinationCreateWithData(bytes, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, context.makeImage()!, nil)
        precondition(CGImageDestinationFinalize(destination))
        return bytes as Data
    }()
    private static let lock = NSLock()
    private static var counts: [URL: Int] = [:]
    private static var failures = Set<URL>()
    static func calls(_ url: URL) -> Int { lock.lock(); defer { lock.unlock() }; return counts[url, default: 0] }
    static func fail(_ url: URL) { lock.lock(); defer { lock.unlock() }; failures.insert(url) }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        Self.lock.lock()
        Self.counts[url, default: 0] += 1
        let failed = Self.failures.contains(url)
        Self.lock.unlock()
        if failed {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
        } else {
            let data = url.path == "/invalid" ? Data("not an image".utf8) :
                (url.path == "/large" ? Self.largePNG : Self.png)
            let deliver = {
                self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
                self.client?.urlProtocol(self, didLoad: data)
                self.client?.urlProtocolDidFinishLoading(self)
            }
            if url.path == "/slow" { DispatchQueue.global().asyncAfter(deadline: .now() + 0.15, execute: deliver) }
            else { deliver() }
        }
    }
    override func stopLoading() {}
}

@main private enum ArtworkCacheContract {
    static func require(_ value: Bool, _ label: String) throws {
        guard value else { throw NSError(domain: "ArtworkCacheContract", code: 1, userInfo: [NSLocalizedDescriptionKey: label]) }
        print("PASS: \(label)")
    }
    static func main() async {
        do { try await run() } catch { print("FAIL: \(error.localizedDescription)"); exit(1) }
    }
    static func run() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("madeira-artwork-contract-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArtworkFixture.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let url = URL(string: "https://artwork.test/cover")!
        let directory = root.appendingPathComponent("persistent")
        let cache = ArtworkCache(directory: directory, session: session)
        let largeURL = URL(string: "https://artwork.test/large")!
        for (width, height, scale, expected) in [(28.0, 42.0, 2.0, 128), (48, 72, 2, 256),
                                                (164, 246, 2, 512), (200, 300, 3, 900),
                                                (2000, 3000, 3, 1600), (0, 0, 2, 128),
                                                (Double.infinity, 40, 2, 128), (40, 60, Double.nan, 128)] {
            let pixels = ArtworkCache.thumbnailPixelSize(width: width, height: height, scale: scale)
            try require(pixels == expected, "display bounds select stable thumbnail bucket \(expected)")
        }
        let rowPixels = ArtworkCache.thumbnailPixelSize(width: 28, height: 42, scale: 2)
        let rowBitmap = try await cache.bitmap(for: largeURL, maximumPixelSize: rowPixels)
        let repeatedRow = try await cache.bitmap(for: largeURL, maximumPixelSize: rowPixels)
        try require(rowBitmap.height == 128 && rowBitmap === repeatedRow,
                    "compact rows reuse a bounded bitmap instead of decoding a 900-pixel cover")
        let bitmap = try await cache.bitmap(for: largeURL, maximumPixelSize: 300)
        try require(bitmap.width == 200 && bitmap.height == 300,
                    "portrait artwork is decoded at the requested bound with aspect ratio preserved")
        let repeatedBitmap = try await cache.bitmap(for: largeURL, maximumPixelSize: 300)
        try require(bitmap === repeatedBitmap && ArtworkFixture.calls(largeURL) == 1,
                    "reappearing cards reuse the same decoded bitmap without another decode or download")
        let glow = try await cache.bitmap(for: largeURL, maximumPixelSize: 144)
        try require(glow.width == 96 && glow.height == 144 && glow !== bitmap,
                    "glow and card keep separate bounded bitmap sizes")
        let smallAgain = try await cache.bitmap(for: largeURL, maximumPixelSize: 300)
        try require(smallAgain === bitmap, "a glow request does not evict the card size")
        let cancelled = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await cache.bitmap(for: URL(string: "https://artwork.test/cancelled")!)
        }
        var cancelledCorrectly = false
        do { _ = try await cancelled.value } catch is CancellationError { cancelledCorrectly = true }
        try require(cancelledCorrectly && ArtworkFixture.calls(URL(string: "https://artwork.test/cancelled")!) == 0,
                    "a cancelled artwork consumer receives no bitmap and starts no request")
        let concurrentURL = URL(string: "https://artwork.test/bitmaps")!
        let decoded = try await withThrowingTaskGroup(of: CGImage.self) { group in
            for _ in 0..<12 { group.addTask { try await cache.bitmap(for: concurrentURL) } }
            var values: [CGImage] = []
            for try await value in group { values.append(value) }
            return values
        }
        try require(decoded.count == 12 && decoded.allSatisfy { $0 === decoded[0] } && ArtworkFixture.calls(concurrentURL) == 1,
                    "concurrent consumers share one decoded bitmap and request")
        let slowURL = URL(string: "https://artwork.test/slow")!
        let abandoned = Task { try await cache.bitmap(for: slowURL) }
        let surviving = Task { try await cache.bitmap(for: slowURL) }
        for _ in 0..<100 where ArtworkFixture.calls(slowURL) == 0 { try await Task.sleep(for: .milliseconds(1)) }
        abandoned.cancel()
        var abandonedCorrectly = false
        do { _ = try await abandoned.value } catch is CancellationError { abandonedCorrectly = true }
        let survivor = try await surviving.value
        try require(abandonedCorrectly && survivor.width == 1 && ArtworkFixture.calls(slowURL) == 1,
                    "cancelling a scrolled-away consumer does not cancel another card's shared request")
        let ceiling = try await cache.bitmap(for: largeURL, maximumPixelSize: Int.max)
        try require(ceiling.height == 1600, "oversized decode requests stay below the bitmap ceiling")
        let initial = try await cache.data(for: url)
        let repeatRead = try await cache.data(for: url)
        try require(initial == ArtworkFixture.png && repeatRead == initial && ArtworkFixture.calls(url) == 1,
                    "fresh artwork reuses memory without another request")
        ArtworkFixture.fail(url)
        let recreated = ArtworkCache(directory: directory, session: session)
        let diskRead = try await recreated.data(for: url)
        try require(diskRead == initial && ArtworkFixture.calls(url) == 1,
                    "a new cache instance reads disk while offline without contacting the server")

        let coalesced = URL(string: "https://artwork.test/concurrent")!
        let values = try await withThrowingTaskGroup(of: Data.self) { group in
            for _ in 0..<12 { group.addTask { try await cache.data(for: coalesced) } }
            var values: [Data] = []
            for try await value in group { values.append(value) }
            return values
        }
        try require(values.count == 12 && values.allSatisfy { $0 == initial } && ArtworkFixture.calls(coalesced) == 1,
                    "concurrent cover and glow readers share one request")

        let expired = ArtworkCache(directory: directory, session: session, maxAge: 0)
        let stale = try await expired.data(for: url)
        try require(stale == initial, "expired artwork remains available during offline refresh")
        let invalidURL = URL(string: "https://artwork.test/invalid")!
        let invalidDirectory = root.appendingPathComponent("invalid")
        let invalid = ArtworkCache(directory: invalidDirectory, session: session)
        var rejected = false
        do { _ = try await invalid.data(for: invalidURL) } catch { rejected = true }
        try require(rejected, "a successful HTTP response containing non-image data is rejected")
        let invalidFiles = (try? FileManager.default.contentsOfDirectory(atPath: invalidDirectory.path)) ?? []
        try require(invalidFiles.isEmpty, "invalid image responses never enter disk cache")

        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        let corruptDirectory = root.appendingPathComponent("corrupt")
        try FileManager.default.createDirectory(at: corruptDirectory, withIntermediateDirectories: true)
        for file in files { try Data("broken cache".utf8).write(to: corruptDirectory.appendingPathComponent(file.lastPathComponent)) }
        let repaired = ArtworkCache(directory: corruptDirectory, session: session)
        let repairedData = try await repaired.data(for: coalesced)
        try require(repairedData == initial && ArtworkFixture.calls(coalesced) == 2, "corrupt disk records are replaced with a valid download")

        let boundedDirectory = root.appendingPathComponent("bounded")
        let bounded = ArtworkCache(directory: boundedDirectory, session: session, capacity: 800)
        for number in 0..<10 { _ = try await bounded.data(for: URL(string: "https://artwork.test/bounded/\(number)")!) }
        let boundedFiles = try FileManager.default.contentsOfDirectory(at: boundedDirectory, includingPropertiesForKeys: [.fileSizeKey])
        let bytes = try boundedFiles.reduce(0) { total, file in total + (try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) }
        try require(!boundedFiles.isEmpty && boundedFiles.count < 10 && bytes <= 800, "disk cache evicts old artwork and stays within its byte limit")

        let blockedDirectory = root.appendingPathComponent("not-a-directory")
        try Data().write(to: blockedDirectory)
        let memoryOnly = ArtworkCache(directory: blockedDirectory, session: session)
        let memoryURL = URL(string: "https://artwork.test/memory-only")!
        let first = try await memoryOnly.data(for: memoryURL)
        let second = try await memoryOnly.data(for: memoryURL)
        try require(first == second && ArtworkFixture.calls(memoryURL) == 1, "unavailable disk storage preserves in-memory artwork loading")
    }
}
