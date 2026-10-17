#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the production setup queue's account, foreground and session boundaries."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class InstallSetupTests(unittest.TestCase):
    def test_deferred_setup_boundaries(self):
        source = (ROOT / 'app/Madeira/SteamOwnedLibrary.swift').read_text()
        queue = source.split('struct SteamInstallSetupQueue:', 1)[1].split(
            "// The account's owned Steam games", 1)[0]
        harness = r'''
import Foundation
@main struct Main {
 static func main() throws {
  func check(_ value: @autoclosure () -> Bool, _ why: String) throws {
   if !value() { throw NSError(domain: why, code: 1) }
  }
  let a = String(repeating: "a", count: 64), b = String(repeating: "b", count: 64)
  let one = SteamInstallSetupQueue.Request(appID: 10, entryID: UUID(), account: a)
  let two = SteamInstallSetupQueue.Request(appID: 20, entryID: UUID(), account: b)
  let entries = [one.entryID: one.appID, two.entryID: two.appID]
  var q = SteamInstallSetupQueue()
  q.enqueue(one); q.enqueue(one); q.enqueue(two)
  try check(q.requests.count == 2, "queue stores each account/game request once")
  q = try JSONDecoder().decode(SteamInstallSetupQueue.self, from: JSONEncoder().encode(q))
  try check(q.take(account: a, entries: entries, foreground: false, idle: true, sessionAvailable: true) == nil, "background cannot start setup")
  try check(q.take(account: a, entries: entries, foreground: true, idle: false, sessionAvailable: true) == nil, "download or native work must drain")
  try check(q.take(account: a, entries: entries, foreground: true, idle: true, sessionAvailable: false) == nil, "consumed session defers until restart")
  try check(q.requests.count == 2, "deferral keeps requests across restarts")
  try check(q.take(account: a, entries: entries, foreground: true, idle: true, sessionAvailable: true) == one, "only the active account starts")
  try check(q.take(account: a, entries: entries, foreground: true, idle: true, sessionAvailable: true) == nil, "setup is consumed once and another account waits")
  var changed = entries; changed[two.entryID] = 999
  try check(q.take(account: b, entries: changed, foreground: true, idle: true, sessionAvailable: true) == nil, "changed entry cannot start a different game")
  try check(q.requests.isEmpty, "stale entry request is removed")
  q.enqueue(one)
  try check(q.take(account: a, entries: [:], foreground: true, idle: true, sessionAvailable: true) == nil, "uninstalled game cannot start setup")
  q.enqueue(.init(appID: -1, entryID: UUID(), account: a))
  q.enqueue(.init(appID: 1, entryID: UUID(), account: "not-an-account-hash"))
  try check(q.requests.isEmpty, "invalid persisted request is rejected")
  q.enqueue(one); q.enqueue(two); q.cancel(appID: one.appID, account: b)
  try check(q.requests.count == 2, "cancel cannot clear another account's request")
  q.cancel(appID: one.appID, account: a)
  try check(q.requests == [two], "opting out or cancelling removes only the requested setup")
  let gate = SteamInstallSetupQueue.DeferralGate()
  try check(gate.permits(foreground: true, ownsRequest: true), "foreground owner is permitted")
  try check(!gate.permits(foreground: false, ownsRequest: true), "background defers setup")
  try check(gate.permits(foreground: true, ownsRequest: true) && gate.deferred,
            "returning foreground during cleanup cannot erase the deferred request")
  let switched = SteamInstallSetupQueue.DeferralGate()
  try check(!switched.permits(foreground: true, ownsRequest: false), "account change or opt-out invalidates launch")
  try check(switched.permits(foreground: true, ownsRequest: true) && switched.deferred,
            "account restoration during cleanup cannot erase deferral")
  print("PASS account-bound setup queue, persistence, foreground and session deferral")
 }
}
'''
        with tempfile.TemporaryDirectory(prefix='madeira-install-setup-') as scratch:
            scratch = Path(scratch)
            swift = scratch / 'queue.swift'
            swift.write_text('import Foundation\nstruct SteamInstallSetupQueue:' + queue + harness)
            binary = scratch / 'test'
            build = subprocess.run(['xcrun', 'swiftc', '-j', '1', '-parse-as-library',
                                    str(swift), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
