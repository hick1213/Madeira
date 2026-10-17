#!/usr/bin/env python3
"""Execute actual artwork resolution; reject repeated per-card metadata scans.

Only SteamOwnedGame's data shape and the metadata lookup boundary are synthetic.
Production artwork, filename validation, cover URL construction and asset base
compile unchanged. No account, network, image-cache, preferences or device access.
"""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/SteamGames.swift"

HARNESS = r'''
import Foundation
import Darwin
struct SteamOwnedGame {
    var parentID: Int? = nil
    var libraryCapsule: String? = nil
    var headerImage: String? = nil
}
enum SteamGamesRules {
__PRODUCTION__
}
@main struct Check {
    static func main() {
        var failures = 0
        var assertions = 0
        func check(_ condition: Bool, _ name: String) {
            assertions += 1
            print("\(condition ? "PASS" : "FAIL"): \(name)")
            if !condition { failures += 1 }
        }
        func resolve(_ name: String, _ fixtures: [Int: SteamOwnedGame], _ expected: [String], _ expectedLookups: [Int]) {
            var lookups: [Int] = []
            let urls = SteamGamesRules.artwork(appID: 42) { id in
                lookups.append(id)
                return fixtures[id]
            }.map(\.absoluteString)
            check(urls == expected, name + " preserves ordered artwork candidates")
            print("Observed metadata lookups \(name): \(lookups)")
            check(lookups == expectedLookups, name + " looks up each required metadata record once")
        }
        resolve("own metadata", [42: SteamOwnedGame(libraryCapsule: "hashed/capsule.jpg", headerImage: "hashed/header.jpg")], [
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/42/hashed/capsule.jpg",
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg",
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/42/hashed/header.jpg"
        ], [42])
        resolve("demo parent", [42: SteamOwnedGame(parentID: 77, libraryCapsule: "demo/capsule.jpg", headerImage: "demo/header.jpg"),
            77: SteamOwnedGame(parentID: 88, libraryCapsule: "full/capsule.jpg", headerImage: "full/header.jpg")], [
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/42/demo/capsule.jpg",
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg",
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/77/full/capsule.jpg",
            "https://cdn.cloudflare.steamstatic.com/steam/apps/77/library_600x900.jpg",
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/42/demo/header.jpg"
        ], [42, 77])
        resolve("missing own metadata", [:], [
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg"
        ], [42])
        resolve("missing parent metadata", [42: SteamOwnedGame(parentID: 77)], [
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg",
            "https://cdn.cloudflare.steamstatic.com/steam/apps/77/library_600x900.jpg"
        ], [42, 77])
        resolve("duplicate header", [42: SteamOwnedGame(libraryCapsule: "same.jpg", headerImage: "same.jpg")], [
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/42/same.jpg",
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg"
        ], [42])
        resolve("self parent and unsafe files", [42: SteamOwnedGame(parentID: 42, libraryCapsule: "../escape.jpg", headerImage: "https://evil.test/image.jpg")], [
            "https://cdn.cloudflare.steamstatic.com/steam/apps/42/library_600x900.jpg"
        ], [42])
        for unsafe in ["", "/absolute.jpg", "folder/../escape.jpg", "image.jpg?auth=secret", "image.jpg#fragment", "unicode-é.jpg", "space image.jpg", String(repeating: "a", count: 257)] {
            check(!SteamGamesRules.safeAssetName(unsafe), "rejects unsafe artwork path: " + unsafe.prefix(40))
        }
        check(SteamGamesRules.safeAssetName("hashed_folder/library-capsule_2.jpg"), "accepts Steam's hashed relative artwork path")
        check(SteamGamesRules.cover(0) == nil && SteamGamesRules.cover(-1) == nil, "invalid app IDs never yield a legacy cover URL")
        print("Artwork lookup assertions: \(assertions); failures: \(failures)")
        exit(failures == 0 ? 0 : 1)
    }
}
'''


def main() -> int:
    extractor = runpy.run_path(str(ROOT / "tests/host/check-performance-integration.py"),
                              run_name="artwork_extractor")
    source = SOURCE.read_text()
    functions = [extractor["function"](source, signature) for signature in [
        "static func artwork(appID:", "static func safeAssetName(", "static func cover("]]
    constants = re.findall(r'(?m)^\s*static let assetBase\s*=\s*"[^"\n]+"\s*$', source)
    if len(constants) != 1:
        raise RuntimeError("expected one actual production artwork asset-base declaration")
    production = "\n".join(constants + functions)
    with tempfile.TemporaryDirectory(prefix="madeira-artwork-lookups-") as temporary:
        directory = Path(temporary)
        harness = directory / "check.swift"
        binary = directory / "check"
        harness.write_text(HARNESS.replace("__PRODUCTION__", production))
        compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "6",
                                   "-strict-concurrency=complete", "-warnings-as-errors",
                                   "-parse-as-library", "-O", str(harness), "-o", str(binary)],
                                  capture_output=True, text=True, timeout=90)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
