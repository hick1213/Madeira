// SPDX-License-Identifier: GPL-3.0-or-later
// BuildPatch chunk-format reference: Legendary contributors,
// https://github.com/legendary-gl/legendary/blob/master/legendary/models/chunk.py
// Native Swift implementation; no Madeira Converter Exception is applied.

import Foundation
import CryptoKit
import Darwin

enum EpicDownloader {
    typealias Fetch = @Sendable (URL, Int) async throws -> Data
    enum CDNFailure: Error, Codable, Equatable, Sendable {
        case http(Int), network(Int), integrity, other
    }
    struct CDNMeasurement: Codable, Equatable, Sendable {
        let host: String
        let scheme: String
        let bytesPerSecond: Double?
        let failed: Bool
        var failure: CDNFailure? = nil
    }

    struct TransferRateMeter {
        private var lastTime: TimeInterval
        private var lastBytes = 0
        private var reported = false
        init(now: TimeInterval) { lastTime = now }
        mutating func sample(bytes: Int, now: TimeInterval, finished: Bool = false) -> Double? {
            let elapsed = now - lastTime
            guard bytes >= lastBytes, elapsed.isFinite, elapsed > 0,
                  elapsed >= 1 || (finished && !reported) else { return nil }
            let rate = Double(bytes - lastBytes) / elapsed
            guard rate.isFinite, rate > 0 else { return nil }
            lastTime = now; lastBytes = bytes; reported = true
            return rate
        }
    }

    /// Reserve the encoded response, its decoder payload copy and decoded window.
    /// An individually larger supported chunk runs alone instead of being refused.
    static let prefetchByteBudget = 32 * 1024 * 1024
    static let maximumPrefetchParts = 64
    static func prefetchReservation(_ info: EpicChunkInfo) -> Int { 2 * info.fileSize + info.windowSize }

    static func prefetchWindow(parts: [EpicChunkPart], from start: Int, chunks: [String: EpicChunkInfo],
                               isCached: (EpicChunkInfo) throws -> Bool) throws -> (end: Int, missing: [EpicChunkInfo]) {
        var end = start, missing: [EpicChunkInfo] = [], seen = Set<String>(), reserved = 0
        while end < parts.count && end - start < maximumPrefetchParts {
            try Task.checkCancellation()
            guard let info = chunks[parts[end].guid] else { throw EpicDownloadError.invalidManifest }
            if seen.insert(info.guid).inserted, try !isCached(info) {
                let cost = prefetchReservation(info)
                if !missing.isEmpty && (missing.count == 4 || cost > prefetchByteBudget - reserved) { break }
                missing.append(info); reserved += cost
                end += 1
                if reserved > prefetchByteBudget { break }
            } else { end += 1 }
        }
        return (end, missing)
    }

    /// One common clock and byte counter for all overlapping network responses.
    /// File/cache/decompression progress never enters this counter.
    struct AggregateTransferRateMeter {
        private var meter: TransferRateMeter?
        private var active = 0, bytes = 0
        private var lastReportAt = -Double.infinity
        mutating func begin(now: TimeInterval) {
            if active == 0 && now - lastReportAt >= 1 { meter = TransferRateMeter(now: now); bytes = 0 }
            active += 1
        }
        mutating func receive(bytes count: Int, now: TimeInterval) -> Double? {
            guard active > 0, count > 0 else { return nil }
            bytes += count
            return sample(now: now, finished: false)
        }
        mutating func end(now: TimeInterval) -> Double? {
            guard active > 0 else { return nil }
            active -= 1
            return sample(now: now, finished: active == 0)
        }
        private mutating func sample(now: TimeInterval, finished: Bool) -> Double? {
            guard let rate = meter?.sample(bytes: bytes, now: now, finished: finished) else { return nil }
            lastReportAt = now
            return rate
        }
    }

    /// Chunk results may arrive after a newer attempt on the same origin.
    /// Accept health updates by actual attempt completion, not group delivery.
    struct CDNMeasurementClock {
        private var latest: [Int: UInt64] = [:]
        mutating func accept(origin: Int, completedAt: UInt64) -> Bool {
            guard latest[origin].map({ completedAt > $0 }) ?? true else { return false }
            latest[origin] = completedAt
            return true
        }
    }

    private struct CDNSelection {
        var clock = CDNMeasurementClock()
        var probed = false
        var rates: [Int: Double] = [:]
        var failed = Set<Int>()

        func order(count: Int) -> [Int] {
            (0..<count).sorted {
                if failed.contains($0) != failed.contains($1) { return !failed.contains($0) }
                let left = rates[$0] ?? 0, right = rates[$1] ?? 0
                return left == right ? $0 < $1 : left > right
            }
        }
    }

    static func fetchManifest(_ url: URL) async throws -> Data {
        let transport = try EpicCDNTransport(baseURLs: [url])
        defer { transport.finish() }
        do { return try await transport.fetch(url, maximum: EpicManifest.maximumManifestBytes, exactSize: false) }
        catch {
            if Task.isCancelled || error is CancellationError { throw CancellationError() }
            // Foundation errors may embed a signed CDN URL. Keep it out of UI
            // diagnostics by returning only our credential-free domain errors.
            throw (error as? EpicDownloadError) ?? .contentServer
        }
    }

    static func install(manifestData: Data, baseURLs: [URL], destination: URL,
                        progress: @escaping @Sendable (Int64, Int64) -> Void,
                        onTransferRate: (@Sendable (Double) -> Void)? = nil,
                        onCDNMeasurement: (@Sendable (CDNMeasurement) -> Void)? = nil) async throws -> EpicInstallResult {
        let transport = try EpicCDNTransport(baseURLs: baseURLs, onTransferRate: onTransferRate)
        defer { transport.finish() }
        let probeTransport = try EpicCDNTransport(baseURLs: baseURLs, probing: true, onTransferRate: onTransferRate)
        defer { probeTransport.finish() }
        return try await install(manifestData: manifestData, baseURLs: baseURLs, destination: destination,
                                 progress: progress, onCDNMeasurement: onCDNMeasurement,
                                 probeFetch: { try await probeTransport.fetch($0, maximum: $1) },
                                 fetch: { try await transport.fetch($0, maximum: $1) })
    }

    // Host tests inject bytes only at the network boundary. Parsing, hashes,
    // filesystem confinement, checkpoint and cancellation remain production code.
    static func install(manifestData: Data, baseURLs: [URL], destination: URL,
                        progress: @escaping @Sendable (Int64, Int64) -> Void,
                        onCDNMeasurement: (@Sendable (CDNMeasurement) -> Void)? = nil,
                        probeFetch: Fetch? = nil,
                        fetch: @escaping Fetch) async throws -> EpicInstallResult {
        try Task.checkCancellation()
        guard !baseURLs.isEmpty, baseURLs.count <= 16, baseURLs.allSatisfy(EpicCDNTransport.safeURL) else {
            throw EpicDownloadError.contentServer
        }
        let manifest = try EpicManifest.parse(manifestData)
        let store = try EpicInstallStore(destination: destination, manifestData: manifestData, appName: manifest.appName)
        var selection = CDNSelection()
        var completed: Int64 = 0
        progress(completed, manifest.totalBytes)
        for file in manifest.fileManifestList {
            try Task.checkCancellation()
            if try store.verifiedExisting(file) {
                completed += file.fileSize; progress(completed, manifest.totalBytes)
                continue
            }
            let (descriptor, partial) = try store.makePartial()
            do {
                defer { close(descriptor); store.discardPartial(partial) }
                var hash = Insecure.SHA1(), written: Int64 = 0
                var partIndex = 0
                while partIndex < file.chunkParts.count {
                    try Task.checkCancellation()
                    let window = try prefetchWindow(parts: file.chunkParts, from: partIndex, chunks: manifest.chunks,
                                                    isCached: { try store.cachedChunk($0) != nil })
                    var missing = window.missing
                    if !selection.probed, !missing.isEmpty {
                        // Preserve the serial verified benchmark of every API origin.
                        // Its winning payload is cached and released before fan-out.
                        _ = try await download(missing.removeFirst(), baseURLs: baseURLs, store: store,
                                               selection: &selection, onCDNMeasurement: onCDNMeasurement,
                                               probeFetch: probeFetch, fetch: fetch)
                    }
                    try await prefetch(missing, baseURLs: baseURLs, store: store, selection: &selection,
                                       onCDNMeasurement: onCDNMeasurement, fetch: fetch)
                    // The task group has fully drained. Only this parent owns disk
                    // writes, checkpoint publication and deterministic file ordering.
                    for part in file.chunkParts[partIndex..<window.end] {
                        try Task.checkCancellation()
                        guard let info = manifest.chunks[part.guid], let chunk = try store.cachedChunk(info) else {
                            throw EpicDownloadError.hashMismatch
                        }
                        let slice = chunk.subdata(in: part.offset..<(part.offset + part.size))
                        hash.update(data: slice)
                        try EpicInstallStore.write(descriptor, data: slice)
                        written += Int64(part.size)
                        progress(completed + written, manifest.totalBytes)
                    }
                    partIndex = window.end
                }
                guard written == file.fileSize, Data(hash.finalize()) == file.fileHash else {
                    throw EpicDownloadError.hashMismatch
                }
                try Task.checkCancellation()
                guard fsync(descriptor) == 0 else { throw EpicDownloadError.filesystem }
                try store.commitPartial(partial, descriptor: descriptor, file: file)
                completed += written
            } catch {
                throw error
            }
        }
        try Task.checkCancellation()
        // Re-read final names before publishing completion: a replaced partial
        // or a changed earlier file cannot turn into a falsely ready install.
        for file in manifest.fileManifestList {
            guard try store.verifiedExisting(file) else { throw EpicDownloadError.hashMismatch }
        }
        try Task.checkCancellation()
        try store.complete(chunks: manifest.chunks)
        progress(manifest.totalBytes, manifest.totalBytes)
        return EpicInstallResult(executable: manifest.launchExecutable, arguments: manifest.launchCommand,
                                 totalBytes: manifest.totalBytes)
    }

    private struct MeasurementEvent: Sendable {
        let index: Int
        let completedAt: UInt64
        let measurement: CDNMeasurement
    }
    private struct FetchedChunk: Sendable {
        let info: EpicChunkInfo
        let data: Data?
        let failure: EpicDownloadError?
        let events: [MeasurementEvent]
    }

    private static func prefetch(_ missing: [EpicChunkInfo], baseURLs: [URL], store: EpicInstallStore,
                                 selection: inout CDNSelection,
                                 onCDNMeasurement: (@Sendable (CDNMeasurement) -> Void)?, fetch: @escaping Fetch) async throws {
        guard !missing.isEmpty else { return }
        let order = selection.order(count: baseURLs.count)
        try await withThrowingTaskGroup(of: FetchedChunk.self) { group in
            for info in missing {
                group.addTask { try await fetchVerified(info, baseURLs: baseURLs, order: order, fetch: fetch) }
            }
            do {
                while let result = try await group.next() {
                    try Task.checkCancellation()
                    for event in result.events {
                        // A slow fallback must not overwrite newer health from a
                        // sibling that already completed on this same origin.
                        guard selection.clock.accept(origin: event.index, completedAt: event.completedAt) else { continue }
                        selection.rates[event.index] = event.measurement.bytesPerSecond
                        if event.measurement.failed { selection.failed.insert(event.index) }
                        else { selection.failed.remove(event.index) }
                        onCDNMeasurement?(event.measurement)
                    }
                    if let failure = result.failure { throw failure }
                    guard let data = result.data else { throw EpicDownloadError.contentServer }
                    try store.cacheChunk(result.info, data: data)
                }
            } catch {
                group.cancelAll()
                // Structured scope exit awaits even a noncooperative injected
                // network fetch; no child can outlive the store or cancellation.
                throw error
            }
        }
    }

    private static func fetchVerified(_ info: EpicChunkInfo, baseURLs: [URL], order: [Int], fetch: Fetch) async throws -> FetchedChunk {
        var events: [MeasurementEvent] = [], integrityFailure = false, unsupportedFailure = false
        for _ in 0..<2 {
            for index in order {
                try Task.checkCancellation()
                do {
                    let verified = try await transfer(info, base: baseURLs[index], fetch: fetch)
                    try Task.checkCancellation()
                    events.append(MeasurementEvent(index: index, completedAt: DispatchTime.now().uptimeNanoseconds, measurement: measurement(baseURLs[index], rate: verified.rate, failed: false)))
                    return FetchedChunk(info: info, data: verified.data, failure: nil, events: events)
                } catch is CancellationError { throw CancellationError() }
                catch {
                    if Task.isCancelled { throw CancellationError() }
                    events.append(MeasurementEvent(index: index, completedAt: DispatchTime.now().uptimeNanoseconds, measurement: measurement(baseURLs[index], rate: nil, failed: true,
                                                                                        failure: classifyFailure(error))))
                    if let error = error as? EpicDownloadError {
                        unsupportedFailure = unsupportedFailure || error == .unsupportedManifest
                        integrityFailure = integrityFailure || error == .hashMismatch || error == .invalidChunk
                    }
                }
            }
        }
        return FetchedChunk(info: info, data: nil,
                            failure: integrityFailure ? .hashMismatch : unsupportedFailure ? .unsupportedManifest : .contentServer,
                            events: events)
    }

    private static func download(_ info: EpicChunkInfo, baseURLs: [URL], store: EpicInstallStore,
                                 selection: inout CDNSelection,
                                 onCDNMeasurement: (@Sendable (CDNMeasurement) -> Void)?,
                                 probeFetch: Fetch?, fetch: Fetch) async throws -> Data {
        let probing = !selection.probed
        selection.probed = true
        var integrityFailure = false, unsupportedFailure = false
        var cachedBest = false, bestRate: Double?
        for attempt in 0..<2 {
            // Benchmark every supplied origin once, serially, using the first
            // missing chunk. Later transfers retain every origin for failover.
            let order = probing && attempt == 0 ? Array(baseURLs.indices) : selection.order(count: baseURLs.count)
            for index in order {
                try Task.checkCancellation()
                let base = baseURLs[index]
                let verified: (data: Data, rate: Double?)
                do {
                    if probing && attempt == 0, let probeFetch {
                        verified = try await transfer(info, base: base, fetch: probeFetch)
                    } else {
                        verified = try await transfer(info, base: base, fetch: fetch)
                    }
                } catch is CancellationError { throw CancellationError() }
                catch {
                    if Task.isCancelled { throw CancellationError() }
                    _ = selection.clock.accept(origin: index, completedAt: DispatchTime.now().uptimeNanoseconds)
                    selection.rates[index] = nil
                    selection.failed.insert(index)
                    onCDNMeasurement?(measurement(base, rate: nil, failed: true, failure: classifyFailure(error)))
                    if let error = error as? EpicDownloadError, error == .unsupportedManifest { unsupportedFailure = true }
                    if let error = error as? EpicDownloadError, error == .hashMismatch || error == .invalidChunk {
                        integrityFailure = true
                    }
                    continue
                }
                try Task.checkCancellation()
                _ = selection.clock.accept(origin: index, completedAt: DispatchTime.now().uptimeNanoseconds)
                selection.rates[index] = verified.rate
                selection.failed.remove(index)
                onCDNMeasurement?(measurement(base, rate: verified.rate, failed: false))
                try Task.checkCancellation()
                if !probing || attempt > 0 {
                    try store.cacheChunk(info, data: verified.data)
                    return verified.data
                }
                if !cachedBest || (verified.rate ?? 0) > (bestRate ?? 0) {
                    // Keep the best verified payload in the existing disk cache;
                    // no decoded best chunk is retained during another fetch.
                    try store.cacheChunk(info, data: verified.data)
                    cachedBest = true; bestRate = verified.rate
                }
            }
            if cachedBest {
                try Task.checkCancellation()
                guard let cached = try store.cachedChunk(info) else { throw EpicDownloadError.hashMismatch }
                return cached
            }
        }
        if integrityFailure { throw EpicDownloadError.hashMismatch }
        if unsupportedFailure { throw EpicDownloadError.unsupportedManifest }
        throw EpicDownloadError.contentServer
    }

    private static func transfer(_ info: EpicChunkInfo, base: URL, fetch: Fetch) async throws -> (data: Data, rate: Double?) {
        let start = ProcessInfo.processInfo.systemUptime
        let response = try await fetch(base.appendingPathComponent(info.path), info.fileSize)
        let elapsed = ProcessInfo.processInfo.systemUptime - start
        try Task.checkCancellation()
        let decoded = try decodeChunk(response, expected: info)
        try Task.checkCancellation()
        let rate = Double(response.count) / elapsed
        return (decoded, elapsed > 0 && elapsed.isFinite && rate > 0 && rate.isFinite ? rate : nil)
    }

    private static func classifyFailure(_ error: Error) -> CDNFailure {
        if let failure = error as? CDNFailure { return failure }
        if let network = error as? URLError { return .network(network.errorCode) }
        if let failure = error as? EpicDownloadError,
           failure == .hashMismatch || failure == .invalidChunk || failure == .unsupportedManifest { return .integrity }
        return .other
    }

    private static func measurement(_ base: URL, rate: Double?, failed: Bool, failure: CDNFailure? = nil) -> CDNMeasurement {
        CDNMeasurement(host: EpicCDNURLPolicy.host(base.host ?? "") ?? "", scheme: base.scheme?.lowercased() ?? "",
                       bytesPerSecond: rate, failed: failed, failure: failure)
    }

    static func decodeChunk(_ data: Data, expected: EpicChunkInfo) throws -> Data {
        guard data.count == expected.fileSize, data.count <= EpicManifest.maximumChunkBytes + 65536 else {
            throw EpicDownloadError.invalidChunk
        }
        var reader = EpicBinaryReader(data)
        guard try reader.u32() == 0xB1FE3AA2 else { throw EpicDownloadError.invalidChunk }
        let version = try reader.u32(), headerSize = Int(try reader.u32()), storedSize = Int(try reader.u32())
        let guid = try reader.guid(), rolling = try reader.u64(), storedAs = try reader.u8()
        if storedAs & 2 != 0 { throw EpicDownloadError.unsupportedManifest }
        guard version >= 1, version <= 4, storedAs & ~1 == 0,
              guid == expected.guid, rolling == expected.rollingHash else { throw EpicDownloadError.invalidChunk }
        var chunkSHA: Data?
        if version >= 2 {
            let sha = try reader.bytes(20), hashType = try reader.u8()
            guard hashType & ~3 == 0 else { throw EpicDownloadError.invalidChunk }
            if hashType & 2 != 0 { chunkSHA = sha }
        }
        let expanded = version >= 3 ? Int(try reader.u32()) : 1024 * 1024
        if version >= 4 {
            guard try reader.bytes(16) == Data(repeating: 0, count: 16) else { throw EpicDownloadError.unsupportedManifest }
            _ = try reader.bytes(16)
        }
        let knownHeader = version >= 4 ? 98 : version >= 3 ? 66 : version >= 2 ? 62 : 41
        guard headerSize == knownHeader, storedSize == data.count - headerSize,
              expanded == expected.windowSize, expanded <= EpicManifest.maximumChunkBytes else {
            throw EpicDownloadError.invalidChunk
        }
        let payload = try reader.bytes(storedSize)
        try reader.finish()
        let content = storedAs & 1 != 0 ? try EpicCompression.inflate(payload, expectedSize: expanded) : payload
        let sha = Data(Insecure.SHA1.hash(data: content))
        guard content.count == expanded, sha == expected.sha1, chunkSHA == nil || chunkSHA == sha else {
            throw EpicDownloadError.hashMismatch
        }
        return content
    }
}

/// No account credentials enter this session. Only supplied HTTPS CDN origins
/// are allowed; redirects cannot introduce a different origin or URL credentials.
final class EpicCDNTransport: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let origins: Set<String>
    private var session: URLSession!
    private let onTransferRate: (@Sendable (Double) -> Void)?
    private let rateLock = NSLock()
    private var rateMeter = EpicDownloader.AggregateTransferRateMeter()

    init(baseURLs: [URL], configuration: URLSessionConfiguration = .ephemeral, probing: Bool = false,
         onTransferRate: (@Sendable (Double) -> Void)? = nil) throws {
        guard !baseURLs.isEmpty, baseURLs.count <= 16, baseURLs.allSatisfy(Self.safeURL) else {
            throw EpicDownloadError.contentServer
        }
        origins = Set(baseURLs.map(Self.origin))
        self.onTransferRate = onTransferRate
        super.init()
        configuration.httpAdditionalHeaders = nil
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = probing ? 4 : 30
        configuration.timeoutIntervalForResource = probing ? 8 : 180
        configuration.httpMaximumConnectionsPerHost = probing ? 1 : 4
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }

    static func safeURL(_ url: URL) -> Bool {
        EpicCDNURLPolicy.isHTTPS(url)
    }

    private static func origin(_ url: URL) -> String { "\(EpicCDNURLPolicy.host(url.host ?? "") ?? ""):\(url.port ?? 443)" }

    func allows(_ url: URL) -> Bool { Self.safeURL(url) && origins.contains(Self.origin(url)) }

    func finish() { session.invalidateAndCancel() }

    func fetch(_ url: URL, maximum: Int, exactSize: Bool = true) async throws -> Data {
        guard allows(url), maximum > 0, maximum <= EpicManifest.maximumManifestBytes else {
            throw EpicDownloadError.contentServer
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let transfer = Transfer(transport: self, maximum: maximum, exactSize: exactSize)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                transfer.start(session: session, request: request, continuation: continuation)
            }
        } onCancel: {
            transfer.cancel()
        }
    }

    /// Receive network blocks directly; AsyncBytes can buffer slow, small
    /// deliveries until completion. Own one bounded buffer and continuation.
    private final class Transfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        private weak var transport: EpicCDNTransport?
        private let maximum: Int
        private let exactSize: Bool
        private let lock = NSLock()
        private var task: URLSessionDataTask?
        private var continuation: CheckedContinuation<Data, Error>?
        private var content = Data()
        private var finished = false
        private var accepted = false
        private var rateStarted = false

        init(transport: EpicCDNTransport, maximum: Int, exactSize: Bool) {
            self.transport = transport; self.maximum = maximum; self.exactSize = exactSize
            content.reserveCapacity(exactSize ? maximum : min(maximum, 1024 * 1024))
        }

        func start(session: URLSession, request: URLRequest, continuation: CheckedContinuation<Data, Error>) {
            let task = session.dataTask(with: request)
            task.delegate = self
            lock.lock()
            guard !finished else {
                lock.unlock(); task.cancel(); continuation.resume(throwing: CancellationError()); return
            }
            self.task = task; self.continuation = continuation
            transport?.beginTransfer(); rateStarted = true
            lock.unlock()
            task.resume()
        }

        func cancel() { finish(.failure(CancellationError())) }

        private func finish(_ result: Result<Data, Error>) {
            lock.lock()
            guard !finished else { lock.unlock(); return }
            finished = true
            let continuation = self.continuation, task = self.task
            self.continuation = nil; self.task = nil; content = Data()
            let ended = rateStarted; rateStarted = false
            lock.unlock()
            if ended { transport?.endTransfer(report: { if case .success = result { return true }; return false }()) }
            if case .failure = result { task?.cancel() }
            continuation?.resume(with: result)
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                completionHandler(.cancel); finish(.failure(EpicDownloader.CDNFailure.http(http.statusCode))); return
            }
            guard response is HTTPURLResponse,
                  response.url.flatMap({ transport?.allows($0) }) == true,
                  response.expectedContentLength == -1 || (exactSize
                    ? response.expectedContentLength == Int64(maximum)
                    : response.expectedContentLength >= 0 && response.expectedContentLength <= Int64(maximum)) else {
                completionHandler(.cancel); finish(.failure(EpicDownloadError.contentServer)); return
            }
            lock.lock(); let allow = !finished; accepted = allow; lock.unlock()
            completionHandler(allow ? .allow : .cancel)
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            lock.lock()
            guard !finished else { lock.unlock(); return }
            guard accepted, data.count <= maximum - content.count else {
                lock.unlock(); finish(.failure(EpicDownloadError.invalidChunk)); return
            }
            content.append(data)
            lock.unlock()
            // Rate consumers may cancel their task. Never invoke them while
            // holding this transfer's cancellation/continuation lock.
            transport?.receiveWireBytes(data.count)
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            if let error { finish(.failure(error)); return }
            lock.lock()
            guard !finished else { lock.unlock(); return }
            guard accepted, !content.isEmpty, !exactSize || content.count == maximum else {
                lock.unlock(); finish(.failure(EpicDownloadError.invalidChunk)); return
            }
            let data = content
            lock.unlock()
            finish(.success(data))
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            guard let transport else { completionHandler(nil); return }
            transport.urlSession(session, task: task, willPerformHTTPRedirection: response,
                                 newRequest: request, completionHandler: completionHandler)
        }
    }

    private func beginTransfer() {
        rateLock.lock(); defer { rateLock.unlock() }
        rateMeter.begin(now: ProcessInfo.processInfo.systemUptime)
    }

    private func receiveWireBytes(_ count: Int) {
        rateLock.lock()
        let rate = rateMeter.receive(bytes: count, now: ProcessInfo.processInfo.systemUptime)
        rateLock.unlock()
        if let rate { onTransferRate?(rate) }
    }

    private func endTransfer(report: Bool) {
        rateLock.lock()
        let rate = rateMeter.end(now: ProcessInfo.processInfo.systemUptime)
        rateLock.unlock()
        if report, let rate { onTransferRate?(rate) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, allows(url) else { completionHandler(nil); return }
        var clean = URLRequest(url: url)
        clean.httpMethod = "GET"
        clean.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        completionHandler(clean)
    }
}
