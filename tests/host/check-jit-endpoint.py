#!/usr/bin/env python3
"""Execute paired-peer selection and JIT endpoint message round trips on macOS."""

import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app/Madeira"


def main():
    archive = Path(os.environ["MADEIRA_RP_HOST_ARCHIVE"])
    with tempfile.TemporaryDirectory(prefix="madeira-jit-endpoint-") as tmp:
        directory = Path(tmp)
        harness = directory / "main.swift"
        harness.write_text(r"""
import Foundation

func require(_ value: Bool, _ message: String) {
    guard value else { fatalError(message) }
    print("PASS: \(message)")
}

let key = Data(base64Encoded: "Mgp6ZGPzXM2ku9br46vsiw==")!
let pairing = try PropertyListSerialization.data(fromPropertyList: ["alt_irk": key], format: .binary, options: 0)
let txt = ["identifier": Data("2BE6E510-0325-4365-923E-B14C6F57DB3A".utf8),
           "authTag": Data("kXjlTr2l".utf8)]
require(JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: txt, port: 56958) == 56958,
        "current paired peer supplies its changed port")
var other = txt
other["authTag"] = Data("AAAAAAAA".utf8)
require(JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: other, port: 49152) == nil,
        "another advertised device is rejected")
require(JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: txt, port: 0) == nil &&
        JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: txt, port: 65536) == nil,
        "invalid ports are rejected")
require(JITRemoteEndpoint.matchedPort(pairingData: Data(), txt: txt, port: 56958) == nil,
        "malformed pairing data cannot select a peer")
var invalidTXT = txt
invalidTXT["identifier"] = Data([0xff])
require(JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: invalidTXT, port: 56958) == nil,
        "invalid UTF8 TXT cannot select a peer")
invalidTXT = txt
invalidTXT["identifier"] = Data("2BE6E510-0325-4365-923E-B14C6F57DB3A\0ignored".utf8)
require(JITRemoteEndpoint.matchedPort(pairingData: pairing, txt: invalidTXT, port: 56958) == nil,
        "embedded NUL cannot truncate the authenticated identifier")
for request in [MadeiraJITRequest.prepare(pairingData: Data([1]), rsdPort: 56958),
                MadeiraJITRequest.enable(targetPID: 123, pairingData: Data([1]), scriptBase64: "AA==", rsdPort: 56958)] {
    let encoded = try JSONEncoder().encode(request)
    let decoded = try JSONDecoder().decode(MadeiraJITRequest.self, from: encoded)
    require(decoded.rsdPort == 56958, "helper request preserves resolved port for \(decoded.operation)")
}
let legacy = Data(#"{"operation":"prepare","pairingData":"AQ=="}"#.utf8)
require(try JSONDecoder().decode(MadeiraJITRequest.self, from: legacy).rsdPort == nil,
        "legacy request remains decodable")

MainActor.assumeIsolated {
    // Real Bonjour publication/resolution: no TCP listener and no device attach.
    let advertisement = NetService(domain: "local.", type: "_remotepairing._tcp.",
                                   name: "MadeiraEndpointTest-\(UUID().uuidString)", port: 56958)
    require(advertisement.setTXTRecord(NetService.data(fromTXTRecord: txt)), "test TXT is accepted")
    advertisement.publish()
    defer { advertisement.stop() }
    var selected: UInt16?
    var callbacks = 0
    JITRemoteEndpoint.resolve(pairingData: pairing) { port in selected = port; callbacks += 1 }
    let deadline = Date(timeIntervalSinceNow: 6)
    while selected == nil && Date() < deadline {
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
    require(selected == 56958 && callbacks == 1, "real Bonjour lookup selects only the authenticated port once")

    var unknownKey = key
    unknownKey[0] ^= 1
    let unknownPairing = try! PropertyListSerialization.data(fromPropertyList: ["alt_irk": unknownKey], format: .binary, options: 0)
    var fallback: UInt16?
    var fallbackCallbacks = 0
    let start = Date()
    JITRemoteEndpoint.resolve(pairingData: unknownPairing) { port in fallback = port; fallbackCallbacks += 1 }
    let fallbackDeadline = Date(timeIntervalSinceNow: 6)
    while fallback == nil && Date() < fallbackDeadline {
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
    require(fallback == 49152 && fallbackCallbacks == 1 && Date().timeIntervalSince(start) >= 3.9,
            "unmatched Bonjour peers reach the bounded default fallback once")
    require(callbacks == 1, "completed lookup cannot complete again at its old deadline")
}
""")
        executable = directory / "endpoint-test"
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-O",
                "-import-objc-header",
                str(APP / "MadeiraRPPairing.h"),
                str(APP / "JITRemoteEndpoint.swift"),
                str(APP / "JITBuiltInMessages.swift"),
                str(harness),
                str(archive),
                "-lresolv",
                "-framework",
                "Security",
                "-framework",
                "SystemConfiguration",
                "-o",
                str(executable),
            ],
            check=True,
            timeout=120,
        )
        subprocess.run([str(executable)], check=True, timeout=30)
    print("check-jit-endpoint: PASS")


if __name__ == "__main__":
    main()
