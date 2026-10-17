#!/usr/bin/env python3
"""Run native Epic parsing/install/network production code on BuildPatch fixtures.

Wire fixtures are independently encoded here, not translated from production
Swift. Everything except the network response runs unchanged. Ordinary fixtures,
compiler outputs and reports stay on the external disk. Install fixtures require
both APFS rename/link semantics and the real NSHomeDirectory container boundary,
so they use a scoped Home temporary directory, cleaned in finally after their
results are archived externally.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "app/Build/Products/IPAInspection/epic-container-regression/download"
SOURCES = [ROOT / "app/Madeira" / name for name in
           ("EpicCatalog.swift", "EpicDirectory.swift", "EpicManifest.swift", "EpicInstall.swift", "EpicDownloader.swift")]
GUIDS = ["01020304111213142122232431323334", "A1A2A3A4B1B2B3B4C1C2C3C4D1D2D3D4"]
ROLLING = [0xC011223344556677, 0x1020304050607080]
PAYLOADS = [b"MZnative fixture" + bytes(1024 * 1024 - 16),
            b"second payload" + bytes(1024 * 1024 - 14)]
FILES = [("Bin/Game.exe", [(0, 0, 16)]),
         ("Data/片段.bin", [(0, 2, 9), (1, 0, 14)]), ("empty.txt", [])]


def u32(number):
    return struct.pack("<I", number)


def blob(raw):
    return "".join(f"{byte:03d}" for byte in raw)


def number_blob(number, width):
    return blob(number.to_bytes(width, "little"))


def guid_words(guid):
    return struct.pack("<IIII", *(int(guid[i:i + 8], 16) for i in range(0, 32, 8)))


def fstring(text):
    if not text:
        return u32(0)
    try:
        raw = text.encode("ascii")
        return u32(len(raw) + 1) + raw + b"\0"
    except UnicodeEncodeError:
        raw = text.encode("utf-16le")
        return struct.pack("<i", -(len(raw) // 2 + 1)) + raw + b"\0\0"


def section(raw):
    return u32(len(raw) + 4) + raw


def contents(parts):
    return b"".join(PAYLOADS[chunk][offset:offset + size] for chunk, offset, size in parts)


def chunk(index, version=3, compressed=True):
    payload = PAYLOADS[index]
    encoded = zlib.compress(payload) if compressed else payload
    header_size = {1: 41, 2: 62, 3: 66, 4: 98}[version]
    header = struct.pack("<IIII", 0xB1FE3AA2, version, header_size, len(encoded))
    header += guid_words(GUIDS[index]) + struct.pack("<Q", ROLLING[index]) + bytes([int(compressed)])
    if version >= 2:
        header += hashlib.sha1(payload).digest() + b"\3"
    if version >= 3:
        header += u32(len(payload))
    if version >= 4:
        header += bytes(32)
    return header + encoded


def json_manifest():
    return {"ManifestFileVersion": number_blob(13, 4), "bIsFileData": False,
            "AppNameString": "EpicNativeFixture", "LaunchExeString": "Bin\\Game.exe",
            "LaunchCommand": "--windowed", "ChunkFilesizeList": {
                GUIDS[i]: number_blob(len(chunk(i)), 8) for i in range(2)},
            "ChunkHashList": {GUIDS[i]: number_blob(ROLLING[i], 8) for i in range(2)},
            "ChunkShaList": {GUIDS[i]: hashlib.sha1(PAYLOADS[i]).hexdigest() for i in range(2)},
            "DataGroupList": {GUIDS[i]: number_blob(i + 3, 1) for i in range(2)},
            "FileManifestList": [{"Filename": name, "FileHash": blob(hashlib.sha1(contents(parts)).digest()),
                                  "FileChunkParts": [{"Guid": GUIDS[i], "Offset": number_blob(offset, 4),
                                                      "Size": number_blob(size, 4)} for i, offset, size in parts]}
                                 for name, parts in FILES]}


def binary_manifest(feature=18, compressed=True, file_version=0, chunk_version=3):
    metadata = bytes([2]) + u32(feature) + b"\0" + u32(0)
    metadata += b"".join(fstring(text) for text in ["EpicNativeFixture", "1.2.3", "Bin/Game.exe", "--windowed"])
    metadata += u32(1) + fstring("fixture")
    metadata += b"".join(fstring(text) for text in ["", "", "", "build-id", "", ""])
    cdl = b"\0" + u32(2) + b"".join(guid_words(guid) for guid in GUIDS)
    cdl += b"".join(struct.pack("<Q", value) for value in ROLLING)
    cdl += b"".join(hashlib.sha1(payload).digest() for payload in PAYLOADS)
    cdl += bytes([3, 4]) + u32(len(PAYLOADS[0])) + u32(len(PAYLOADS[1]))
    cdl += b"".join(struct.pack("<q", len(chunk(i, version=chunk_version))) for i in range(2))
    if feature >= 22:
        cdl += bytes(32) + u32(0) + u32(0) + bytes(32)
    fml = bytes([file_version]) + u32(len(FILES))
    fml += b"".join(fstring(name) for name, _ in FILES) + fstring("") * len(FILES)
    fml += b"".join(hashlib.sha1(contents(parts)).digest() for _, parts in FILES) + bytes([4, 0, 0])
    fml += (u32(1) + fstring("Windows")) * len(FILES)
    for _, parts in FILES:
        fml += u32(len(parts))
        for i, offset, size in parts:
            fml += section(guid_words(GUIDS[i]) + u32(offset) + u32(size))
    if file_version >= 1:
        fml += (u32(1) + bytes(16)) * len(FILES) + fstring("application/octet-stream") * len(FILES)
    if file_version >= 2:
        fml += bytes(32) * len(FILES)
    body = section(metadata) + section(cdl) + section(fml) + section(b"\0" + u32(0))
    if feature >= 22:
        body += section(b"\0" + u32(0))
    packed = zlib.compress(body) if compressed else body
    header_size = 73 if feature >= 22 else 41
    header = struct.pack("<IIII", 0x44BEC00C, header_size, len(body), len(packed))
    header += hashlib.sha1(body).digest() + bytes([int(compressed)]) + u32(feature)
    if feature >= 22:
        header += bytes(32)
    return header + packed


def write_fixtures(directory):
    directory.mkdir(parents=True, exist_ok=True)
    good = json_manifest()
    (directory / "manifest.json").write_text(json.dumps(good, ensure_ascii=False))
    percent = deepcopy(good)
    percent["LaunchExeString"] = "Bin\\Game%00tail.exe"
    percent["FileManifestList"][0]["Filename"] = "Bin/Game%00tail.exe"
    (directory / "manifest-percent.json").write_text(json.dumps(percent, ensure_ascii=False))
    for feature, compressed, version in [(18, True, 0), (18, False, 2), (22, True, 1), (24, False, 2)]:
        (directory / f"manifest-{feature}-{int(compressed)}-{version}.bin").write_bytes(
            binary_manifest(feature, compressed, version))
    for version in range(1, 5):
        (directory / f"chunk-v{version}.bin").write_bytes(chunk(0, version))
    (directory / "chunk-raw.bin").write_bytes(chunk(0, compressed=False))
    for i in range(2):
        (directory / f"chunk-{GUIDS[i]}.bin").write_bytes(chunk(i))
    many = deepcopy(good)
    many["FileManifestList"] = [
        {"Filename": "Bin/Game.exe" if index == 0 else f"small/file-{index:03d}.bin",
         "FileHash": blob(hashlib.sha1(PAYLOADS[0][:16]).digest()),
         "FileChunkParts": [{"Guid": GUIDS[0], "Offset": number_blob(0, 4), "Size": number_blob(16, 4)}]}
        for index in range(96)]
    (directory / "many-small-files.json").write_text(json.dumps(many))
    parallel = deepcopy(good)
    parallel["ChunkFilesizeList"] = {}; parallel["ChunkHashList"] = {}
    parallel["ChunkShaList"] = {}; parallel["DataGroupList"] = {}
    parallel_parts = []; expected = b""
    for index in range(9):
        guid = f"{index + 101:032X}"
        payload = f"MZparallel-{index:02d}".encode().ljust(1024 * 1024, bytes([index + 32]))
        packed = zlib.compress(payload)
        encoded = struct.pack("<IIII", 0xB1FE3AA2, 3, 66, len(packed))
        encoded += guid_words(guid) + struct.pack("<Q", ROLLING[0]) + b"\1"
        encoded += hashlib.sha1(payload).digest() + b"\3" + u32(len(payload)) + packed
        (directory / f"chunk-{guid}.bin").write_bytes(encoded)
        parallel["ChunkFilesizeList"][guid] = number_blob(len(encoded), 8)
        parallel["ChunkHashList"][guid] = number_blob(ROLLING[0], 8)
        parallel["ChunkShaList"][guid] = hashlib.sha1(payload).hexdigest()
        parallel["DataGroupList"][guid] = number_blob(3, 1)
        repetitions = 2 if index == 3 else 1
        for _ in range(repetitions):
            parallel_parts.append({"Guid": guid, "Offset": number_blob(index, 4), "Size": number_blob(16, 4)})
            expected += payload[index:index + 16]
    parallel["FileManifestList"] = [{"Filename": "Bin/Game.exe", "FileHash": blob(hashlib.sha1(expected).digest()),
                                     "FileChunkParts": parallel_parts}]
    (directory / "parallel.json").write_text(json.dumps(parallel))
    (directory / "parallel-expected.bin").write_bytes(expected)
    health = deepcopy(parallel)
    for key in ("ChunkFilesizeList", "ChunkHashList", "ChunkShaList", "DataGroupList"):
        health[key] = dict(list(health[key].items())[:3])
    health["FileManifestList"][0]["FileChunkParts"] = parallel_parts[:3]
    health["FileManifestList"][0]["FileHash"] = blob(hashlib.sha1(expected[:48]).digest())
    (directory / "stale-health.json").write_text(json.dumps(health))
    invalid = []
    for name in ["../x", "..\\x", "C:\\x", "/x", "//x", "a//x", "x.", "NUL.exe",
                 "COM1.txt", "x:y", "x|y", "a/./b", "a/../b", ".madeira-epic/x", "bad\0.exe"]:
        manifest = deepcopy(good)
        manifest["LaunchExeString"] = name
        manifest["FileManifestList"][0]["Filename"] = name
        invalid.append(manifest)
    for other in ["bin/game.exe", "Bin", "Bin/../Game.exe"]:
        manifest = deepcopy(good)
        manifest["FileManifestList"].append({**manifest["FileManifestList"][0], "Filename": other})
        invalid.append(manifest)
    malformed = deepcopy(good)
    malformed["FileManifestList"][0]["FileHash"] = "999" * 20
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["FileManifestList"][0]["FileChunkParts"][0]["Offset"] = number_blob(0xFFFFFFFF, 4)
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["ChunkFilesizeList"][GUIDS[0]] = number_blob(0xFFFFFFFFFFFFFFFF, 8)
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["FileManifestList"][0]["SymlinkTarget"] = "outside"
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["ChunkShaList"][GUIDS[0]] = "0" * 38 + "é"
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["LaunchExeString"] = "missing.exe"
    invalid.append(malformed)
    malformed = deepcopy(good)
    malformed["bIsFileData"] = True
    invalid.append(malformed)
    for i, manifest in enumerate(invalid):
        (directory / f"invalid-{i}.json").write_text(json.dumps(manifest))
    (directory / "invalid-count.txt").write_text(str(len(invalid)))
    file_bad = deepcopy(good)
    file_bad["FileManifestList"][0]["FileHash"] = blob(bytes(20))
    (directory / "bad-file-hash.json").write_text(json.dumps(file_bad))
    changed = deepcopy(good)
    changed["LaunchCommand"] = "--other-build"
    (directory / "changed.json").write_text(json.dumps(changed))


RUNNER = r"""
import Foundation
import CryptoKit
import Darwin

func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw NSError(domain: "EpicTest", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
}

actor ChunkSource {
    let fixtures: URL
    var seen: [String] = []
    var cancelledName: String?
    var corrupt = false
    var slow = false
    var ignoresCancellation = false
    init(_ fixtures: URL, cancelledName: String? = nil, corrupt: Bool = false, slow: Bool = false, ignoresCancellation: Bool = false) {
        self.fixtures = fixtures; self.cancelledName = cancelledName; self.corrupt = corrupt; self.slow = slow; self.ignoresCancellation = ignoresCancellation
    }
    func fetch(_ url: URL, _ maximum: Int) async throws -> Data {
        try require(EpicCDNTransport.safeURL(url), "fetch received unsafe URL")
        seen.append(url.lastPathComponent)
        if slow { try await Task.sleep(for: .seconds(20)) }
        if ignoresCancellation {
            await withCheckedContinuation { continuation in
                DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(100)) { continuation.resume() }
            }
        }
        if let cancelledName, url.lastPathComponent.contains(cancelledName) { throw CancellationError() }
        let guid = url.lastPathComponent.split(separator: "_").last!.split(separator: ".").first!
        var data = try Data(contentsOf: fixtures.appendingPathComponent("chunk-\(guid).bin"))
        try require(data.count == maximum, "manifest expected compressed chunk length")
        if corrupt { data[data.count - 1] ^= 1 }
        return data
    }
    func count() -> Int { seen.count }
}

actor RatedChunkSource {
    let fixtures: URL
    let corruptHost: String?
    let unsupportedHost: String?
    let corruptSecondHost: String?
    var seen: [(String, String)] = []
    init(_ fixtures: URL, corruptHost: String? = nil, unsupportedHost: String? = nil, corruptSecondHost: String? = nil) {
        self.fixtures = fixtures; self.corruptHost = corruptHost; self.unsupportedHost = unsupportedHost
        self.corruptSecondHost = corruptSecondHost
    }
    func fetch(_ url: URL, _ maximum: Int) async throws -> Data {
        let host = url.host!
        let guid = String(url.lastPathComponent.split(separator: "_").last!.split(separator: ".").first!)
        seen.append((host, guid))
        // The fastest corrupt response must never win a verified benchmark.
        try await Task.sleep(for: .milliseconds(host == "slow.example.net" ? 60 : host == "fast.example.net" ? 15 : 1))
        var data = try Data(contentsOf: fixtures.appendingPathComponent("chunk-\(guid).bin"))
        try require(data.count == maximum, "rated fetch expected exact chunk size")
        if host == corruptHost || (host == corruptSecondHost && guid.hasPrefix("A1A2A3A4")) { data[data.count - 1] ^= 1 }
        if host == unsupportedHost { data[40] = 2 }
        return data
    }
    func requests() -> [(String, String)] { seen }
}

actor ParallelChunkSource {
    let fixtures: URL
    var active = 0, peak = 0, finished = 0
    var seen: [String] = []
    init(_ fixtures: URL) { self.fixtures = fixtures }
    func fetch(_ url: URL, _ maximum: Int) async throws -> Data {
        let guid = String(url.lastPathComponent.split(separator: "_").last!.split(separator: ".").first!)
        seen.append(guid); active += 1; peak = max(peak, active)
        // Deliberately noncooperative: the structured parent must drain us.
        let delay = 50 + (Int(guid.suffix(2), radix: 16)! % 4) * 20
        await withCheckedContinuation { continuation in
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(delay)) { continuation.resume() }
        }
        active -= 1; finished += 1
        let data = try Data(contentsOf: fixtures.appendingPathComponent("chunk-\(guid).bin"))
        try require(data.count == maximum, "parallel network receives exact compressed bound")
        return data
    }
    func stats() -> (active: Int, peak: Int, finished: Int, seen: [String]) { (active, peak, finished, seen) }
}

actor TimedOutProbeSource {
    var seen: [URL] = []
    func fetch(_ url: URL, _ maximum: Int) async throws -> Data {
        seen.append(url)
        throw URLError(.timedOut)
    }
    func requests() -> [URL] { seen }
}

actor StaleHealthChunkSource {
    let fixtures: URL
    var seen: [(String, String)] = []
    init(_ fixtures: URL) { self.fixtures = fixtures }
    func fetch(_ url: URL, _ maximum: Int) async throws -> Data {
        let host: String = url.host!
        let guid: String = String(url.lastPathComponent.split(separator: "_").last!.split(separator: ".").first!)
        seen.append((host, guid))
        if host == "fast.example.net", guid.hasSuffix("66") {
            try await Task.sleep(for: .milliseconds(1))
            throw URLError(.cannotConnectToHost)
        }
        let delay: Int
        if guid.hasSuffix("65") { delay = host == "slow.example.net" ? 60 : 10 }
        else { delay = host == "slow.example.net" ? 150 : 100 }
        try await Task.sleep(for: .milliseconds(delay))
        let data: Data = try Data(contentsOf: fixtures.appendingPathComponent("chunk-\(guid).bin"))
        try require(data.count == maximum, "stale-health network receives exact compressed bound")
        return data
    }
    func requests() -> [(String, String)] { seen }
}

final class CDNObserver: @unchecked Sendable {
    private let lock = NSLock()
    private var results: [EpicDownloader.CDNMeasurement] = []
    func record(_ result: EpicDownloader.CDNMeasurement) { lock.lock(); results.append(result); lock.unlock() }
    func values() -> [EpicDownloader.CDNMeasurement] { lock.lock(); defer { lock.unlock() }; return results }
}

final class TransferRateObserver: @unchecked Sendable {
    private let lock = NSLock()
    private var rates: [Double] = []
    func record(_ rate: Double) { lock.lock(); rates.append(rate); lock.unlock() }
    func values() -> [Double] { lock.lock(); defer { lock.unlock() }; return rates }
}

final class ProtocolState: @unchecked Sendable {
    let lock = NSLock()
    var payload = Data()
    var status = 200
    var unknownLength = false
    var delay: TimeInterval = 0
    var trickle = false
    var requests: [URLRequest] = []
    func set(_ payload: Data, status: Int = 200, unknownLength: Bool = false, delay: TimeInterval = 0, trickle: Bool = false) { lock.lock(); defer { lock.unlock() }; self.payload = payload; self.status = status; self.unknownLength = unknownLength; self.delay = delay; self.trickle = trickle; requests = [] }
    func receive(_ request: URLRequest) -> (Data, Int, Bool, TimeInterval, Bool) { lock.lock(); defer { lock.unlock() }; requests.append(request); return (payload, status, unknownLength, delay, trickle) }
    func last() -> URLRequest? { lock.lock(); defer { lock.unlock() }; return requests.last }
    func requestCount() -> Int { lock.lock(); defer { lock.unlock() }; return requests.count }
}

final class CheckpointObserver: @unchecked Sendable {
    private let lock = NSLock()
    private let path: String
    private var snapshots = Set<String>()
    init(_ destination: URL) { path = destination.appendingPathComponent(".madeira-epic/checkpoint.json").path }
    func observe() {
        var status = stat()
        guard lstat(path, &status) == 0 else { return }
        let value = "\(status.st_ino):\(status.st_mtimespec.tv_sec):\(status.st_mtimespec.tv_nsec)"
        lock.lock(); snapshots.insert(value); lock.unlock()
    }
    func replacements() -> Int { lock.lock(); defer { lock.unlock() }; return snapshots.count }
}

final class FixtureProtocol: URLProtocol, @unchecked Sendable {
    static let state = ProtocolState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    private let stopLock = NSLock()
    private var stopped = false
    override func startLoading() {
        let (data, status, unknownLength, delay, trickle) = Self.state.receive(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: unknownLength ? [:] : ["Content-Length": String(data.count)])!
        client!.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if trickle {
            guard let task = self.task as? URLSessionDataTask,
                  let delegate = task.delegate as? URLSessionDataDelegate else { fatalError("missing production receive delegate") }
            // CFNetwork coalesces very small URLProtocol bodies. Drive the
            // declared response/data delegate boundary for this slow fixture;
            // all production source and ordinary URLProtocol paths stay intact.
            delegate.urlSession?(URLSession.shared, dataTask: task, didReceive: response) { disposition in
                precondition(disposition == .allow)
            }
            DispatchQueue.global().async {
                for offset in stride(from: 0, to: data.count, by: 4) {
                    Thread.sleep(forTimeInterval: 1.05)
                    self.stopLock.lock()
                    guard !self.stopped else { self.stopLock.unlock(); return }
                    delegate.urlSession?(URLSession.shared, dataTask: task,
                        didReceive: data.subdata(in: offset..<min(data.count, offset + 4)))
                    self.stopLock.unlock()
                }
                self.stopLock.lock(); defer { self.stopLock.unlock() }
                if !self.stopped {
                    delegate.urlSession?(URLSession.shared, task: task, didCompleteWithError: nil)
                    self.client?.urlProtocolDidFinishLoading(self)
                }
            }
        } else if delay > 0 {
            DispatchQueue.global().asyncAfter(deadline: .now() + delay) { self.deliver(data) }
        } else { deliver(data) }
    }
    private func deliver(_ data: Data) {
        stopLock.lock(); defer { stopLock.unlock() }
        guard !stopped else { return }
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { stopLock.lock(); stopped = true; stopLock.unlock() }
}

@main struct EpicTests {
    @MainActor private static func checkPrefetchBudgetAndAggregateMeter() async throws {
        func budgetInfo(_ guid: String, fileSize: Int, window: Int) -> EpicChunkInfo {
            EpicChunkInfo(guid: guid, rollingHash: 0, sha1: Data(repeating: 0, count: 20), group: 0,
                          windowSize: window, fileSize: fileSize, featureLevel: 13)
        }
        let budgetChunks: [String: EpicChunkInfo] = ["a": budgetInfo("a", fileSize: 8 * 1024 * 1024, window: 1024 * 1024),
                            "b": budgetInfo("b", fileSize: 8 * 1024 * 1024, window: 1024 * 1024),
                            "c": budgetInfo("c", fileSize: 20 * 1024 * 1024, window: 1024 * 1024)]
        let budgetGUIDs: [String] = ["a", "a", "b", "c"]
        let budgetParts: [EpicChunkPart] = budgetGUIDs.map { EpicChunkPart(guid: $0, offset: 0, size: 1) }
        let budgetWindow = try EpicDownloader.prefetchWindow(parts: budgetParts, from: 0, chunks: budgetChunks, isCached: { _ in false })
        try require(budgetWindow.end == 2 && budgetWindow.missing.map(\.guid) == ["a"],
                    "memory budget includes two encoded copies plus decoded window and deduplicates repeated GUIDs")
        let oversizedWindow = try EpicDownloader.prefetchWindow(parts: budgetParts, from: 3, chunks: budgetChunks, isCached: { _ in false })
        try require(oversizedWindow.end == 4 && oversizedWindow.missing.map(\.guid) == ["c"],
                    "one supported oversized chunk runs alone")
        let cachedParts: [EpicChunkPart] = Array(repeating: EpicChunkPart(guid: "a", offset: 0, size: 1), count: 128)
        let cachedWindow = try EpicDownloader.prefetchWindow(parts: cachedParts, from: 0, chunks: budgetChunks,
                                                            isCached: { _ in true })
        try require(cachedWindow.end == 64 && cachedWindow.missing.isEmpty,
                    "a cached repeated prefix still stops after a bounded 64 file parts")
        let cancelledWindow: Task<Bool, Error> = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            do {
                _ = try EpicDownloader.prefetchWindow(parts: budgetParts, from: 0, chunks: [:], isCached: { _ in false })
                return false
            } catch is CancellationError { return true }
        }
        try require(try await cancelledWindow.value,
                    "an already-cancelled window throws before looking up any chunk")
        var aggregateMeter = EpicDownloader.AggregateTransferRateMeter()
        aggregateMeter.begin(now: 10); aggregateMeter.begin(now: 10)
        try require(aggregateMeter.receive(bytes: 100, now: 10.5) == nil, "parallel wire transfers share a common sample interval")
        try require(aggregateMeter.receive(bytes: 300, now: 11) == 400,
                    "parallel speed sums actual received bytes in the same wall-clock window")
        try require(aggregateMeter.end(now: 11.1) == nil && aggregateMeter.end(now: 11.2) == nil,
                    "per-chunk completion cannot inflate the aggregate network speed")
        aggregateMeter.begin(now: 20)
        try require(aggregateMeter.receive(bytes: 200, now: 20.5) == nil && aggregateMeter.end(now: 20.5) == 400,
                    "an idle interval resets the common network meter without counting cached bytes")
    }

    @MainActor private static func checkParallelInstall(fixtures: URL, external: URL, base: [URL]) async throws {
        let parallelData: Data = try Data(contentsOf: fixtures.appendingPathComponent("parallel.json"))
        let parallelSource = ParallelChunkSource(fixtures)
        let parallelRoot = external.appendingPathComponent("parallel-install")
        _ = try await EpicDownloader.install(manifestData: parallelData, baseURLs: [base[0]], destination: parallelRoot,
            progress: { _, _ in }, fetch: { try await parallelSource.fetch($0, $1) })
        let parallelStats = await parallelSource.stats()
        try require(parallelStats.peak == 4, "missing chunks overlap with a strict four-transfer cap")
        try require(parallelStats.seen.count == 9 && Set(parallelStats.seen).count == 9,
                    "repeated file parts fetch each distinct GUID only once")
        try require(try Data(contentsOf: parallelRoot.appendingPathComponent("Bin/Game.exe")) ==
                    Data(contentsOf: fixtures.appendingPathComponent("parallel-expected.bin")),
                    "out-of-order network completions assemble exactly ordered file bytes")
        let drainSource = ParallelChunkSource(fixtures)
        let drainRoot = external.appendingPathComponent("parallel-cancel-drain")
        let drainTask: Task<EpicInstallResult, Error> = Task {
            try await EpicDownloader.install(manifestData: parallelData, baseURLs: [base[0]], destination: drainRoot,
                progress: { _, _ in }, fetch: { try await drainSource.fetch($0, $1) })
        }
        for _ in 0..<400 {
            if await drainSource.stats().peak == 4 { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        let cancellationPeak = await drainSource.stats().peak
        drainTask.cancel()
        do { _ = try await drainTask.value; throw NSError(domain: "parallel cancellation ignored", code: 1) }
        catch is CancellationError {}
        let drained = await drainSource.stats()
        try require(cancellationPeak == 4, "cancellation exercises an actual full parallel window")
        try require(drained.active == 0 && drained.finished == drained.seen.count,
                    "install cancellation drains every noncooperative child before cleanup can start")
        try require(!FileManager.default.fileExists(atPath: drainRoot.appendingPathComponent("Bin/Game.exe").path),
                    "cancelled window commits no partially assembled file")
    }

    @MainActor private static func checkAggregateTransport(_ aggregateTransport: EpicCDNTransport,
                                                         rates: TransferRateObserver, base: URL, goodChunk: Data) async throws {
        FixtureProtocol.state.set(goodChunk, delay: 0.4)
        let aggregateStart = ProcessInfo.processInfo.systemUptime
        async let aggregateFirst: Data = aggregateTransport.fetch(base, maximum: goodChunk.count)
        async let aggregateSecond: Data = aggregateTransport.fetch(base, maximum: goodChunk.count)
        let aggregateBodies: (Data, Data) = try await (aggregateFirst, aggregateSecond)
        let aggregateElapsed: TimeInterval = ProcessInfo.processInfo.systemUptime - aggregateStart
        let actualAggregateRates: [Double] = rates.values()
        try require(aggregateBodies.0 == goodChunk && aggregateBodies.1 == goodChunk && actualAggregateRates.count == 1,
                    "overlapping real transport bodies emit one shared short-transfer rate")
        let expectedWireBytes: Double = Double(goodChunk.count) * 2
        let measuredWireBytes: Double = actualAggregateRates[0] * aggregateElapsed
        let byteTolerance: Double = Double(goodChunk.count) * 0.4
        try require(abs(measuredWireBytes - expectedWireBytes) < byteTolerance,
                    "actual delegate speed accounts for both wire bodies over one common elapsed interval")
    }

    @MainActor private static func checkStaleCDNHealth(fixtures: URL, external: URL) async throws {
        let manifestData: Data = try Data(contentsOf: fixtures.appendingPathComponent("stale-health.json"))
        let bases: [URL] = [URL(string: "https://slow.example.net/content")!, URL(string: "https://fast.example.net/content")!]
        let source = StaleHealthChunkSource(fixtures)
        let observer = CDNObserver()
        let destination: URL = external.appendingPathComponent("stale-cdn-health")
        _ = try await EpicDownloader.install(manifestData: manifestData, baseURLs: bases, destination: destination,
            progress: { _, _ in }, onCDNMeasurement: { observer.record($0) }, fetch: { try await source.fetch($0, $1) })
        let requests: [(String, String)] = await source.requests()
        try require(requests.contains { $0.0 == "slow.example.net" && $0.1.hasSuffix("66") },
                    "an early fast-origin failure actually waits for the slower fallback")
        let fastMeasurements: [EpicDownloader.CDNMeasurement] = observer.values().filter { $0.host == "fast.example.net" }
        try require(fastMeasurements.count == 2 && fastMeasurements.last?.failed == false,
                    "an older failure returned by a slow fallback cannot overwrite a newer healthy sibling transfer")
        let installed: Data = try Data(contentsOf: destination.appendingPathComponent("Bin/Game.exe"))
        let expected: Data = try Data(contentsOf: fixtures.appendingPathComponent("parallel-expected.bin"))
        try require(installed == Data(expected.prefix(48)), "stale-health failover still assembles every verified chunk")
    }

    static func main() async throws {
        let fixtures = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let external = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let apfs = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
        var meter = EpicDownloader.TransferRateMeter(now: 10)
        try require(meter.sample(bytes: 50, now: 10.5) == nil, "live transfer rate waits for a full sampling window")
        try require(meter.sample(bytes: 200, now: 12) == 100, "live rate counts wire bytes over monotonic elapsed time")
        try require(meter.sample(bytes: 500, now: 13) == 300, "next live rate uses only the current network window")
        try require(meter.sample(bytes: 501, now: 13.01, finished: true) == nil, "tiny final tail cannot replace the measured speed with a spike")
        var shortMeter = EpicDownloader.TransferRateMeter(now: 20)
        try require(shortMeter.sample(bytes: 400, now: 20.5, finished: true) == 800, "a short completed wire transfer still reports its measured rate")
        try require(shortMeter.sample(bytes: 0, now: 21) == nil && shortMeter.sample(bytes: 500, now: .nan) == nil,
                    "invalid counter or clock cannot create a live speed sample")
        let aliasFixture = URL(fileURLWithPath: CommandLine.arguments[4], isDirectory: true)
        try FileManager.default.createDirectory(at: apfs, withIntermediateDirectories: true)
        let base = [URL(string: "https://cdn.example.net/content")!, URL(string: "https://backup.example.net/content")!]
        let data = try Data(contentsOf: fixtures.appendingPathComponent("manifest.json"))
        let manifest = try EpicManifest.parse(data)
        try require(manifest.appName == "EpicNativeFixture", "real JSON metadata")
        try require(manifest.launchExecutable == "Bin/Game.exe", "Windows launch path normalization")
        try require(manifest.fileManifestList.count == 3 && manifest.totalBytes == 39, "JSON decimal blobs and parts")
        try require(manifest.chunks.values.allSatisfy { $0.path.hasPrefix("ChunksV3/") }, "JSON chunk directory")
        var checks = 10
        for name in ["manifest-18-1-0.bin", "manifest-18-0-2.bin", "manifest-22-1-1.bin", "manifest-24-0-2.bin"] {
            let binary = try EpicManifest.parse(Data(contentsOf: fixtures.appendingPathComponent(name)))
            try require(binary.totalBytes == 39 && binary.fileManifestList[1].filename == "Data/片段.bin", "binary compressed/raw UTF16 and versions")
            try require(binary.launchCommand == "--windowed", "binary metadata")
            if name.contains("22") || name.contains("24") {
                try require(binary.chunks.values.allSatisfy { $0.path.hasPrefix("ChunksV5/plain/") }, "plain V5 chunk URL")
            }
            checks += 1
        }
        let invalidCount = Int(try String(contentsOf: fixtures.appendingPathComponent("invalid-count.txt"), encoding: .utf8))!
        for i in 0..<invalidCount {
            do { _ = try EpicManifest.parse(Data(contentsOf: fixtures.appendingPathComponent("invalid-\(i).json"))); throw NSError(domain: "accepted invalid manifest \(i)", code: 1) }
            catch is EpicDownloadError { checks += 1 }
        }
        let binaryGood = try Data(contentsOf: fixtures.appendingPathComponent("manifest-18-1-0.bin"))
        for mutation in 0..<6 {
            var bad = binaryGood
            switch mutation {
            case 0: bad.removeLast()
            case 1: bad[16] ^= 1
            case 2: bad[36] = 2
            case 3: bad.replaceSubrange(8..<12, with: [255, 255, 255, 127])
            case 4: bad.replaceSubrange(4..<8, with: [1, 0, 0, 0])
            default: bad.append(0)
            }
            do { _ = try EpicManifest.parse(bad); throw NSError(domain: "accepted damaged binary", code: 1) }
            catch is EpicDownloadError { checks += 1 }
        }
        let guid = "01020304111213142122232431323334"
        let expected = manifest.chunks[guid]!
        for name in ["chunk-v1.bin", "chunk-v2.bin", "chunk-v3.bin", "chunk-v4.bin", "chunk-raw.bin"] {
            let bytes = try Data(contentsOf: fixtures.appendingPathComponent(name))
            let info = EpicChunkInfo(guid: expected.guid, rollingHash: expected.rollingHash, sha1: expected.sha1,
                                     group: expected.group, windowSize: expected.windowSize, fileSize: bytes.count, featureLevel: 18)
            let decoded = try EpicDownloader.decodeChunk(bytes, expected: info)
            try require(decoded.count == 1048576 && Data(Insecure.SHA1.hash(data: decoded)) == expected.sha1, "real chunk format \(name)")
            checks += 1
        }
        let goodChunk = try Data(contentsOf: fixtures.appendingPathComponent("chunk-v3.bin"))
        for position in [0, 16, 32, 40, 41, 61, 62, goodChunk.count - 1] {
            var bad = goodChunk; bad[position] ^= 4
            do { _ = try EpicDownloader.decodeChunk(bad, expected: expected); throw NSError(domain: "accepted corrupt chunk", code: 1) }
            catch is EpicDownloadError { checks += 1 }
        }
        try await checkPrefetchBudgetAndAggregateMeter()
        checks += 8
        try await checkParallelInstall(fixtures: fixtures, external: external, base: base)
        checks += 6
        try await checkStaleCDNHealth(fixtures: fixtures, external: external)
        checks += 3
        let ratedBases = ["slow", "fast", "bad"].map { URL(string: "https://\($0).example.net/content?token=fixture-secret")! }
        let ratedSource = RatedChunkSource(fixtures, corruptHost: "bad.example.net")
        let ratedObserver = CDNObserver()
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: ratedBases,
            destination: external.appendingPathComponent("rated-cdns"), progress: { _, _ in },
            onCDNMeasurement: { ratedObserver.record($0) },
            fetch: { try await ratedSource.fetch($0, $1) })
        let ratedRequests = await ratedSource.requests()
        try require(ratedRequests.filter { $0.1 == guid }.map { $0.0 } == ratedBases.map { $0.host! },
                    "all supplied CDN candidates benchmark the first missing chunk once")
        try require(ratedRequests.filter { $0.1 != guid }.map { $0.0 } == ["fast.example.net"],
                    "subsequent chunk uses fastest verified CDN and corrupt speed is excluded")
        let ratedMeasurements = ratedObserver.values()
        try require(ratedMeasurements.count == 4 && ratedMeasurements.allSatisfy {
            $0.scheme == "https" && ($0.failed ? $0.bytesPerSecond == nil : ($0.bytesPerSecond ?? 0) > 0)
        }, "telemetry reports only verified positive throughput and excludes corrupt speed")
        let telemetryJSON = String(decoding: try JSONEncoder().encode(ratedMeasurements), as: UTF8.self)
        try require(!telemetryJSON.contains("fixture-secret") && !telemetryJSON.contains("content"),
                    "CDN telemetry contains no signed URL path or query")
        checks += 4
        let corruptFastSource = RatedChunkSource(fixtures, corruptHost: "fast.example.net")
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: Array(ratedBases.prefix(2)),
            destination: external.appendingPathComponent("corrupt-fast-cdn"), progress: { _, _ in },
            fetch: { try await corruptFastSource.fetch($0, $1) })
        let corruptFastRequests = await corruptFastSource.requests()
        try require(corruptFastRequests.filter { $0.1 == guid }.count == 2 &&
                    corruptFastRequests.filter { $0.1 != guid }.map { $0.0 } == ["slow.example.net"],
                    "fast corrupt candidate cannot outrank a verified slower candidate")
        checks += 1
        let unsupportedSource = RatedChunkSource(fixtures, unsupportedHost: "slow.example.net")
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: Array(ratedBases.prefix(2)),
            destination: external.appendingPathComponent("unsupported-cdn"), progress: { _, _ in },
            fetch: { try await unsupportedSource.fetch($0, $1) })
        try require(await unsupportedSource.requests().count == 3,
                    "unsupported first CDN response does not prevent a valid other candidate")
        checks += 1
        let fallbackSource = RatedChunkSource(fixtures, corruptSecondHost: "fast.example.net")
        let fallbackObserver = CDNObserver()
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: Array(ratedBases.prefix(2)),
            destination: external.appendingPathComponent("rated-cdn-failover"), progress: { _, _ in },
            onCDNMeasurement: { fallbackObserver.record($0) }, fetch: { try await fallbackSource.fetch($0, $1) })
        try require(await fallbackSource.requests().map { $0.0 } ==
            ["slow.example.net", "fast.example.net", "fast.example.net", "slow.example.net"],
            "real transfer falls back to retained origin after fastest CDN becomes corrupt")
        let fallbackMeasurements = fallbackObserver.values()
        try require(fallbackMeasurements[2].failed && fallbackMeasurements[2].bytesPerSecond == nil &&
                    !fallbackMeasurements[3].failed && (fallbackMeasurements[3].bytesPerSecond ?? 0) > 0,
                    "failure demotes measured winner and successful real fallback updates verified rate")
        checks += 2
        let timedOutProbes = TimedOutProbeSource()
        let ordinaryRetry = ChunkSource(fixtures)
        let timeoutObserver = CDNObserver()
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: ratedBases,
            destination: external.appendingPathComponent("probe-timeout-normal-retry"), progress: { _, _ in },
            onCDNMeasurement: { timeoutObserver.record($0) },
            probeFetch: { try await timedOutProbes.fetch($0, $1) }, fetch: { try await ordinaryRetry.fetch($0, $1) })
        let timeoutRequests = await timedOutProbes.requests()
        try require(timeoutRequests.map { $0.host! } == ratedBases.map { $0.host! } &&
                    timeoutRequests.allSatisfy { $0.lastPathComponent.contains(guid) },
                    "all origins receive bounded probes only for the first missing chunk")
        try require(await ordinaryRetry.count() == 2,
                    "all probe timeouts fall back to ordinary transfer then continue without reprobing")
        let timeoutMeasurements = timeoutObserver.values()
        try require(timeoutMeasurements.count == 5 && timeoutMeasurements.prefix(3).allSatisfy {
            $0.failed && $0.bytesPerSecond == nil
        } && timeoutMeasurements.suffix(2).allSatisfy { !$0.failed && ($0.bytesPerSecond ?? 0) > 0 },
                    "timed-out probes never receive a rate; ordinary fallback can succeed")
        try require(timeoutObserver.values().filter(\.failed).allSatisfy { $0.failure == .network(URLError.timedOut.rawValue) },
                    "probe failure telemetry retains only the typed network code, never signed URLs")
        checks += 4
        let source = ChunkSource(fixtures)
        let installed = external.appendingPathComponent("installed")
        let result = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: installed,
             progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
        try require(result.executable == "Bin/Game.exe" && result.arguments == "--windowed" && result.totalBytes == 39, "complete result")
        try require(try Data(contentsOf: installed.appendingPathComponent("Bin/Game.exe")) == Data("MZnative fixture".utf8), "exact EXE file")
        try require(try Data(contentsOf: installed.appendingPathComponent("empty.txt")).isEmpty, "empty file")
        try require(await source.count() == 3, "first missing chunk probes both CDNs then shared chunks reuse cache")
        try require(FileManager.default.fileExists(atPath: installed.appendingPathComponent(".madeira-epic/complete").path), "completion only after validation")
        try require(try FileManager.default.contentsOfDirectory(atPath: installed.appendingPathComponent(".madeira-epic/chunks").path).isEmpty, "completed cache reclaimed")
        checks += 6
        let resumedSource = ChunkSource(fixtures)
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: installed,
             progress: { _, _ in }, fetch: { try await resumedSource.fetch($0, $1) })
        try require(await resumedSource.count() == 0, "completed files hash-verified and resumed without fetch")
        checks += 1
        let paused = external.appendingPathComponent("paused")
        let pauseSource = ChunkSource(fixtures, cancelledName: "A1A2A3A4")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: paused,
                progress: { _, _ in }, fetch: { try await pauseSource.fetch($0, $1) })
            throw NSError(domain: "cancelled fetch installed", code: 1)
        } catch is CancellationError {}
        try require(FileManager.default.fileExists(atPath: paused.appendingPathComponent("Bin/Game.exe").path), "completed first file retained")
        try require(!FileManager.default.fileExists(atPath: paused.appendingPathComponent(".madeira-epic/complete").path), "partial install not complete")
        let continueSource = ChunkSource(fixtures)
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: paused,
            progress: { _, _ in }, fetch: { try await continueSource.fetch($0, $1) })
        try require(await continueSource.count() == 2, "verified chunk/file survive pause; only missing chunk probes both CDNs")
        checks += 3
        let damagedCacheRoot = external.appendingPathComponent("damaged-cache")
        let cachePause = ChunkSource(fixtures, cancelledName: "A1A2A3A4")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: damagedCacheRoot,
                progress: { _, _ in }, fetch: { try await cachePause.fetch($0, $1) })
        } catch is CancellationError {}
        try FileManager.default.removeItem(at: damagedCacheRoot.appendingPathComponent("Bin/Game.exe"))
        try Data("damaged-cache".utf8).write(to: damagedCacheRoot.appendingPathComponent(".madeira-epic/chunks/\(guid)"))
        let cacheResume = ChunkSource(fixtures)
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: damagedCacheRoot,
            progress: { _, _ in }, fetch: { try await cacheResume.fetch($0, $1) })
        try require(await cacheResume.count() == 3, "damaged cached chunk discarded, benchmarked and fetched again")
        checks += 1
        let manyData = try Data(contentsOf: fixtures.appendingPathComponent("many-small-files.json"))
        let manyManifest = try EpicManifest.parse(manyData)
        let manyRoot = external.appendingPathComponent("many-small-files")
        let manyObserver = CheckpointObserver(manyRoot)
        let manySource = ChunkSource(fixtures)
        _ = try await EpicDownloader.install(manifestData: manyData, baseURLs: base, destination: manyRoot,
            progress: { _, _ in manyObserver.observe() }, fetch: { try await manySource.fetch($0, $1) })
        try require(manyObserver.replacements() <= 12, "small-file install rewrites checkpoint too often: \(manyObserver.replacements())")
        try require(manyManifest.fileManifestList.allSatisfy { FileManager.default.fileExists(atPath: manyRoot.appendingPathComponent($0.filename).path) }, "all 96 small files installed")
        checks += 2
        let manyPaused = external.appendingPathComponent("many-small-files-paused")
        let pausedObserver = CheckpointObserver(manyPaused)
        let manyPauseSource = ChunkSource(fixtures)
        let manyPauseTask = Task {
            try await EpicDownloader.install(manifestData: manyData, baseURLs: base, destination: manyPaused,
                progress: { done, _ in
                    pausedObserver.observe()
                    if done >= 16 * 37 { withUnsafeCurrentTask { $0?.cancel() } }
                }, fetch: { try await manyPauseSource.fetch($0, $1) })
        }
        do { _ = try await manyPauseTask.value; throw NSError(domain: "small-file cancellation ignored", code: 1) }
        catch is CancellationError {}
        try require(pausedObserver.replacements() <= 12, "cancelled small-file install rewrites checkpoint too often")
        let retained = manyManifest.fileManifestList.filter { FileManager.default.fileExists(atPath: manyPaused.appendingPathComponent($0.filename).path) }.count
        try require(retained == 36, "cancel keeps exactly the 36 committed files")
        try require(!FileManager.default.fileExists(atPath: manyPaused.appendingPathComponent(".madeira-epic/complete").path), "cancelled small-file install not marked ready")
        // A legacy checkpoint can claim files that do not exist. Production
        // resume must inspect/hash actual files rather than trusting its list.
        let legacy: [String: Any] = ["version": 1, "manifestHash": EpicInstallStore.digest(manyData),
            "appName": manyManifest.appName, "completed": manyManifest.fileManifestList.map(\.filename)]
        try JSONSerialization.data(withJSONObject: legacy).write(to: manyPaused.appendingPathComponent(".madeira-epic/checkpoint.json"))
        let manyResumeSource = ChunkSource(fixtures)
        let manyResumed = try await EpicDownloader.install(manifestData: manyData, baseURLs: base, destination: manyPaused,
            progress: { _, _ in }, fetch: { try await manyResumeSource.fetch($0, $1) })
        let resumedSmallFetches = await manyResumeSource.count()
        try require(manyResumed.totalBytes == 96 * 16 && resumedSmallFetches == 0, "small-file resume reuses verified files/chunks despite inaccurate old journal")
        try require(manyManifest.fileManifestList.allSatisfy { FileManager.default.fileExists(atPath: manyPaused.appendingPathComponent($0.filename).path) }, "resume reconstructs all missing small files")
        checks += 5
        let corruptRoot = external.appendingPathComponent("corrupt")
        let corruptSource = ChunkSource(fixtures, corrupt: true)
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: corruptRoot,
                progress: { _, _ in }, fetch: { try await corruptSource.fetch($0, $1) })
            throw NSError(domain: "corrupt chunk installed", code: 1)
        } catch EpicDownloadError.hashMismatch {}
        try require(await corruptSource.count() == 4, "bounded CDN failover attempts")
        try require(!FileManager.default.fileExists(atPath: corruptRoot.appendingPathComponent("Bin/Game.exe").path), "bad chunk never committed")
        let metadataFiles = try FileManager.default.contentsOfDirectory(atPath: corruptRoot.appendingPathComponent(".madeira-epic").path)
        try require(!metadataFiles.contains { $0.hasPrefix("partial-") }, "owned partial cleaned on failure")
        checks += 3
        let fileBad = try Data(contentsOf: fixtures.appendingPathComponent("bad-file-hash.json"))
        do {
            _ = try await EpicDownloader.install(manifestData: fileBad, baseURLs: base, destination: external.appendingPathComponent("file-bad"),
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "bad final hash installed", code: 1)
        } catch EpicDownloadError.hashMismatch { checks += 1 }
        let changed = try Data(contentsOf: fixtures.appendingPathComponent("changed.json"))
        do {
            _ = try await EpicDownloader.install(manifestData: changed, baseURLs: base, destination: installed,
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "manifest isolation missing", code: 1)
        } catch EpicDownloadError.differentInstall { checks += 1 }
        try Data("user-changed".utf8).write(to: installed.appendingPathComponent("Bin/Game.exe"))
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: installed,
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "user file overwritten", code: 1)
        } catch EpicDownloadError.existingFile {}
        try require(try Data(contentsOf: installed.appendingPathComponent("Bin/Game.exe")) == Data("user-changed".utf8), "modified game file preserved")
        checks += 1
        let unrelated = external.appendingPathComponent("unrelated")
        try FileManager.default.createDirectory(at: unrelated, withIntermediateDirectories: true)
        try Data("existing user game".utf8).write(to: unrelated.appendingPathComponent("user.txt"))
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: unrelated,
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "adopted unrelated folder", code: 1)
        } catch EpicDownloadError.unsafeDestination { checks += 1 }
        let active = external.appendingPathComponent("cancel-task")
        let slow = ChunkSource(fixtures, slow: true)
        let task = Task {
            try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: active,
                progress: { _, _ in }, fetch: { try await slow.fetch($0, $1) })
        }
        while await slow.count() == 0 { try await Task.sleep(for: .milliseconds(5)) }
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: active,
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "concurrent installation allowed", code: 1)
        } catch EpicDownloadError.unsafeDestination { checks += 1 }
        task.cancel()
        do { _ = try await task.value; throw NSError(domain: "cancelled task completed", code: 1) }
        catch is CancellationError { checks += 1 }
        try require(!FileManager.default.fileExists(atPath: active.appendingPathComponent(".madeira-epic/complete").path), "task cancellation has no completion")
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: active,
             progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
        checks += 1

        let ignoresRoot = external.appendingPathComponent("noncooperative-cancel")
        let ignoresSource = ChunkSource(fixtures, ignoresCancellation: true)
        let ignoresObserver = CDNObserver()
        let ignoresTask = Task {
            try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: ignoresRoot,
                progress: { _, _ in }, onCDNMeasurement: { ignoresObserver.record($0) },
                fetch: { try await ignoresSource.fetch($0, $1) })
        }
        while await ignoresSource.count() == 0 { try await Task.sleep(for: .milliseconds(5)) }
        ignoresTask.cancel()
        do { _ = try await ignoresTask.value; throw NSError(domain: "ignored cancellation committed files", code: 1) }
        catch is CancellationError { checks += 1 }
        try require(!FileManager.default.fileExists(atPath: ignoresRoot.appendingPathComponent("Bin/Game.exe").path), "cancel check after noncooperative network await")
        try require(await ignoresSource.count() == 1 && ignoresObserver.values().isEmpty,
                    "cancelled probe makes no later requests and reports no unverified measurement")
        try require(try FileManager.default.contentsOfDirectory(atPath: ignoresRoot.appendingPathComponent(".madeira-epic/chunks").path).isEmpty,
                    "cancelled probe writes no cache")
        checks += 3
        // APFS is required for actual symlink/hardlink fixtures; no copies of
        // the game or IPA are made on the internal disk.
        let outside = apfs.appendingPathComponent("outside")
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        try Data("untouched".utf8).write(to: outside.appendingPathComponent("Game.exe"))
        for name in ["direct-parent", "missing-intermediate", "existing-install"] {
            let target = apfs.appendingPathComponent("protected-target-" + name)
            let alias = apfs.appendingPathComponent("ancestor-link-" + name)
            try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
            try Data("Steam fixture preserved".utf8).write(to: target.appendingPathComponent("steam-user.txt"))
            var forbidden = alias.appendingPathComponent("account-game")
            if name == "missing-intermediate" { forbidden = alias.appendingPathComponent("missing-parent/account-game") }
            if name == "existing-install" {
                let cancelled = ChunkSource(fixtures, cancelledName: "A1A2A3A4")
                do {
                    _ = try await EpicDownloader.install(manifestData: data, baseURLs: base,
                        destination: target.appendingPathComponent("account-game"), progress: { _, _ in },
                        fetch: { try await cancelled.fetch($0, $1) })
                } catch is CancellationError {}
            }
            try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: target)
            let before = try FileManager.default.contentsOfDirectory(atPath: target.path).sorted()
            do {
                _ = try EpicInstallStore(destination: forbidden, manifestData: data, appName: manifest.appName)
                throw NSError(domain: "user ancestor symlink accepted: " + name, code: 1)
            } catch EpicDownloadError.unsafeDestination {}
            try require(try FileManager.default.contentsOfDirectory(atPath: target.path).sorted() == before,
                        "ancestor link target untouched: " + name)
            checks += 1
        }
        let systemAlias = URL(fileURLWithPath: aliasFixture.path.replacingOccurrences(of: "/private/var/", with: "/var/"), isDirectory: true)
            .appendingPathComponent("trusted-var-alias")
        let aliasStore = try EpicInstallStore(destination: systemAlias, manifestData: data,
            appName: manifest.appName, trustedContainer: aliasFixture)
        withExtendedLifetime(aliasStore) {}
        try require(systemAlias.path.hasPrefix("/var/") &&
            FileManager.default.fileExists(atPath: aliasFixture.appendingPathComponent("trusted-var-alias/.madeira-epic/checkpoint.json").path),
            "protected /var system alias remains supported by the real install store")
        checks += 1
        let missingParents = external.appendingPathComponent("new-parent/new-child/account-game")
        _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: missingParents,
            progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
        try require(FileManager.default.fileExists(atPath: missingParents.appendingPathComponent("Bin/Game.exe").path),
                    "missing safe ancestors created through directory descriptors")
        checks += 1
        let linked = apfs.appendingPathComponent("linked")
        try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: outside)
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: linked,
                progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
            throw NSError(domain: "root symlink followed", code: 1)
        } catch EpicDownloadError.unsafeDestination { checks += 1 }
        for name in ["symlink-dir", "symlink-cache", "hardlink-file", "symlink-file", "symlink-metadata"] {
            let folder = apfs.appendingPathComponent(name)
            if name == "symlink-metadata" {
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                try FileManager.default.createSymbolicLink(at: folder.appendingPathComponent(".madeira-epic"), withDestinationURL: outside)
            } else {
                let cancellation = ChunkSource(fixtures, cancelledName: "A1A2A3A4")
                do {
                    _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: folder,
                        progress: { _, _ in }, fetch: { try await cancellation.fetch($0, $1) })
                } catch is CancellationError {}
                if name == "symlink-dir" {
                    try FileManager.default.createSymbolicLink(at: folder.appendingPathComponent("Data"), withDestinationURL: outside)
                } else if name == "symlink-cache" {
                    let cache = folder.appendingPathComponent(".madeira-epic/chunks/\(guid)")
                    try FileManager.default.removeItem(at: cache)
                    try FileManager.default.createSymbolicLink(at: cache, withDestinationURL: outside.appendingPathComponent("Game.exe"))
                    // Force the first file to need its cached chunk again.
                    try FileManager.default.removeItem(at: folder.appendingPathComponent("Bin/Game.exe"))
                } else {
                    let gameFile = folder.appendingPathComponent("Bin/Game.exe")
                    try FileManager.default.removeItem(at: gameFile)
                    if name == "hardlink-file" { try FileManager.default.linkItem(at: outside.appendingPathComponent("Game.exe"), to: gameFile) }
                    else { try FileManager.default.createSymbolicLink(at: gameFile, withDestinationURL: outside.appendingPathComponent("Game.exe")) }
                }
            }
            do {
                _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: folder,
                    progress: { _, _ in }, fetch: { try await source.fetch($0, $1) })
                throw NSError(domain: "unsafe link accepted \(name)", code: 1)
            } catch EpicDownloadError.unsafeDestination { checks += 1 }
        }
        try require(try Data(contentsOf: outside.appendingPathComponent("Game.exe")) == Data("untouched".utf8), "link target untouched")
        checks += 1
        let race = apfs.appendingPathComponent("race")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: race,
                progress: { _, _ in }, fetch: { url, maximum in
                    let bin = race.appendingPathComponent("Bin")
                    try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
                    let racedFile = bin.appendingPathComponent("Game.exe")
                    if !FileManager.default.fileExists(atPath: racedFile.path) {
                        try Data("race-preserved".utf8).write(to: racedFile)
                    }
                    return try await source.fetch(url, maximum)
                })
            throw NSError(domain: "racing user file overwritten", code: 1)
        } catch EpicDownloadError.existingFile {}
        try require(try Data(contentsOf: race.appendingPathComponent("Bin/Game.exe")) == Data("race-preserved".utf8), "exclusive atomic commit preserves racing file")
        checks += 1
        let replacedRoot = apfs.appendingPathComponent("replaced-root")
        let movedRoot = apfs.appendingPathComponent("moved-root")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: replacedRoot,
                progress: { _, _ in }, fetch: { url, maximum in
                    if !FileManager.default.fileExists(atPath: movedRoot.path) {
                        try FileManager.default.moveItem(at: replacedRoot, to: movedRoot)
                        try FileManager.default.createDirectory(at: replacedRoot, withIntermediateDirectories: true)
                        try Data("new user folder".utf8).write(to: replacedRoot.appendingPathComponent("user.txt"))
                    }
                    return try await source.fetch(url, maximum)
                })
            throw NSError(domain: "replaced root marked ready", code: 1)
        } catch EpicDownloadError.unsafeDestination {}
        try require(try FileManager.default.contentsOfDirectory(atPath: replacedRoot.path) == ["user.txt"], "directory descriptor stayed in original root and new folder preserved")
        checks += 1
        let partialRace = apfs.appendingPathComponent("partial-race")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: partialRace,
                progress: { _, _ in }, fetch: { url, maximum in
                    let metadata = partialRace.appendingPathComponent(".madeira-epic")
                    for name in try FileManager.default.contentsOfDirectory(atPath: metadata.path) where name.hasPrefix("partial-") {
                        let partial = metadata.appendingPathComponent(name)
                        try FileManager.default.removeItem(at: partial)
                        try FileManager.default.createSymbolicLink(at: partial, withDestinationURL: outside.appendingPathComponent("Game.exe"))
                    }
                    return try await source.fetch(url, maximum)
                })
            throw NSError(domain: "replaced partial marked ready", code: 1)
        } catch EpicDownloadError.unsafeDestination {}
        try require(!FileManager.default.fileExists(atPath: partialRace.appendingPathComponent("Bin/Game.exe").path), "partial substitution rejected before commit")
        checks += 1
        let finalRace = apfs.appendingPathComponent("final-race")
        do {
            _ = try await EpicDownloader.install(manifestData: data, baseURLs: base, destination: finalRace,
                progress: { _, _ in }, fetch: { url, maximum in
                    if url.lastPathComponent.contains("A1A2A3A4") {
                        try Data("user changed during install".utf8).write(to: finalRace.appendingPathComponent("Bin/Game.exe"))
                    }
                    return try await source.fetch(url, maximum)
                })
            throw NSError(domain: "changed final file marked ready", code: 1)
        } catch EpicDownloadError.existingFile {}
        try require(!FileManager.default.fileExists(atPath: finalRace.appendingPathComponent(".madeira-epic/complete").path), "final paths hash verified before ready")
        checks += 1
        let whitespaceManifest = try EpicManifest.parse(Data([9, 13, 32]) + data)
        try require(whitespaceManifest.totalBytes == 39, "JSON leading whitespace")
        checks += 1
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FixtureProtocol.self]
        config.httpAdditionalHeaders = ["Authorization": "Bearer test-secret", "Cookie": "test-secret"]
        let wireRates = TransferRateObserver()
        let transport = try EpicCDNTransport(baseURLs: base, configuration: config,
            onTransferRate: { wireRates.record($0) })
        defer { transport.finish() }
        try require(config.timeoutIntervalForRequest == 30 && config.timeoutIntervalForResource == 180 &&
                    config.httpMaximumConnectionsPerHost == 4, "ordinary CDN transport permits bounded four-chunk concurrency")
        let probeConfig = URLSessionConfiguration.ephemeral
        let probeTransport = try EpicCDNTransport(baseURLs: base, configuration: probeConfig, probing: true)
        defer { probeTransport.finish() }
        try require(probeConfig.timeoutIntervalForRequest == 4 && probeConfig.timeoutIntervalForResource == 8 &&
                    probeConfig.httpMaximumConnectionsPerHost == 1, "probe CDN transport has short serial timeout budgets")
        checks += 2
        for text in ["http://cdn.example.net/content", "https://user:password@cdn.example.net/content", "https://cdn.example.net/content#fragment",
                     "https://localhost/content", "https://content.local/content", "https://content.internal/content",
                     "https://127.0.0.1/content", "https://192.168.1.2/content", "https://8.8.8.8/content",
                     "https://127.1/content", "https://2130706433/content", "https://0x7f.0.0.0x1/content",
                     "https://[::1]/content", "https://cdn.example.net:80/content", "https://cdn.example.net:444/content"] {
            try require(!EpicCDNTransport.safeURL(URL(string: text)!), "URL rejected \(text)")
            checks += 1
        }
        try require(!transport.allows(URL(string: "https://evil.example.net/x")!), "redirect origin rejected")
        try require(!transport.allows(URL(string: "https://cdn.example.net:444/x")!), "redirect port rejected")
        try require(transport.allows(URL(string: "https://CDN.example.net:443/x")!), "HTTPS origin normalized")
        try require(transport.allows(URL(string: "https://cdn.example.net.:443/x")!), "fully-qualified DNS origin normalized")
        checks += 4
        FixtureProtocol.state.set(goodChunk)
        let fetched = try await transport.fetch(base[0].appendingPathComponent(expected.path), maximum: goodChunk.count)
        try require(fetched == goodChunk, "actual URLSession streaming transport")
        let originalRequest = FixtureProtocol.state.last()
        try require(originalRequest != nil, "credential checks inspect the actual configured transport request")
        try require(originalRequest?.value(forHTTPHeaderField: "Authorization") == nil, "no bearer credentials to CDN")
        try require(originalRequest?.value(forHTTPHeaderField: "Cookie") == nil, "no cookie credentials to CDN")
        try require(wireRates.values().count == 1 && wireRates.values().allSatisfy { $0.isFinite && $0 > 0 },
                    "actual network body reports a finite live speed separate from installed file progress")
        _ = try await transport.fetch(base[0].appendingPathComponent(expected.path), maximum: goodChunk.count)
        try require(wireRates.values().count == 1, "rapid wire requests throttle live speed callbacks to one per second")
        let aggregateConfig = URLSessionConfiguration.ephemeral
        aggregateConfig.protocolClasses = [FixtureProtocol.self]
        let aggregateRates = TransferRateObserver()
        let aggregateTransport = try EpicCDNTransport(baseURLs: base, configuration: aggregateConfig,
            onTransferRate: { aggregateRates.record($0) })
        defer { aggregateTransport.finish() }
        try await checkAggregateTransport(aggregateTransport, rates: aggregateRates, base: base[0], goodChunk: goodChunk)
        checks += 2
        let refusedConfig = URLSessionConfiguration.ephemeral
        refusedConfig.protocolClasses = [FixtureProtocol.self]
        let refusedRates = TransferRateObserver()
        let refusedTransport = try EpicCDNTransport(baseURLs: base, configuration: refusedConfig,
            onTransferRate: { refusedRates.record($0) })
        defer { refusedTransport.finish() }
        FixtureProtocol.state.set(goodChunk, status: 403)
        do { _ = try await refusedTransport.fetch(base[0], maximum: goodChunk.count); throw NSError(domain: "HTTP403 produced content", code: 1) }
        catch EpicDownloader.CDNFailure.http(403) {}
        try require(refusedRates.values().isEmpty, "refused response bodies cannot appear as successful wire speed")
        let trickleConfig = URLSessionConfiguration.ephemeral
        trickleConfig.protocolClasses = [FixtureProtocol.self]
        let trickleRates = TransferRateObserver()
        let trickleTransport = try EpicCDNTransport(baseURLs: base, configuration: trickleConfig,
            onTransferRate: { trickleRates.record($0) })
        defer { trickleTransport.finish() }
        let trickleBody = Data(repeating: 42, count: 12)
        FixtureProtocol.state.set(trickleBody, trickle: true)
        let trickleContent = try await trickleTransport.fetch(base[0], maximum: trickleBody.count)
        try require(trickleContent == trickleBody && trickleRates.values().count >= 2,
                    "a slowly trickling body refreshes its rate before any 16KiB boundary or completion; rate-samples=\(trickleRates.values().count)")
        let cancelConfig = URLSessionConfiguration.ephemeral
        cancelConfig.protocolClasses = [FixtureProtocol.self]
        let cancelledRates = TransferRateObserver()
        let cancelTransport = try EpicCDNTransport(baseURLs: base, configuration: cancelConfig,
            onTransferRate: { cancelledRates.record($0) })
        defer { cancelTransport.finish() }
        FixtureProtocol.state.set(trickleBody, trickle: true)
        let cancellingTransfer = Task { try await cancelTransport.fetch(base[0], maximum: trickleBody.count) }
        try await Task.sleep(nanoseconds: 1_200_000_000)
        cancellingTransfer.cancel()
        do { _ = try await cancellingTransfer.value; throw NSError(domain: "cancelled wire transfer returned content", code: 1) }
        catch is CancellationError {}
        let rateCountAfterCancel = cancelledRates.values().count
        try await Task.sleep(nanoseconds: 1_150_000_000)
        try require(cancelledRates.values().count == rateCountAfterCancel,
                    "cancelling the real transport drains its continuation and stops later receive-rate events")
        let requestsBeforeCancellation = FixtureProtocol.state.requestCount()
        let alreadyCancelled = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await cancelTransport.fetch(base[0], maximum: trickleBody.count)
        }
        do { _ = try await alreadyCancelled.value; throw NSError(domain: "already cancelled transport returned content", code: 1) }
        catch is CancellationError {}
        try require(cancelledRates.values().count == rateCountAfterCancel &&
                    FixtureProtocol.state.requestCount() == requestsBeforeCancellation,
                    "cancellation before delegate start cannot resume a network task or report a new rate")
        FixtureProtocol.state.set(goodChunk)
        checks += 10
        var dirtyRedirect = URLRequest(url: base[1])
        dirtyRedirect.httpMethod = "POST"
        dirtyRedirect.httpBody = Data("test-secret".utf8)
        dirtyRedirect.setValue("Bearer test-secret", forHTTPHeaderField: "Authorization")
        dirtyRedirect.setValue("test-secret", forHTTPHeaderField: "Cookie")
        var cleanRedirect: URLRequest?
        transport.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: base[0]),
            willPerformHTTPRedirection: HTTPURLResponse(url: base[0], statusCode: 302, httpVersion: nil, headerFields: nil)!,
            newRequest: dirtyRedirect) { cleanRedirect = $0 }
        try require(cleanRedirect?.httpMethod == "GET" && cleanRedirect?.httpBody == nil &&
            cleanRedirect?.value(forHTTPHeaderField: "Authorization") == nil && cleanRedirect?.value(forHTTPHeaderField: "Cookie") == nil,
            "redirect request strips body and credentials")
        dirtyRedirect.url = URL(string: "https://evil.example.net/x")!
        cleanRedirect = dirtyRedirect
        transport.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: base[0]),
            willPerformHTTPRedirection: HTTPURLResponse(url: base[0], statusCode: 302, httpVersion: nil, headerFields: nil)!,
            newRequest: dirtyRedirect) { cleanRedirect = $0 }
        try require(cleanRedirect == nil, "actual redirect delegate rejects unlisted origin")
        checks += 2
        FixtureProtocol.state.set(data)
        let downloadedManifest = try await transport.fetch(base[0], maximum: EpicManifest.maximumManifestBytes, exactSize: false)
        try require(try EpicManifest.parse(downloadedManifest).appName == "EpicNativeFixture", "manifest transport permits bounded variable size")
        checks += 1
        FixtureProtocol.state.set(goodChunk, status: 403)
        do { _ = try await transport.fetch(base[0], maximum: goodChunk.count); throw NSError(domain: "403 accepted", code: 1) }
        catch EpicDownloader.CDNFailure.http(403) { checks += 1 }
        FixtureProtocol.state.set(goodChunk + Data([1]))
        do { _ = try await transport.fetch(base[0], maximum: goodChunk.count); throw NSError(domain: "excess bytes accepted", code: 1) }
        catch EpicDownloadError.contentServer { checks += 1 }
        FixtureProtocol.state.set(goodChunk + Data([1]), unknownLength: true)
        do { _ = try await transport.fetch(base[0], maximum: goodChunk.count); throw NSError(domain: "unknown-length stream exceeded bound", code: 1) }
        catch EpicDownloadError.invalidChunk { checks += 1 }
        FixtureProtocol.state.set(goodChunk, delay: 1)
        let networkTask = Task { try await transport.fetch(base[0], maximum: goodChunk.count) }
        while FixtureProtocol.state.last() == nil { try await Task.sleep(for: .milliseconds(5)) }
        networkTask.cancel()
        do { _ = try await networkTask.value; throw NSError(domain: "URLSession cancellation ignored", code: 1) }
        catch { try require((error as? URLError)?.code == .cancelled || error is CancellationError, "URLSession cancelled real stream"); checks += 1 }
        print("PASS: \(checks) native Epic BuildPatch, hashes, confinement, resume, cancellation and CDN checks")
        let existingChecks = checks
        // The manifest is valid in these cases: exercise the actual install
        // Store's URL boundary rather than relying on manifest safePath.
        let nullFixture = external.appendingPathComponent("direct-store-null", isDirectory: true)
        try FileManager.default.createDirectory(at: nullFixture, withIntermediateDirectories: true)
        let nulDestinations = [
            URL(fileURLWithPath: nullFixture.path + "/actual\0tail/account-game", isDirectory: true),
            URL(fileURLWithPath: nullFixture.path + "/actual-leaf\0tail", isDirectory: true),
            URL(string: nullFixture.appendingPathComponent("encoded").absoluteString + "%00tail/account-game")!,
            URL(string: nullFixture.appendingPathComponent("encoded-leaf").absoluteString + "%00tail")!
        ]
        for destination in nulDestinations {
            do {
                _ = try EpicInstallStore(destination: destination, manifestData: data, appName: manifest.appName)
                throw NSError(domain: "direct install Store accepted a NUL URL", code: 1)
            } catch EpicDownloadError.unsafeDestination { checks += 1 }
        }
        try require(try FileManager.default.contentsOfDirectory(atPath: nullFixture.path).isEmpty,
                    "NUL URLs rejected before creating any directory or metadata")
        checks += 1
        let literalRoot = nullFixture.appendingPathComponent("literal%00parent/literal%00game", isDirectory: true)
        let literalStore = try EpicInstallStore(destination: literalRoot, manifestData: data, appName: manifest.appName)
        withExtendedLifetime(literalStore) {}
        try require(FileManager.default.fileExists(atPath: literalRoot.appendingPathComponent(".madeira-epic/checkpoint.json").path),
                    "literal percent-zero-zero Store path is accepted without double decoding")
        checks += 1
        let percentData = try Data(contentsOf: fixtures.appendingPathComponent("manifest-percent.json"))
        let percentRoot = external.appendingPathComponent("percent%00parent/percent%00game", isDirectory: true)
        let percentSource = ChunkSource(fixtures)
        let percentResult = try await EpicDownloader.install(manifestData: percentData, baseURLs: base, destination: percentRoot,
            progress: { _, _ in }, fetch: { try await percentSource.fetch($0, $1) })
        try require(percentResult.executable == "Bin/Game%00tail.exe", "literal percent filename retained in install result")
        try require(try Data(contentsOf: percentRoot.appendingPathComponent("Bin/Game%00tail.exe")) == Data("MZnative fixture".utf8),
                    "literal percent filename installed with exact game bytes")
        try require(try Data(contentsOf: percentRoot.appendingPathComponent(".madeira-epic/manifest")) == percentData,
                    "literal percent filename retained in owned manifest metadata")
        let percentResume = ChunkSource(fixtures)
        _ = try await EpicDownloader.install(manifestData: percentData, baseURLs: base, destination: percentRoot,
            progress: { _, _ in }, fetch: { try await percentResume.fetch($0, $1) })
        try require(await percentResume.count() == 0, "literal percent file and manifest identity resume without fetch")
        checks += 4
        print("PASS: \(checks - existingChecks) direct Store NUL and literal percent URL boundary checks")

        // Cancellation owns only an incomplete root whose original builder
        // manifest and checkpoint agree. A public catalog asset may be an alias.
        func cleanupFixture(_ name: String) throws -> URL {
            let folder = external.appendingPathComponent(name)
            let store = try EpicInstallStore(destination: folder, manifestData: data, appName: manifest.appName)
            let (fd, partial) = try store.makePartial()
            defer { close(fd) }
            try EpicInstallStore.write(fd, data: Data("MZnative fixture".utf8))
            try store.commitPartial(partial, descriptor: fd, file: manifest.fileManifestList[0])
            try store.cacheChunk(manifest.chunks.values.first!, data: Data("cached partial bytes".utf8))
            let (pendingFD, _) = try store.makePartial()
            close(pendingFD)
            return folder
        }
        func rejectedCleanup(_ folder: URL, hash: String? = nil) throws {
            do {
                _ = try EpicInstallStore.inspectIncompleteDownload(destination: folder, cancellationID: UUID(), expectedManifestHash: hash)
                throw NSError(domain: "unsafe cancellation accepted", code: 1)
            } catch is EpicDownloadCleanupError {}
            try require(FileManager.default.fileExists(atPath: folder.appendingPathComponent(".madeira-epic/manifest").path), "rejected cleanup preserves ownership manifest")
            try require(FileManager.default.fileExists(atPath: folder.appendingPathComponent("Bin/Game.exe").path), "rejected cleanup makes zero preflight deletions")
        }
        let cancelRoot = try cleanupFixture("cancel-public-asset-alias")
        let cancelID = UUID()
        print("CHECK: cancellation paused ownership and complete cleanup"); fflush(nil)
        guard let cancelIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: cancelRoot, cancellationID: cancelID) else {
            throw NSError(domain: "owned paused cancellation identity missing", code: 1)
        }
        try require(cancelIdentity.manifestHash == EpicInstallStore.digest(data) && cancelIdentity.manifestAppName == manifest.appName,
                    "legacy cancellation identifies builder manifest without public-asset-name comparison")
        try EpicInstallStore.removeIncompleteDownload(destination: cancelRoot, cancellationID: cancelID, identity: cancelIdentity)
        try require(!FileManager.default.fileExists(atPath: cancelRoot.path), "Cancel removes committed incomplete files, partials and private cache")
        try require(FileManager.default.fileExists(atPath: installed.path), "Cancel preserves completed sibling install")
        try require(try EpicInstallStore.inspectIncompleteDownload(destination: cancelRoot, cancellationID: cancelID) == nil, "missing cleanup is idempotent")
        try EpicInstallStore.removeIncompleteDownload(destination: cancelRoot, cancellationID: cancelID, identity: cancelIdentity)

        let wrongHash = try cleanupFixture("cancel-wrong-hash")
        print("CHECK: cancellation rejects unsafe ownership before deletion"); fflush(nil)
        try rejectedCleanup(wrongHash, hash: String(repeating: "0", count: 64))
        let unknown = try cleanupFixture("cancel-unknown")
        try Data("user save".utf8).write(to: unknown.appendingPathComponent("save.dat"))
        try rejectedCleanup(unknown)
        let modified = try cleanupFixture("cancel-modified")
        try Data("user-modified game".utf8).write(to: modified.appendingPathComponent("Bin/Game.exe"))
        try rejectedCleanup(modified)
        let completedCancel = try cleanupFixture("cancel-complete")
        try FileManager.default.createDirectory(at: completedCancel.appendingPathComponent(".madeira-epic/complete"), withIntermediateDirectories: false)
        do {
            _ = try EpicInstallStore.inspectIncompleteDownload(destination: completedCancel, cancellationID: UUID())
            throw NSError(domain: "completed cancellation accepted", code: 1)
        } catch EpicDownloadCleanupError.completed {}
        try require(FileManager.default.fileExists(atPath: completedCancel.appendingPathComponent("Bin/Game.exe").path), "any complete entry preserves install")
        let heldRoot = external.appendingPathComponent("cancel-active-lock")
        print("CHECK: cancellation active lock waits for writer drain"); fflush(nil)
        var heldStore: EpicInstallStore? = try EpicInstallStore(destination: heldRoot, manifestData: data, appName: manifest.appName)
        withExtendedLifetime(heldStore) {
            do {
                _ = try EpicInstallStore.inspectIncompleteDownload(destination: heldRoot, cancellationID: UUID())
                throw NSError(domain: "active cancellation accepted", code: 1)
            } catch is EpicDownloadCleanupError {} catch { fatalError("active cleanup unexpected error") }
        }
        heldStore = nil
        let drainedID = UUID()
        let drainedIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: heldRoot, cancellationID: drainedID)!
        try EpicInstallStore.removeIncompleteDownload(destination: heldRoot, cancellationID: drainedID, identity: drainedIdentity)

        let cancelOutside = apfs.appendingPathComponent("cancel-cancelOutside-sentinel")
        print("CHECK: cancellation protects links and replaced roots"); fflush(nil)
        try Data("cancelOutside sentinel".utf8).write(to: cancelOutside)
        let hardlinked = try cleanupFixture("cancel-hardlink")
        try FileManager.default.linkItem(at: cancelOutside, to: hardlinked.appendingPathComponent(".madeira-epic/partial-" + UUID().uuidString))
        try rejectedCleanup(hardlinked)
        let symlinked = try cleanupFixture("cancel-symlink")
        try FileManager.default.createSymbolicLink(at: symlinked.appendingPathComponent(".madeira-epic/write-" + UUID().uuidString), withDestinationURL: cancelOutside)
        try rejectedCleanup(symlinked)
        let replaced = try cleanupFixture("cancel-replaced")
        let replaceID = UUID()
        let replaceIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: replaced, cancellationID: replaceID)!
        let original = external.appendingPathComponent("cancel-original")
        try FileManager.default.moveItem(at: replaced, to: original)
        try FileManager.default.createDirectory(at: replaced, withIntermediateDirectories: false)
        try Data("replacement user data".utf8).write(to: replaced.appendingPathComponent("user.txt"))
        do {
            try EpicInstallStore.removeIncompleteDownload(destination: replaced, cancellationID: replaceID, identity: replaceIdentity)
            throw NSError(domain: "replacement root cancellation accepted", code: 1)
        } catch is EpicDownloadCleanupError {}
        try require(try Data(contentsOf: replaced.appendingPathComponent("user.txt")) == Data("replacement user data".utf8), "persisted identity never deletes replacement root")
        try require(try Data(contentsOf: cancelOutside) == Data("cancelOutside sentinel".utf8), "cleanup links preserve cancelOutside sentinel")

        // Reproduce a process dying after quarantine and one verified file was
        // removed. Retry uses persisted identity and ignores a recreated source.
        let retryRoot = try cleanupFixture("cancel-retry")
        print("CHECK: cancellation resumes persisted quarantine after interruption"); fflush(nil)
        let retryID = UUID()
        let retryIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: retryRoot, cancellationID: retryID)!
        let quarantine = external.appendingPathComponent(".madeira-cancel-" + EpicInstallStore.digest(Data(retryRoot.lastPathComponent.utf8)) + "-" + retryID.uuidString)
        try FileManager.default.moveItem(at: retryRoot, to: quarantine)
        try FileManager.default.removeItem(at: quarantine.appendingPathComponent("Bin/Game.exe"))
        try FileManager.default.createDirectory(at: retryRoot, withIntermediateDirectories: false)
        try Data("new root".utf8).write(to: retryRoot.appendingPathComponent("new.txt"))
        try EpicInstallStore.removeIncompleteDownload(destination: retryRoot, cancellationID: retryID, identity: retryIdentity)
        try require(!FileManager.default.fileExists(atPath: quarantine.path), "retry removes only original quarantine with partially removed files")
        try require(try Data(contentsOf: retryRoot.appendingPathComponent("new.txt")) == Data("new root".utf8), "retry preserves recreated source")
        print("CHECK: cancellation recovers renamed leaves and metadata retirement"); fflush(nil)
        for phase in ["renamed-leaves", "retired-manifest", "retired-legacy-checkpoint", "final-lock"] {
            let phaseRoot = try cleanupFixture("cancel-" + phase)
            if phase == "retired-legacy-checkpoint" {
                let legacyCheckpoint: [String: Any] = ["version": 1, "manifestHash": EpicInstallStore.digest(data),
                    "appName": manifest.appName, "completed": ["Bin/Game.exe"]]
                try JSONSerialization.data(withJSONObject: legacyCheckpoint).write(to: phaseRoot.appendingPathComponent(".madeira-epic/checkpoint.json"))
            }
            let phaseID = UUID()
            let phaseIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: phaseRoot, cancellationID: phaseID)!
            let phaseQuarantine = external.appendingPathComponent(".madeira-cancel-" + EpicInstallStore.digest(Data(phaseRoot.lastPathComponent.utf8)) + "-" + phaseID.uuidString)
            func leafAlias(_ name: String) -> String {
                return ".madeira-clean-" + EpicInstallStore.digest(Data(name.utf8)) + "-" + phaseID.uuidString
            }
            try FileManager.default.moveItem(at: phaseRoot, to: phaseQuarantine)
            let phaseMeta = phaseQuarantine.appendingPathComponent(".madeira-epic")
            if phase == "renamed-leaves" {
                let bin = phaseQuarantine.appendingPathComponent("Bin")
                try FileManager.default.moveItem(at: bin.appendingPathComponent("Game.exe"), to: bin.appendingPathComponent(leafAlias("Game.exe")))
                try FileManager.default.moveItem(at: bin, to: phaseQuarantine.appendingPathComponent(leafAlias("Bin")))
                for name in try FileManager.default.contentsOfDirectory(atPath: phaseMeta.path) where name.hasPrefix("partial-") {
                    try FileManager.default.moveItem(at: phaseMeta.appendingPathComponent(name), to: phaseMeta.appendingPathComponent(".madeira-clean-" + name))
                }
                try FileManager.default.moveItem(at: phaseMeta, to: phaseQuarantine.appendingPathComponent(leafAlias(".madeira-epic")))
            } else {
                try FileManager.default.removeItem(at: phaseQuarantine.appendingPathComponent("Bin"))
                try FileManager.default.removeItem(at: phaseMeta.appendingPathComponent("chunks"))
                for name in try FileManager.default.contentsOfDirectory(atPath: phaseMeta.path) where name.hasPrefix("partial-") {
                    try FileManager.default.removeItem(at: phaseMeta.appendingPathComponent(name))
                }
                try FileManager.default.removeItem(at: phaseMeta.appendingPathComponent("manifest"))
                if phase == "retired-manifest" || phase == "retired-legacy-checkpoint" {
                    try JSONEncoder().encode(phaseIdentity).write(to: phaseMeta.appendingPathComponent("cancel-receipt-" + phaseID.uuidString))
                } else {
                    try FileManager.default.removeItem(at: phaseMeta.appendingPathComponent("checkpoint.json"))
                }
            }
            try EpicInstallStore.removeIncompleteDownload(destination: phaseRoot, cancellationID: phaseID, identity: phaseIdentity)
            try require(!FileManager.default.fileExists(atPath: phaseQuarantine.path), "interrupted " + phase + " cleanup resumes safely")
        }
        let missingParentRoot = external.appendingPathComponent("cancel-missing-parent/game")
        try require(try EpicInstallStore.inspectIncompleteDownload(destination: missingParentRoot, cancellationID: UUID()) == nil,
                    "missing ancestors are idempotent and never created by inspection")
        try require(!FileManager.default.fileExists(atPath: missingParentRoot.deletingLastPathComponent().path),
                    "missing ancestor inspection creates no directories")
        print("CHECK: cancellation recovers strictly initial ownership creation"); fflush(nil)
        for phase in ["empty-root", "empty-metadata", "lock-only", "checkpoint-only"] {
            let initialRoot = external.appendingPathComponent("cancel-initial-" + phase)
            try FileManager.default.createDirectory(at: initialRoot, withIntermediateDirectories: false)
            let initialMeta = initialRoot.appendingPathComponent(".madeira-epic")
            if phase != "empty-root" {
                try FileManager.default.createDirectory(at: initialMeta, withIntermediateDirectories: false)
                if phase != "empty-metadata" { try Data().write(to: initialMeta.appendingPathComponent("lock")) }
                if phase == "checkpoint-only" {
                    let checkpoint: [String: Any] = ["version": 1, "manifestHash": EpicInstallStore.digest(data), "appName": manifest.appName, "completed": []]
                    try JSONSerialization.data(withJSONObject: checkpoint).write(to: initialMeta.appendingPathComponent("checkpoint.json"))
                    try Data("interrupted metadata write".utf8).write(to: initialMeta.appendingPathComponent("write-" + UUID().uuidString))
                }
            }
            let initialID = UUID()
            let initialIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: initialRoot, cancellationID: initialID)!
            try EpicInstallStore.removeIncompleteDownload(destination: initialRoot, cancellationID: initialID, identity: initialIdentity)
            try require(!FileManager.default.fileExists(atPath: initialRoot.path), "initial " + phase + " safely cancels without a manifest")
        }
        for unsafe in ["bad-hash", "wrong-expected-hash", "bad-builder", "bad-version", "nonempty-completed", "unknown", "partial", "chunks", "game"] {
            let initialRoot = external.appendingPathComponent("cancel-initial-reject-" + unsafe)
            let initialMeta = initialRoot.appendingPathComponent(".madeira-epic")
            try FileManager.default.createDirectory(at: initialMeta, withIntermediateDirectories: true)
            try Data().write(to: initialMeta.appendingPathComponent("lock"))
            let checkpoint: [String: Any] = ["version": unsafe == "bad-version" ? 2 : 1,
                "manifestHash": unsafe == "bad-hash" ? "x" : EpicInstallStore.digest(data),
                "appName": unsafe == "bad-builder" ? String(repeating: "x", count: 257) : manifest.appName,
                "completed": unsafe == "nonempty-completed" ? ["Bin/Game.exe"] : []]
            try JSONSerialization.data(withJSONObject: checkpoint).write(to: initialMeta.appendingPathComponent("checkpoint.json"))
            switch unsafe {
            case "unknown": try Data("user metadata".utf8).write(to: initialMeta.appendingPathComponent("user.txt"))
            case "partial": try Data("partial".utf8).write(to: initialMeta.appendingPathComponent("partial-" + UUID().uuidString))
            case "chunks": try FileManager.default.createDirectory(at: initialMeta.appendingPathComponent("chunks"), withIntermediateDirectories: false)
            case "game": try Data("user game".utf8).write(to: initialRoot.appendingPathComponent("Game.exe"))
            default: break
            }
            let checkpointBefore = try Data(contentsOf: initialMeta.appendingPathComponent("checkpoint.json"))
            do {
                _ = try EpicInstallStore.inspectIncompleteDownload(destination: initialRoot, cancellationID: UUID(),
                    expectedManifestHash: unsafe == "wrong-expected-hash" ? String(repeating: "0", count: 64) : nil)
                throw NSError(domain: "unsafe initial cleanup accepted " + unsafe, code: 1)
            } catch is EpicDownloadCleanupError {}
            try require(FileManager.default.fileExists(atPath: initialMeta.appendingPathComponent("checkpoint.json").path), "unsafe initial ownership is preserved")
            try require(try Data(contentsOf: initialMeta.appendingPathComponent("checkpoint.json")) == checkpointBefore,
                        "rejected initial cleanup leaves checkpoint bytes unchanged")
            if unsafe == "unknown" {
                try require(try Data(contentsOf: initialMeta.appendingPathComponent("user.txt")) == Data("user metadata".utf8),
                            "rejected initial cleanup preserves user sentinel bytes")
            }
        }
        for initialPhase in ["empty-metadata", "checkpoint-write"] {
            let interruptedRoot = external.appendingPathComponent("cancel-initial-retry-" + initialPhase)
            let interruptedMeta = interruptedRoot.appendingPathComponent(".madeira-epic")
            try FileManager.default.createDirectory(at: interruptedMeta, withIntermediateDirectories: true)
            if initialPhase == "checkpoint-write" {
                try Data().write(to: interruptedMeta.appendingPathComponent("lock"))
                let checkpoint: [String: Any] = ["version": 1, "manifestHash": EpicInstallStore.digest(data), "appName": manifest.appName, "completed": []]
                try JSONSerialization.data(withJSONObject: checkpoint).write(to: interruptedMeta.appendingPathComponent("checkpoint.json"))
                try Data("pending ownership write".utf8).write(to: interruptedMeta.appendingPathComponent("write-" + UUID().uuidString))
            }
            let interruptedID = UUID()
            let interruptedIdentity = try EpicInstallStore.inspectIncompleteDownload(destination: interruptedRoot, cancellationID: interruptedID)!
            try JSONEncoder().encode(interruptedIdentity).write(to: interruptedMeta.appendingPathComponent("cancel-receipt-" + interruptedID.uuidString))
            let interruptedQuarantine = external.appendingPathComponent(".madeira-cancel-" + EpicInstallStore.digest(Data(interruptedRoot.lastPathComponent.utf8)) + "-" + interruptedID.uuidString)
            try FileManager.default.moveItem(at: interruptedRoot, to: interruptedQuarantine)
            try EpicInstallStore.removeIncompleteDownload(destination: interruptedRoot, cancellationID: interruptedID, identity: interruptedIdentity)
            try require(!FileManager.default.fileExists(atPath: interruptedQuarantine.path), "initial " + initialPhase + " receipt retry finishes")
        }
        print("PASS: incomplete cancellation ownership, preflight, identity and retry safety")
    }
}
"""


def main():
    global OUT
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--optimize", action="store_true")
    arguments.add_argument("--mutations", action="store_true")
    arguments.add_argument("--report-dir", type=Path, default=OUT)
    options = arguments.parse_args()
    OUT = options.report_dir
    OUT.mkdir(parents=True, exist_ok=True)
    missing = [str(path.relative_to(ROOT)) for path in SOURCES if not path.is_file()]
    if missing:
        raise SystemExit("RED: native Epic implementation missing: " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix="run-", dir=OUT) as external, \
         tempfile.TemporaryDirectory(prefix="madeira-epic-container-download-", dir=Path.home()) as apfs, \
         tempfile.TemporaryDirectory(prefix="madeira-epic-container-var-alias-", dir="/private/var/tmp") as alias:
        work = Path(external)
        fixtures = work / "fixtures"
        write_fixtures(fixtures)
        runner = work / "Runner.swift"
        runner.write_text(RUNNER)
        snapshots = []
        production_hashes = {}
        for source in SOURCES:
            content = source.read_bytes()
            snapshot = work / source.name
            snapshot.write_bytes(content)
            snapshots.append(snapshot)
            production_hashes[str(source.relative_to(ROOT))] = hashlib.sha256(content).hexdigest()
        executable = work / "epic-download-tests"
        compile_command = ["xcrun", "swiftc", "-j", "1", "-swift-version", "6", "-strict-concurrency=complete",
                           "-parse-as-library", "-module-cache-path", str(work / "module-cache"),
                           *map(str, snapshots), str(runner), "-o", str(executable), "-lz"]
        if options.optimize:
            compile_command.insert(2, "-O")
        compile_result = subprocess.run(compile_command, text=True, capture_output=True, timeout=120)
        (OUT / "compile-result.txt").write_text(compile_result.stdout + compile_result.stderr)
        if compile_result.returncode:
            raise SystemExit(compile_result.stdout + compile_result.stderr)
        environment = dict(os.environ)
        environment["SWIFT_BACKTRACE"] = "enable=no"
        result = subprocess.run([str(executable), str(fixtures), str(Path(apfs) / "installs"), str(Path(apfs) / "links"), str(alias)],
                                text=True, capture_output=True, timeout=90, env=environment)
        (OUT / "test-result.txt").write_text(result.stdout + result.stderr)
        print(result.stdout, end="")
        if result.returncode:
            raise SystemExit(result.stderr or f"native test exited {result.returncode}")
        mutation_results = []
        if options.mutations:
            mutations = [
                ("path-traversal", "EpicManifest.swift", 'part != ".", part != "..", part.utf8.count <= 255,\n                  !part.hasSuffix(".")', 'true, true, part.utf8.count <= 255,\n                  true'),
                ("manifest-SHA1", "EpicManifest.swift", "Data(Insecure.SHA1.hash(data: body)) == digest", "true"),
                ("chunk-SHA1", "EpicDownloader.swift", "sha == expected.sha1, chunkSHA == nil || chunkSHA == sha", "true"),
                ("file-SHA1", "EpicDownloader.swift", "Data(hash.finalize()) == file.fileHash", "true"),
                ("resume-SHA1", "EpicInstall.swift", "try Self.fileHash(descriptor, expectedSize: file.fileSize) == file.fileHash", "true"),
                ("manifest-isolation", "EpicInstall.swift", "checkpoint.manifestHash == manifestHash", "true"),
                ("exclusive-commit", "EpicInstall.swift", "UInt32(RENAME_EXCL)", "UInt32(0)"),
                ("CDN-origin", "EpicDownloader.swift", "Self.safeURL(url) && origins.contains(Self.origin(url))", "Self.safeURL(url)"),
                ("cancel-after-await", "EpicDownloader.swift", "try Task.checkCancellation()", "/* cancellation check removed */"),
                ("ancestor-no-follow", "EpicDirectory.swift", "openat(fd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)",
                 "openat(fd, component, O_RDONLY | O_DIRECTORY | O_CLOEXEC)"),
            ]
            for index, (name, source_name, old, new) in enumerate(mutations):
                source_index = next(i for i, source in enumerate(SOURCES) if source.name == source_name)
                original = snapshots[source_index].read_text()
                if old not in original:
                    raise SystemExit(f"mutation target missing: {name}")
                variant = work / ("mutation-" + SOURCES[source_index].name)
                variant.write_text(original.replace(old, new))
                mutant_sources = list(snapshots)
                mutant_sources[source_index] = variant
                binary = work / f"mutation-{index}"
                command = ["xcrun", "swiftc", "-j", "1", "-swift-version", "6", "-strict-concurrency=complete",
                           "-parse-as-library", "-module-cache-path", str(work / "module-cache"),
                           *map(str, mutant_sources), str(runner), "-o", str(binary), "-lz"]
                if options.optimize:
                    command.insert(2, "-O")
                compiled = subprocess.run(command, text=True, capture_output=True, timeout=90)
                if compiled.returncode:
                    raise SystemExit(f"mutation does not compile: {name}\n{compiled.stderr}")
                case_root = Path(apfs) / f"mutation-{index}"
                alias_root = Path(alias) / f"mutation-{index}"
                alias_root.mkdir()
                observed = subprocess.run([str(binary), str(fixtures), str(case_root / "installs"), str(case_root / "links"), str(alias_root)],
                                          text=True, capture_output=True, timeout=90, env=environment)
                if observed.returncode == 0:
                    raise SystemExit(f"test did not detect mutation: {name}")
                mutation_results.append({"name": name, "returncode": observed.returncode,
                                         "error": observed.stderr[:1000]})
            (OUT / "mutation-result.json").write_text(json.dumps(mutation_results, indent=2))
            print(f"PASS: {len(mutation_results)} compiled production mutations detected")
        (OUT / "result.json").write_text(json.dumps({"returncode": result.returncode,
             "production_sources": production_hashes, "stdout": result.stdout,
             "APFS_temporary_directory": apfs, "protected_var_alias_fixture": alias,
             "optimized": options.optimize}, indent=2))
    if Path(apfs).exists() or Path(alias).exists() or Path(external).exists():
        raise SystemExit("task-owned temporary directory cleanup failed")
    report = json.loads((OUT / "result.json").read_text())
    report["APFS_temporary_directory_removed"] = True
    report["protected_var_alias_fixture_removed"] = True
    report["external_work_directory_removed"] = True
    (OUT / "result.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
