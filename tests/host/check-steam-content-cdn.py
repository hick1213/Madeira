#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 125hz
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Execute production chunk retries/health with real AES/ZIP and local HTTP.

Never contacts Steam or reads credentials. Requires macOS Swift/SDK and openssl.
Changing retry selection to an index into a newly sorted list must fail these
tests: it skips a healthy server and repeats a failed one. The last pool member
must remain reachable, and corrupt/cancelled transfers must not write a chunk.
Manual selection must override measured automatic choices, with a visible
fallback after failure. Cancelling a real manifest request must retain health.
"""

import hashlib
import time
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[2]
STEAM = ROOT / "app/Madeira/SwiftSteam"


def block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start:index + 1]
    raise ValueError("unterminated production declaration: " + marker)


HARNESS = r'''
import Foundation
import CommonCrypto
import zlib

nonisolated(unsafe) var manifestBarrier: String?
nonisolated(unsafe) var encryptedFixture: Data?
final class ManifestStall: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host?.hasSuffix(".manifest-fixture.steamcontent.com") == true || request.url?.host?.hasSuffix(".chunk-fixture.steamcontent.com") == true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        if request.url?.host?.hasSuffix(".chunk-fixture.steamcontent.com") == true {
            require(request.url?.scheme == "https" && request.url?.query == "auth=synthetic-chunk", "HTTPS chunk retains exact origin-scoped CDN token")
            require(request.value(forHTTPHeaderField: "Authorization") == nil && request.value(forHTTPHeaderField: "Cookie") == nil, "HTTPS chunk carries no account bearer or cookie")
            let body = encryptedFixture!
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["Content-Length": String(body.count)])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        require(request.url?.host == "two.manifest-fixture.steamcontent.com", "manual HTTPS manifest candidate is used")
        require(request.url?.scheme == "https", "manifest transport remains HTTPS")
        require(request.url?.query == "auth=synthetic-manifest", "HTTPS manifest retains its origin-scoped CDN token")
        require(request.value(forHTTPHeaderField: "Authorization") == nil && request.value(forHTTPHeaderField: "Cookie") == nil, "manifest carries no account authorization or cookies")
        require(request.url?.path == "/depot/42/manifest/1/5/7", "secure manifest request code stays on HTTPS")
        if let path = manifestBarrier { try! Data([1]).write(to: URL(fileURLWithPath: path)) }
    }
    override func stopLoading() {}
}
nonisolated(unsafe) var failureBarrier: String?
enum SteamLog {
    static func event(_ line: String) { print(line) }
    static func trace(_ line: @autoclosure () -> String) {
        if line().hasPrefix("chunk attempt 1 failed:"), let path = failureBarrier {
            try! Data([1]).write(to: URL(fileURLWithPath: path), options: .atomic)
        }
    }
}
func require(_ value: Bool, _ message: String) {
    guard value else { fatalError(message) }
    print("PASS: \(message)")
}
func hex(_ value: String) -> Data {
    var result = Data()
    for index in stride(from: 0, to: value.count, by: 2) {
        let start = value.index(value.startIndex, offsetBy: index)
        result.append(UInt8(value[start..<value.index(start, offsetBy: 2)], radix: 16)!)
    }
    return result
}
func waitForBarrier(_ path: String) async throws {
    let deadline = Date(timeIntervalSinceNow: 10)
    while !FileManager.default.fileExists(atPath: path), Date() < deadline {
        try await Task.sleep(nanoseconds: 10_000_000)
    }
    require(FileManager.default.fileExists(atPath: path), "observed the requested operation before cancelling")
}
@main struct Check {
    static func main() async throws {
        let raw = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let fixture = try JSONSerialization.jsonObject(with: raw) as! [String: Any]
        encryptedFixture = hex(fixture["wire"] as! String)
        let hosts = fixture["hosts"] as! [String]
        failureBarrier = fixture["failurePath"] as? String
        let mode = fixture["mode"] as! String
        let manual = ["manual", "manual-fallback", "manifest-cancel"].contains(mode) ? hosts.last : nil
        let health = ContentHostHealth(manualHost: manual)
        if let manual {
            health.recordSuccess(hosts[0], bytes: 10_000, seconds: 0.1)
            health.recordSuccess(manual, bytes: 1_000, seconds: 1)
            require(health.order(hosts, seed: 0).first == manual,
                    "manual node remains first despite a faster measured automatic node")
        }
        let chunk = DepotManifest.ChunkEntry(sha: hex(fixture["sha"] as! String),
            crc: (fixture["crc"] as! NSNumber).uint32Value, offset: 0,
            compressedSize: (fixture["encryptedSize"] as! NSNumber).uint32Value,
            uncompressedSize: (fixture["plainSize"] as! NSNumber).uint32Value)
        let plan = DepotDownloader.DepotPlan(depotID: 42, manifestGID: 1,
            key: hex(fixture["key"] as! String), manifest: DepotManifest(), hosts: hosts,
            auth: fixture["auth"] as! [String: String],
            declaredSize: 0, health: health)
        let path = fixture["path"] as! String
        if mode == "benchmark" || mode == "benchmark-corrupt" || mode == "benchmark-recovery" || mode == "benchmark-forbidden" {
            let fastest = try await DepotDownloader.measure(chunk, plan: plan, path: path)
            require(fastest == fixture["expectedFastest"] as? String, "fastest verified candidate is selected after all probes")
            require(try Data(contentsOf: URL(fileURLWithPath: path)) == hex(fixture["plain"] as! String), "probe chunk reused without a second download")
            if mode != "benchmark-recovery" {
            let next = path + ".next"
            try Data(repeating: 0, count: Int(chunk.uncompressedSize)).write(to: URL(fileURLWithPath: next))
            try await DepotDownloader.run(chunk, plan: plan, path: next)
            require(try Data(contentsOf: URL(fileURLWithPath: next)) == hex(fixture["plain"] as! String), "following chunk uses verified fastest source")
            }
            if mode == "benchmark-corrupt" { require(health.verifiedSamples(cellID: 0).count == 2, "corrupt fast origin excluded from speed results") }
        } else if mode == "benchmark-cancel" {
            let task = Task { try await DepotDownloader.measure(chunk, plan: plan, path: path) }
            defer { task.cancel() }
            try await waitForBarrier(fixture["startedPath"] as! String)
            task.cancel()
            do { _ = try await task.value; fatalError("cancelled benchmark accepted") } catch is CancellationError {}
            require(health.verifiedSamples(cellID: 0).isEmpty, "cancelled probe produces no speed")
            require(try Data(contentsOf: URL(fileURLWithPath: path)) == Data(repeating: 0, count: Int(chunk.uncompressedSize)), "cancelled probe never writes data")
        } else if mode == "manifest-cancel" {
            manifestBarrier = fixture["startedPath"] as? String
            let downloader = DepotDownloader()
            let task = Task { try await downloader.runManifest(hosts: hosts, health: health,
                auth: fixture["auth"] as! [String: String], key: plan.key) }
            defer { task.cancel() }
            try await waitForBarrier(fixture["startedPath"] as! String)
            task.cancel()
            do { try await task.value; fatalError("cancelled manifest accepted") }
            catch is CancellationError { }
            require(health.failedHosts().isEmpty,
                    "manifest cancellation does not turn a healthy CDN into a failed host")
            let samples = health.verifiedSamples(cellID: 0)
            require(samples.count == 2 && samples.allSatisfy { $0.successes == 1 } &&
                    Set(samples.map(\.bytesPerSecond)) == Set([100_000.0, 1_000.0]),
                    "manifest cancellation retains both verified rates and success histories")
            require(health.manualFallbackNotice == nil,
                    "manifest cancellation does not show a manual-node failure notice")
        } else if mode == "cancel" || mode == "backoff-cancel" {
            let task = Task { try await DepotDownloader.run(chunk, plan: plan, path: path) }
            defer { task.cancel() }
            try await waitForBarrier(fixture[mode == "cancel" ? "startedPath" : "failurePath"] as! String)
            task.cancel()
            do { try await task.value; fatalError("cancelled transfer accepted") }
            catch is CancellationError { }
            if mode == "cancel" {
                require(health.order(hosts, seed: 0).first == hosts.first &&
                        health.select(hosts, excluding: [], seed: 0) == hosts[0] &&
                        health.select(hosts, excluding: [], seed: 0) == hosts[1],
                        "active cancellation releases the reservation without penalizing its host")
                for host in hosts { health.recordCancellation(host) }
            } else {
                require(health.order(hosts, seed: 0).first == hosts[1],
                        "backoff cancellation retains only the observed server failure")
            }
        } else if mode == "write-failure" {
            do { try await DepotDownloader.run(chunk, plan: plan, path: path); fatalError("FIFO pwrite accepted") }
            catch SteamError.chunkDownloadFailed(let reason) {
                require(reason.contains("Cannot write a game file"), "actual pwrite failure reaches the caller")
            }
        } else if mode == "corrupt" || mode == "sha-mismatch" {
            do { try await DepotDownloader.run(chunk, plan: plan, path: path); fatalError("corrupt chunk accepted") }
            catch { }
            require(health.verifiedSamples(cellID: 0).isEmpty,
                    "invalid payloads never produce a verified speed record")
        } else {
            try await DepotDownloader.run(chunk, plan: plan, path: path)
            require(try Data(contentsOf: URL(fileURLWithPath: path)) == hex(fixture["plain"] as! String),
                    "authenticated, decrypted and verified chunk bytes reach the file")
            if mode == "manual" {
                require(health.manualFallbackNotice == nil, "successful manual transfer has no fallback notice")
            } else if mode == "manual-fallback" {
                require(health.manualFallbackNotice?.contains("failed; using automatic fallback") == true,
                        "manual HTTP failure reports the automatic fallback")
            }
        }
        if mode == "corrupt" || mode == "sha-mismatch" || mode == "cancel" || mode == "backoff-cancel" || mode == "manifest-cancel" {
            require(try Data(contentsOf: URL(fileURLWithPath: path)) == Data(repeating: 0, count: Int(chunk.uncompressedSize)),
                    "unverified or cancelled bytes never reach the file")
        }
        EXTRA_HEALTH_CHECKS
    }
}
'''

HEALTH_CHECKS = r'''
let measured = ContentHostHealth()
let candidates = ["https://fast.steamcontent.com", "https://medium.steamcontent.com", "https://slow.steamcontent.com"]
require(measured.select(candidates, excluding: [], seed: 0) == candidates[0] &&
        measured.select(candidates, excluding: [], seed: 0) == candidates[1] &&
        measured.select(candidates, excluding: [], seed: 0) == candidates[2],
        "initial parallel transfers explore distinct unmeasured servers")
measured.recordSuccess(candidates[0], bytes: 2000, seconds: 0.1)
require(measured.select(candidates, excluding: [], seed: 0) == candidates[0],
        "pending unmeasured servers do not absorb every newly available chunk")
measured.recordCancellation(candidates[0])
measured.recordSuccess(candidates[1], bytes: 2000, seconds: 0.4)
measured.recordSuccess(candidates[2], bytes: 2000, seconds: 1)
require(measured.order(candidates, seed: 2) == candidates,
        "actual successful payload rates beat round-robin ordering")
for _ in 0..<4 { _ = measured.select([candidates[0]], excluding: [], seed: 0) }
require(measured.select(candidates, excluding: [], seed: 0) == candidates[1],
        "in-flight load moves work to the next productive server")
measured.recordCancellation(candidates[1])
for _ in 0..<4 { measured.recordCancellation(candidates[0]) }
require(measured.order(candidates, seed: 1).first == candidates[0],
        "cancellation releases load without demoting the server")
measured.recordFailure(candidates[0], reason: "synthetic")
require(measured.order(candidates, seed: 0).first == candidates[1],
        "a failed fast server yields to verified healthy servers")
measured.recordSuccess(candidates[0], bytes: 2000, seconds: 0.1)
for _ in 0..<12 { measured.recordSuccess(candidates[0], bytes: 2000, seconds: 2) }
require(measured.order(candidates, seed: 0).first == candidates[1],
        "recent slow payload transfers replace an obsolete fast measurement")
require(measured.select(candidates, excluding: Set(candidates), seed: 0) == nil,
        "an exhausted retry round has no hidden repeated candidate")
let offered = (1...9).map { "https://cache\($0).steamcontent.com" } + ["https://steampipe.akamaized.net"]
require(DepotDownloader.contentHostPool(offered, maximum: 6) == offered,
    "every official candidate remains in the pool regardless of transfer concurrency")
require(DepotDownloader.contentHostPool([offered[0], offered[0], offered[1]], maximum: 6) == Array(offered.prefix(2)),
        "duplicate directory entries do not consume retry slots")
let parallel = ContentHostHealth()
let six = (1...6).map { "https://parallel\($0).steamcontent.com" }
let reservationLock = NSLock()
var reservations: [String] = []
DispatchQueue.concurrentPerform(iterations: 60) { _ in
    let host = parallel.select(six, excluding: [], seed: 0)!
    reservationLock.lock(); reservations.append(host); reservationLock.unlock()
}
require(six.allSatisfy { host in reservations.filter { $0 == host }.count == 10 },
        "simultaneous reservations atomically distribute pending work")
for host in reservations { parallel.recordCancellation(host) }
require(parallel.order(six, seed: 0) == six,
        "concurrent reservations leave no load after cancellation")
'''


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, payload, number):
        super().__init__(("127.0.0.1", 0), Handler)
        self.payload = payload
        self.mode = "bad"
        self.requests = 0
        self.unauthorized = 0
        self.lock = threading.Lock()
        self.token = f"synthetic-host-{number}"
        self.started_path = None
        self.release = threading.Event()
        self.request_kind = "chunk"
        self.redirect_target = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.lock:
            self.server.requests += 1
            mode = self.server.mode
            started_path, release = self.server.started_path, self.server.release
            expected = ("/depot/42/manifest/1/5/7" if self.server.request_kind == "manifest"
                        else "/depot/42/chunk/" + ("0" * 40 if mode == "sha-mismatch" else self.server.sha))
            if self.path != expected or self.headers.get("Authorization") or self.headers.get("Cookie"):
                self.server.unauthorized += 1
                self.send_error(403)
                return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target + expected + "?auth=" + self.server.token)
            self.end_headers()
            return
        if mode == "probe-fail-then-good" and self.server.requests == 1:
            self.send_error(503)
            return
        if mode == "forbidden":
            self.send_error(403)
            return
        if mode in ("bench-slow", "bench-fast", "bench-corrupt"):
            time.sleep({"bench-slow": 0.15, "bench-fast": 0.015, "bench-corrupt": 0.001}[mode])
        if mode == "slow":
            Path(started_path).write_bytes(b"started")
            if not release.wait(timeout=15):
                self.send_error(504)
                return
        if mode == "bad":
            self.send_error(503)
            return
        body = self.server.payload if mode not in ("corrupt", "bench-corrupt") else b"corrupt AES payload"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def encrypt(key, data):
    iv = bytes(range(16))
    def openssl(arguments, body):
        return subprocess.run(["openssl", "enc"] + arguments, input=body,
                              capture_output=True, check=True, timeout=30).stdout
    encrypted_iv = openssl(["-aes-256-ecb", "-nopad", "-K", key.hex()], iv)
    encrypted = openssl(["-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex()], data)
    return encrypted_iv + encrypted


def main():
    source = (STEAM / "Content/DepotDownloader.swift").read_text()
    manifest = (STEAM / "Content/DepotManifest.swift").read_text()
    plain = bytes(range(256)) * 8
    key = bytes(range(32))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("chunk", plain)
    payload = encrypt(key, archive.getvalue())
    servers = [Server(payload, number) for number in range(6)]
    for server in servers:
        server.sha = hashlib.sha1(plain).hexdigest()
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="madeira-steam-cdn-") as temporary:
            work = Path(temporary)
            methods = [block(source, name) for name in [
                "struct DepotPlan:", "private nonisolated static func fetchChunk(", "private nonisolated static func benchmarkChunk(", "private nonisolated static func verifiedChunk(",
                "private nonisolated static func failureReason(",
                "private nonisolated static func download(", "private nonisolated static func write(",
                "nonisolated static func contentHostPool(", "private func fetchManifest("]]
            http = source[source.index("private nonisolated static let http:"):
                          source.index("    /// Replaces the content server directory")]
            http = http.replace("let config = URLSessionConfiguration.ephemeral", "let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [ManifestStall.self]")
            downloader = "import Foundation\nimport CommonCrypto\nimport zlib\nfinal class DepotDownloader {\n"
            downloader += http + "\n" + "\n".join(methods)
            downloader += "\nstatic func run(_ chunk: DepotManifest.ChunkEntry, plan: DepotPlan, path: String) async throws {\n"
            downloader += "try await fetchChunk(chunk, plan: plan, path: path, attempts: 5, seed: 0)\n}\n"
            downloader += "static func measure(_ chunk: DepotManifest.ChunkEntry, plan: DepotPlan, path: String) async throws -> String? { try await benchmarkChunk(chunk, plan: plan, path: path) }\n"
            # Only protocol credentials/request-code acquisition and the parser
            # that cancellation must never reach are boundaries. The complete
            # production manifest fetch, HTTP transfer and health are executed.
            downloader += r'''
private var manifestAuth: [String: String] = [:]
private var depotCache: URL? { nil }
private func manifestRequestCode(depotID: UInt32, appID: UInt32, manifestGID: UInt64) async throws -> UInt64 {
    precondition(depotID == 42 && appID == 7 && manifestGID == 1)
    return 7
}
private func cdnAuthFragment(depotID: UInt32, appID: UInt32, host: String) async -> String {
    precondition(depotID == 42 && appID == 7)
    return manifestAuth[host]!
}
private nonisolated static func parseManifestKeepingPayload(_ raw: Data, depotID: UInt32,
    manifestGID: UInt64, key: Data) throws -> (DepotManifest, Data) {
    fatalError("cancelled manifest must not reach the parser")
}
func runManifest(hosts: [String], health: ContentHostHealth, auth: [String: String], key: Data) async throws {
    manifestAuth = auth
    _ = try await fetchManifest(depotID: 42, appID: 7, manifestGID: 1, key: key,
                               hosts: hosts, health: health)
}
}
'''
            if "final class ContentRedirectPolicy:" in source:
                downloader += "\n" + block(source, "final class ContentRedirectPolicy:") + "\n"
            downloader += block(source, "final class ContentHostHealth:")
            (work / "downloader.swift").write_text(downloader)
            (work / "manifest.swift").write_text("import Foundation\nstruct DepotManifest {\n" +
                block(manifest, "struct ChunkEntry {") + "\n" +
                "static let customExecutableFlag: UInt32 = 0x80\n" +
                "struct FileEntry { var flags: UInt32 }; var files: [FileEntry] = []\n}\n")
            (work / "checks.swift").write_text(HARNESS.replace("EXTRA_HEALTH_CHECKS", HEALTH_CHECKS))
            header = STEAM / "lzma_shim.h"
            objects = []
            for name in ["lzma_shim.c", "chunk_zip.c", "zstd_edu.c"]:
                obj = work / (name + ".o")
                subprocess.run(["xcrun", "clang", "-c", "-g", "-fsanitize=address",
                                str(STEAM / name), "-o", str(obj)], check=True, capture_output=True, timeout=120)
                objects.append(str(obj))
            executable = work / "check"
            subprocess.run(["xcrun", "swiftc", "-j", "1", "-parse-as-library", "-swift-version", "5",
                "-sanitize=address", "-g", "-import-objc-header", str(header),
                str(work / "downloader.swift"), str(work / "manifest.swift"),
                str(STEAM / "Content/SteamDownloadSources.swift"),
                str(STEAM / "Content/ContentDecryptor.swift"), str(STEAM / "Core/SteamError.swift"),
                str(work / "checks.swift"), *objects, "-lz", "-llzma", "-o", str(executable)],
                check=True, capture_output=True, timeout=120)

            def run(mode, rows, expected_max=None):
                started_path = work / f"{mode}-request-started"
                failure_path = work / f"{mode}-first-failure"
                for server in servers:
                    with server.lock:
                        server.requests = 0
                        server.unauthorized = 0
                        server.mode = "bad"
                        server.request_kind = "manifest" if mode == "manifest-cancel" else "chunk"
                        server.started_path = started_path
                        server.release = threading.Event()
                for server, kind in rows:
                    server.mode = kind
                path = work / f"{mode}-chunk.bin"
                reader = None
                if mode == "write-failure":
                    os.mkfifo(path)
                    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                else:
                    path.write_bytes(bytes(len(plain)))
                fixture = dict(mode=mode, hosts=[server.url for server, _kind in rows],
                    auth={server.url: "?auth=" + server.token for server, _kind in rows},
                    startedPath=str(started_path), failurePath=str(failure_path),
                    key=key.hex(), expectedFastest=(rows[0][0].url if mode == "benchmark-recovery" else rows[1][0].url) if mode.startswith("benchmark") else None,
                    wire=payload.hex(),
                    sha=("0" * 40 if mode == "sha-mismatch" else servers[0].sha), crc=zlib.adler32(plain, 0), plain=plain.hex(),
                    plainSize=len(plain), encryptedSize=len(payload), path=str(path))
                if mode == "https-chunk":
                    fixture["hosts"] = ["https://one.chunk-fixture.steamcontent.com"]
                    fixture["auth"] = {host: "?auth=synthetic-chunk" for host in fixture["hosts"]}
                if mode == "benchmark-forbidden":
                    secure = "https://one.chunk-fixture.steamcontent.com"
                    fixture["hosts"].append(secure)
                    fixture["auth"][secure] = "?auth=synthetic-chunk"
                    fixture["expectedFastest"] = secure
                if mode == "manifest-cancel":
                    fixture["hosts"] = ["https://one.manifest-fixture.steamcontent.com", "https://two.manifest-fixture.steamcontent.com"]
                    fixture["auth"] = {host: "?auth=synthetic-manifest" for host in fixture["hosts"]}
                file = work / "fixture.json"
                file.write_text(json.dumps(fixture))
                try:
                    result = subprocess.run([str(executable), str(file)], capture_output=True,
                                            text=True, timeout=35)
                finally:
                    for server in servers:
                        server.release.set()
                    if reader is not None:
                        os.close(reader)
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr[-3500:])
                counts = [server.requests for server, _kind in rows]
                if mode == "benchmark-forbidden" and (counts != [1, 1, 1] or result.stdout.count("reason=http403") != 3):
                    raise AssertionError(f"three HTTP403 probes must be diagnosed once then HTTPS must serve both verified chunks: {counts}\n{result.stdout}")
                if mode == "benchmark-recovery" and counts != [2, 1, 1]:
                    raise AssertionError(f"failed short probes must use normal verified chunk retry path: {counts}")
                if mode in ("benchmark", "benchmark-corrupt") and counts != [1, 2, 1]:
                    raise AssertionError(f"all candidates must be probed then next chunk must use fastest verified: {counts}")
                if mode == "benchmark-cancel" and counts != [1, 0]:
                    raise AssertionError(f"cancelled probe cannot start another node: {counts}")
                if expected_max is not None and any(count > expected_max for count in counts):
                    raise AssertionError(f"a failed host was retried before another host: {counts}")
                if mode in ("cancel", "backoff-cancel", "write-failure") and counts != [1, 0]:
                    raise AssertionError(f"{mode} must observe the first transfer and avoid a second: {counts}")
                if mode == "manual" and counts != [0, 0, 1]:
                    raise AssertionError(f"manual selection must actually request only the last node: {counts}")
                if mode == "manual-fallback" and counts != [1, 1]:
                    raise AssertionError(f"manual failure must try the selected last node then its healthy peer: {counts}")
                if mode == "manifest-cancel" and counts != [0, 0]:
                    raise AssertionError(f"secure manifest fixture must never send a request code to local HTTP nodes: {counts}")
                if any(server.unauthorized for server in servers):
                    raise AssertionError("authorization or chunk URL was changed")
                print(result.stdout.strip())
                print(f"PASS: {mode} HTTP requests {counts}")

            servers[0].redirect_target = servers[2].url
            run("benchmark-forbidden", [(server, "forbidden") for server in servers[:3]])
            run("benchmark", [(servers[0], "bench-slow"), (servers[1], "bench-fast"), (servers[2], "bench-slow")])
            run("benchmark-corrupt", [(servers[0], "bench-slow"), (servers[1], "bench-fast"), (servers[2], "bench-corrupt")])
            run("benchmark-recovery", [(server, "probe-fail-then-good") for server in servers[:3]])
            run("benchmark-cancel", [(servers[0], "slow"), (servers[1], "good")])
            run("redirect", [(servers[0], "redirect"), (servers[1], "good")], 1)
            run("retry", [(servers[0], "bad"), (servers[1], "good"), (servers[2], "bad")], 1)
            run("last-host", [(server, "good" if index == 5 else "bad")
                              for index, server in enumerate(servers)], 1)
            run("sha-mismatch", [(servers[0], "sha-mismatch"), (servers[1], "sha-mismatch")])
            run("corrupt", [(servers[0], "corrupt"), (servers[1], "corrupt")])
            run("cancel", [(servers[0], "slow"), (servers[1], "good")], 1)
            run("backoff-cancel", [(servers[0], "bad"), (servers[1], "good")], 1)
            run("write-failure", [(servers[0], "good"), (servers[1], "good")], 1)
            run("manual", [(servers[0], "good"), (servers[1], "good"), (servers[2], "good")], 1)
            run("manual-fallback", [(servers[0], "good"), (servers[1], "bad")], 1)
            run("https-chunk", [(servers[0], "good")])
            run("manifest-cancel", [(servers[0], "good"), (servers[1], "slow")], 1)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        for output in (error.stdout, error.stderr):
            if output:
                print(output.decode(errors="replace") if isinstance(output, bytes) else output, end="")
        raise
