// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 125hz
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
import Foundation
import CryptoKit
import ImageIO

/// Public game artwork only. Cached bytes outlive a view or app process; a card and
/// its glow share an in-flight request. Disk failures never prevent displaying art.
actor ArtworkCache {
    static let shared = ArtworkCache()
    private struct Record: Codable {
        let downloaded: Date
        let data: Data
    }
    private final class Box: NSObject {
        let record: Record
        init(_ record: Record) { self.record = record }
    }
    private final class BitmapBox: NSObject {
        let downloaded: Date
        let image: CGImage
        init(downloaded: Date, image: CGImage) { self.downloaded = downloaded; self.image = image }
    }
    private static let maximumImageBytes = 8 * 1024 * 1024
    private let directory: URL
    private let session: URLSession
    private let capacity: Int
    private let maxAge: TimeInterval
    private let memory = NSCache<NSURL, Box>()
    private let bitmaps = NSCache<NSString, BitmapBox>()
    private let retryAfter = NSCache<NSURL, NSDate>()
    private var pending: [URL: Task<Record, Error>] = [:]

    init(directory: URL = (FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("madeira-artwork", isDirectory: true),
         session: URLSession = .shared, capacity: Int = 128 * 1024 * 1024, maxAge: TimeInterval = 7 * 24 * 60 * 60) {
        self.directory = directory
        self.session = session
        self.capacity = max(0, capacity)
        self.maxAge = max(0, maxAge)
        memory.totalCostLimit = 24 * 1024 * 1024
        bitmaps.totalCostLimit = 32 * 1024 * 1024
        bitmaps.countLimit = 128
        retryAfter.countLimit = 512
    }

    func data(for url: URL) async throws -> Data {
        let record = try await record(for: url)
        try Task.checkCancellation()
        return record.data
    }

    /// Stable decode sizes keep small rows from filling the bitmap cache with
    /// full card images, while nearby layout sizes reuse the same thumbnail.
    nonisolated static func thumbnailPixelSize(width: Double, height: Double, scale: Double) -> Int {
        guard width.isFinite, height.isFinite, scale.isFinite,
              width > 0, height > 0, scale > 0 else { return 128 }
        let pixels = max(width, height) * scale
        return [128, 256, 512, 900, 1600].first { Double($0) >= pixels } ?? 1600
    }

    /// File access and eager ImageIO decode stay on this actor, away from view updates.
    /// Reappearing cards share decoded pixels as well as the downloaded bytes.
    func bitmap(for url: URL, maximumPixelSize: Int = 900) async throws -> CGImage {
        let record = try await record(for: url)
        try Task.checkCancellation()
        let size = min(1600, max(1, maximumPixelSize))
        let key = (url.absoluteString + "|pixels=\(size)") as NSString
        if let hit = bitmaps.object(forKey: key), hit.downloaded == record.downloaded { return hit.image }
        guard let image = Self.bitmap(from: record.data, maximumPixelSize: size) else {
            throw URLError(.cannotDecodeContentData)
        }
        try Task.checkCancellation()
        bitmaps.setObject(BitmapBox(downloaded: record.downloaded, image: image), forKey: key,
                          cost: image.bytesPerRow * image.height)
        return image
    }

    private func record(for url: URL) async throws -> Record {
        try Task.checkCancellation()
        guard url.scheme == "https" || url.scheme == "http" else { throw URLError(.unsupportedURL) }
        let canRetry = (retryAfter.object(forKey: url as NSURL)?.timeIntervalSinceNow ?? 0) <= 0
        if let record = cached(url) {
            if Date().timeIntervalSince(record.downloaded) >= maxAge, canRetry, pending[url] == nil {
                _ = startRequest(url)
            }
            return record
        }
        if let task = pending[url] { return try await task.value }
        guard canRetry else { throw URLError(.resourceUnavailable) }
        return try await startRequest(url).value
    }

    private func file(_ url: URL) -> URL {
        let hash = SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(hash + ".artwork")
    }

    private func cached(_ url: URL) -> Record? {
        if let box = memory.object(forKey: url as NSURL) { return box.record }
        let path = file(url)
        guard let attributes = try? path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
              attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              let size = attributes.fileSize, size <= Self.maximumImageBytes + 4096,
              let encoded = try? Data(contentsOf: path),
              let record = try? PropertyListDecoder().decode(Record.self, from: encoded),
              record.downloaded.timeIntervalSinceReferenceDate.isFinite,
              Self.isImage(record.data) else { return nil }
        memory.setObject(Box(record), forKey: url as NSURL, cost: record.data.count)
        try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: path.path)
        return record
    }

    private func startRequest(_ url: URL) -> Task<Record, Error> {
        let task = Task { try await self.download(url) }
        pending[url] = task
        return task
    }

    private func download(_ url: URL) async throws -> Record {
        defer { pending[url] = nil }
        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 20
            let (data, response) = try await session.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200, Self.isImage(data) else {
                throw URLError(.cannotDecodeContentData)
            }
            let record = Record(downloaded: Date(), data: data)
            memory.setObject(Box(record), forKey: url as NSURL, cost: data.count)
            retryAfter.removeObject(forKey: url as NSURL)
            let encoder = PropertyListEncoder()
            encoder.outputFormat = .binary
            if let encoded = try? encoder.encode(record), encoded.count <= capacity {
                try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                try? encoded.write(to: file(url), options: .atomic)
                trim()
            }
            return record
        } catch {
            // Missing primary capsules also get this short backoff, so rebuilding
            // the card does not repeatedly request a known-missing candidate.
            retryAfter.setObject(Date().addingTimeInterval(5 * 60) as NSDate, forKey: url as NSURL)
            throw error
        }
    }

    private nonisolated static func bitmap(from data: Data, maximumPixelSize: Int) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData,
                  [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize
        ] as CFDictionary)
    }

    private static func isImage(_ data: Data) -> Bool {
        guard !data.isEmpty, data.count <= maximumImageBytes,
              let source = CGImageSourceCreateWithData(data as CFData, nil), CGImageSourceGetCount(source) > 0,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
              let height = properties[kCGImagePropertyPixelHeight] as? NSNumber else { return false }
        return width.doubleValue > 0 && height.doubleValue > 0 &&
            width.doubleValue * height.doubleValue <= 16_000_000
    }

    private func trim() {
        guard let files = try? FileManager.default.contentsOfDirectory(at: directory,
            includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey, .isSymbolicLinkKey]) else { return }
        let entries = files.compactMap { path -> (URL, Int, Date)? in
            guard path.pathExtension == "artwork", let values = try? path.resourceValues(forKeys:
                [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey, .isSymbolicLinkKey]),
                values.isRegularFile == true, values.isSymbolicLink != true, let size = values.fileSize else { return nil }
            return (path, size, values.contentModificationDate ?? .distantPast)
        }.sorted { $0.2 < $1.2 }
        var bytes = entries.reduce(0) { $0 + $1.1 }
        for (path, size, _) in entries where bytes > capacity {
            do { try FileManager.default.removeItem(at: path); bytes -= size } catch { continue }
        }
    }
}

#if canImport(UIKit)
import SwiftUI
import UIKit

extension ArtworkCache {
    /// Decode a bounded bitmap rather than retaining a full-size store banner.
    static func image(from data: Data) -> UIImage? {
        guard let image = bitmap(from: data, maximumPixelSize: 1600) else { return nil }
        return UIImage(cgImage: image)
    }
}

/// The existing AsyncImage phase interface, backed by the shared artwork cache.
struct CachedArtwork<Content: View>: View {
    let url: URL?
    var maximumPixelSize = 900
    @ViewBuilder var content: (AsyncImagePhase) -> Content
    @State private var phase: AsyncImagePhase = .empty
    @State private var loadedURL: URL?
    @State private var loadedPixels = 0
    private struct Request: Hashable { let url: URL?; let pixels: Int }

    var body: some View {
        let pixels = min(1600, max(1, maximumPixelSize))
        content(loadedURL == url ? phase : .empty)
            .task(id: Request(url: url, pixels: pixels)) {
                phase = .empty
                loadedURL = url
                loadedPixels = pixels
                guard let url else { return }
                do {
                    let bitmap = try await ArtworkCache.shared.bitmap(for: url, maximumPixelSize: pixels)
                    guard !Task.isCancelled, loadedURL == url, loadedPixels == pixels else { return }
                    phase = .success(Image(uiImage: UIImage(cgImage: bitmap)))
                } catch {
                    if !Task.isCancelled, loadedURL == url, loadedPixels == pixels { phase = .failure(error) }
                }
            }
    }
}
#endif
