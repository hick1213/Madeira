#!/usr/bin/env python3
"""Run production Foundation core and LibraryEntry against owned APFS fixtures.

Security scope accounting is injected; bookmarks and filesystem operations are
real. This does not establish physical USB or iOS Files authorization.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'app/Build/Products/IPAInspection/external-game-library-build17'

def block(text, header):
    start = text.index(header)
    pos = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[pos] == '{') - (text[pos] == '}')
        pos += 1
    return text[start:pos]

STUBS = '''
import Foundation
enum DisplayMode: String { case fit }
enum ResolutionChoices { static let defaultValue = "1280x720" }
struct TouchControl: Codable { }
enum ControlAction: Codable { case none }
enum LibraryError: LocalizedError { case message(String) }
enum ProMotionIntent { static func supportedMode(_ mode: Int) -> Int32 { Int32(mode) } }
enum GamepadInput { static let keyboardMouseAvailable = false }
enum MadeiraConfig {
 static func get(_ key: String) -> String? { nil }
 static func applyGame(_ text: String?) throws -> [String:String] { [:] }
}
enum SyncEngine { case fastsync; static let current = SyncEngine.fastsync }
final class LogStore { static let shared = LogStore(); func log(_ text: String) {} }
enum GuestDisplay { static func configureSessionDefault(view: CGSize, knob: String) {} }
func madeira_set_vsync_locked(_ mode: Int32) {}
'''

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    run = OUT / 'host-runs' / run_id
    run.mkdir(parents=True)
    names = ('host-report.json', 'host-compile.txt', 'host-tests.txt')
    summary = {'run_id': run_id, 'run_directory': str(run.relative_to(OUT)),
               'status': 'pending', 'success': False, 'exit': None,
               'stage': 'setup', 'assertions': 0, 'physicalUSB': 'untested', 'sources': {}}

    def save_summary():
        data = json.dumps(summary, indent=2) + '\n'
        (run / 'host-report.json').write_text(data)
        pending = OUT / ('.host-report-' + run_id + '.json')
        pending.write_text(data)
        pending.replace(OUT / 'host-report.json')

    def save_log(name, value):
        text = f'Run {run_id} ({summary["stage"]})\n' + value
        (run / name).write_text(text)
        (OUT / name).write_text(text)

    try:
        snapshot = run / 'previous'
        snapshot.mkdir()
        for name in names:
            previous = OUT / name
            if previous.exists():
                previous.replace(snapshot / name)
        save_summary()  # Invalidate the previous PASS before source/compiler work.
        for name in ('host-compile.txt', 'host-tests.txt'):
            save_log(name, 'PENDING: see current host-report.json for this run.\n')
        core = ROOT / 'app/Madeira/ExternalGameLibrary.swift'
        sources = [core, ROOT / 'app/Madeira/Library.swift', Path(__file__).with_suffix('.swift'), Path(__file__).resolve(), Path(__file__).with_name('test-external-game-library-races.swift')]
        inputs = {path: path.read_bytes() for path in sources}
        summary['sources'] = {str(path.relative_to(ROOT)): hashlib.sha256(data).hexdigest() for path, data in inputs.items()}
        with tempfile.TemporaryDirectory(prefix='madeira-external-library-', dir='/private/tmp') as folder:
            temp = Path(folder)
            source = temp / 'main.swift'
            library = inputs[sources[1]].decode()
            saves = '''final class EntrySaveHarness {
              var readOnly = false; var entries: [LibraryEntry] = []; var error: String?
              func persist(_ next: [LibraryEntry]) { entries = next }
            ''' + block(library, '    func save(_ entry: LibraryEntry)') + '\n}\n'
            source.write_text(STUBS + inputs[core].decode() + '\n' + block(library, 'struct LibraryEntry: Codable, Identifiable') + '\n' + saves + inputs[sources[4]].decode() + '\n' + inputs[sources[2]].decode())
            binary = temp / 'tests'
            summary['stage'] = 'compile'
            save_summary()
            compiled = subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', str(source), '-o', str(binary)], text=True, capture_output=True, timeout=90)
            save_log('host-compile.txt', compiled.stdout + compiled.stderr)
            summary['compile_exit'] = compiled.returncode
            if compiled.returncode:
                raise RuntimeError(f'Swift compiler exited {compiled.returncode}')
            fixtures = temp / 'fixtures'
            fixtures.mkdir()
            summary['stage'] = 'runtime'
            save_summary()
            tested = subprocess.run([str(binary), str(fixtures)], text=True, capture_output=True, timeout=30)
            save_log('host-tests.txt', tested.stdout + tested.stderr)
            print(tested.stdout + tested.stderr)
            summary['runtime_exit'] = tested.returncode
            summary['assertions'] = tested.stdout.count('PASS:')
            if tested.returncode:
                raise RuntimeError(f'Native tests exited {tested.returncode}')
            summary['stage'] = 'source-verification'
            if any(path.read_bytes() != data for path, data in inputs.items()):
                raise RuntimeError('Source changed during verification; rerun against final sources')
        summary.update(status='passed', success=True, exit=0, stage='complete')
    except Exception as error:
        summary.update(status='failed', success=False, exit=1,
                       error_type=type(error).__name__, error=str(error))
        failure = f'Run {run_id}: FAILED: {summary["error_type"]}: {error}\n'
        (run / 'failure.txt').write_text(failure)
        (OUT / 'host-tests.txt').write_text(failure + f'Native output, if any: {summary["run_directory"]}/host-tests.txt\n')
        print(f'FAIL: {error}')
    finally:
        save_summary()
    return summary['exit']

if __name__ == '__main__':
    raise SystemExit(main())
