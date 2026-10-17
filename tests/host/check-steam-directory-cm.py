#!/usr/bin/env python3
"""Exercise the actual CM directory adapter with independently encoded wire fixtures."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def integer(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def blob(field: int, value: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(value)) + value


def string(field: int, value: str) -> bytes:
    return blob(field, value.encode())


def swift(value: bytes) -> str:
    return 'Data([' + ','.join(str(byte) for byte in value) + '])'


def main() -> None:
    # Valve's ServerInfo field numbers; this encoder does not use production helpers.
    server = (string(1, 'CDN') + integer(2, (1 << 64) - 7) + integer(3, 198)
              + integer(4, 0) + bytes([45, 0, 0, 192, 63]) + integer(6, 9)
              + integer(7, 0) + string(8, 'st.dl.eccdnx.com')
              + string(9, 'virtual.vendor.example') + integer(10, 1)
              + string(11, '/proxy/%s') + string(12, 'unavailable')
              + integer(13, 0) + blob(13, varint(42) + varint((1 << 32) - 1))
              + integer(15, 3) + string(16, 'CDN') + string(16, 'Proxy')
              + string(17, 'mainland') + blob(100, b'synthetic-secret'))
    another = string(8, 'independent.vendor.example') + string(12, 'mandatory')
    fixtures = {
        'full': blob(1, server) + blob(1, another) + integer(2, 0),
        'explicitEmpty': b'',
        'noChange': integer(2, 1),
        'withRowsNoChange': blob(1, another) + integer(2, 1),
        'emptyRow': blob(1, b''),
        'unknown': integer(99, 7) + blob(100, b'x') + bytes([157, 6, 1, 2, 3, 4]),
    }
    invalid = {
        'truncated submessage': b'\x0a\x03\x42',
        'invalid UTF8': blob(1, blob(8, b'\xff')),
        'zero field tag': b'\x00\x00',
        'oversized field tag': varint(1 << 40) + b'\x00',
        'invalid wire type': b'\x0e',
        'wrong known wire type': b'\x08\x01',
        'wrong server wire type': blob(1, integer(8, 7)),
        'truncated fixed32': blob(1, b'\x2d\x00'),
        'nonfinite weighted load': blob(1, b'\x2d\x00\x00\x80\x7f'),
        'packed ID overflow': blob(1, blob(13, varint(1 << 32))),
        'unpacked ID overflow': blob(1, integer(13, 1 << 32)),
        'truncated packed ID': blob(1, blob(13, b'\x80')),
        'overflowing varint': blob(1, blob(13, b'\xff' * 9 + b'\x02')),
        'truncated unknown field': b'\xa1\x06\x01',
        'invalid nested tag': blob(1, b'\x00\x00'),
    }
    declarations = '\n'.join(f'let {key} = {swift(value)}' for key, value in fixtures.items())
    rejects = '\n'.join(f'try rejected({swift(value)}, "{name}")' for name, value in invalid.items())
    checks = r'''
import Foundation
struct CheckFailure: Error { let message: String }
func require(_ value: Bool, _ message: String) throws {
    guard value else { throw CheckFailure(message: message) }
    print("PASS: " + message)
}
func rows(_ data: Data) throws -> [[String: Any]] {
    let object = try JSONSerialization.jsonObject(with: SteamContentDirectoryMessage.response(data)) as! [String: Any]
    return (object["response"] as! [String: Any])["servers"] as! [[String: Any]]
}
func rejected(_ data: Data, _ name: String) throws {
    do { _ = try SteamContentDirectoryMessage.response(data) }
    catch { print("PASS: rejects " + name); return }
    throw CheckFailure(message: "accepted " + name)
}
@main struct Checks {
    static func main() throws {
        try require(SteamContentDirectoryMessage.request(cellID: 198) == Data([8,198,1,16,100]), "request cell and max_servers fields")
        try require(SteamContentDirectoryMessage.request(cellID: 0) == Data([8,0,16,100]), "zero cell remains explicit")
        try require(SteamContentDirectoryMessage.request(cellID: 0, ipOverride: "114.114.114.114") == Data([8,0,16,100,26,15,49,49,52,46,49,49,52,46,49,49,52,46,49,49,52]), "mainland ip_override is field 3")
        FIXTURES
        let parsed = try rows(full)
        let expected: [String: Any] = ["type": "CDN", "source_id": -7, "cell_id": 198,
            "load": 0, "weighted_load": 1.5, "num_entries": 9, "steam_china_only": false,
            "host": "st.dl.eccdnx.com", "vhost": "virtual.vendor.example", "use_as_proxy": true,
            "proxy_request_path_template": "/proxy/%s", "https_support": "unavailable",
            "allowed_app_ids": [UInt32(0), UInt32(42), UInt32.max], "priority_class": 3,
            "bypass_proxies_of_type": ["CDN", "Proxy"], "group": "mainland"]
        try require(parsed.count == 2 && NSDictionary(dictionary: parsed[0]).isEqual(to: expected), "all public protocol metadata and packed/unpacked app IDs preserved")
        try require(parsed[1]["host"] as? String == "independent.vendor.example", "directory retains other vendors")
        try require(parsed[1]["type"] == nil && parsed[1]["use_as_proxy"] == nil && parsed[1]["allowed_app_ids"] == nil, "missing fields remain absent")
        try require(!(String(data: try SteamContentDirectoryMessage.response(full), encoding: .utf8)!).contains("synthetic-secret"), "unknown metadata is never emitted")
        try require(try rows(explicitEmpty).isEmpty, "explicit empty response remains authoritative")
        try rejected(noChange, "no_change without a base directory")
        try require(try rows(withRowsNoChange).count == 1, "nonempty rows remain usable with no_change")
        try require(try rows(emptyRow).first?.isEmpty == true, "empty row does not fabricate protocol defaults")
        try require(try rows(unknown).isEmpty, "well formed unknown fields skip")
        REJECTS
        try rejected(Data(repeating: 0, count: 2 * 1024 * 1024 + 1), "raw response above 2 MiB")
        // The raw response fits, but JSON escaping expands the public string beyond the bound.
        let huge = Data([10,132,128,8,66,128,128,8]) + Data(repeating: 1, count: 131072)
        var expanded = Data()
        for _ in 0..<3 { expanded.append(huge) }
        try rejected(expanded, "decoded JSON above 2 MiB")
    }
}
'''.replace('FIXTURES', declarations).replace('REJECTS', rejects)
    with tempfile.TemporaryDirectory(prefix='madeira-steam-cm-directory-') as directory:
        temporary = Path(directory)
        harness = temporary / 'checks.swift'
        harness.write_text(checks)
        executable = temporary / 'check'
        compiled = subprocess.run(['xcrun', 'swiftc', '-j', '1', '-parse-as-library',
                                   '-swift-version', '5', str(ROOT / 'app/Madeira/SwiftSteam/Proto/SteamProtoMessages.swift'),
                                   str(ROOT / 'app/Madeira/SwiftSteam/Core/SteamError.swift'), str(harness),
                                   '-o', str(executable)], capture_output=True, text=True, timeout=120)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
        print(result.stdout, end='')
        if result.returncode:
            raise RuntimeError(result.stderr[-5000:])


if __name__ == '__main__':
    main()
