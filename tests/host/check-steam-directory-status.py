#!/usr/bin/env python3
"""Test the production CM directory response-status gate without a connection."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
runner = r'''
import Foundation
func check(_ value: Bool, _ message: String) {
    guard value else { fatalError(message) }; print("PASS: " + message)
}
check(SteamServiceMethod.getContentServers.rawValue == "ContentServerDirectory.GetServersForSteamPipe#1",
      "directory service routes to the official CM endpoint")
for result: Int32 in [0, 2, 15, 84, -1] {
    check(!SteamServiceMethod.getContentServers.acceptsServiceResult(result),
          "non-success CM result cannot become successful empty authority: \(result)")
}
check(SteamServiceMethod.getContentServers.acceptsServiceResult(1),
      "successful CM result can return an explicitly empty directory")
check(SteamServiceMethod.getManifestRequestCode.acceptsServiceResult(15) &&
      SteamServiceMethod.getCDNAuthToken.acceptsServiceResult(0),
      "unrelated services keep their existing result handling")
'''
with tempfile.TemporaryDirectory(prefix="madeira-directory-status-") as temp:
    work = Path(temp)
    code = work / "checks.swift"
    code.write_text((ROOT / "app/Madeira/SwiftSteam/Core/SteamProtocol.swift").read_text() + runner)
    executable = work / "check"
    subprocess.run(["xcrun", "swiftc", "-j", "1", str(code), "-o", str(executable)],
                   check=True, timeout=60)
    subprocess.run([str(executable)], check=True, timeout=10)
