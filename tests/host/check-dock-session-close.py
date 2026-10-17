#!/usr/bin/env python3
"""Execute production Dock completion/generation policy in Swift, without UI mocks."""
from pathlib import Path
import shutil
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError('Run without optimization; regression assertions are required.')
ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'app/Madeira/MadeiraDock.swift').read_text()
start = source.index('struct DockSessionCloseGate {')
brace = source.index('{', start)
end, depth = brace + 1, 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
policy = source[start:end]
parser = source[source.index('    struct Report {'):source.index('    @MainActor private static var lastReport =')]
checks = r'''
let id = UUID(), other = UUID()
var gate = DockSessionCloseGate(sessionID: id, baselineToken: 3)
let report = MadeiraDock.parseReport(Data("[steam-host] ml1970 launch-game-running=1\n[steam-host] ml1970 launch-game-ended=1\n[steam-host] ml1830 shutdown-complete=1\n[steam-host] ml1830 probe-result=0\n".utf8))
let complete = report.fields
assert(complete.count == 4)
assert(gate.request(reportSessionID: other, fields: complete, controlToken: 4, wineRunning: true) == nil)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 3, wineRunning: true) == nil)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 4, wineRunning: false) == nil)
assert(gate.request(reportSessionID: id, fields: ["launch-game-running":"1"], controlToken: 4, wineRunning: true) == nil)
var missing = complete; missing.removeValue(forKey:"shutdown-complete")
assert(gate.request(reportSessionID: id, fields: missing, controlToken: 4, wineRunning: true) == nil)
missing = complete; missing.removeValue(forKey:"probe-result")
assert(gate.request(reportSessionID: id, fields: missing, controlToken: 4, wineRunning: true) == nil)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 5, wineRunning: true) == nil)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 4, wineRunning: true) == 4)
// A native enqueue refusal is retryable. Mark sent only after acceptance.
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 4, wineRunning: true) == 4)
gate.queued(token: 5)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 4, wineRunning: true) == 4)
gate.queued(token: 4)
assert(gate.request(reportSessionID: id, fields: complete, controlToken: 4, wineRunning: true) == nil)
var next = DockSessionCloseGate(sessionID: other, baselineToken: 4)
assert(next.request(reportSessionID: id, fields: complete, controlToken: 5, wineRunning: true) == nil)
assert(next.request(reportSessionID: other, fields: complete, controlToken: 5, wineRunning: true) == 5)
print("PASS: Dock completion requires current report/native generation, complete numeric signals and accepted once-only enqueue")
'''
with tempfile.TemporaryDirectory(prefix='madeira-dock-close-') as td:
    main = Path(td) / 'main.swift'
    binary = Path(td) / 'check'
    main.write_text('import Foundation\n' + policy + '\nenum MadeiraDock {\n' + parser + '\n}\n' + checks)
    subprocess.run([shutil.which('swiftc') or 'swiftc', str(main), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
