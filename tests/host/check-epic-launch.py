#!/usr/bin/env python3
"""Production Epic argument parsing and streaming credential redaction."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'app/Build/Products/IPAInspection/epic-launch-tests'


class EpicLaunchTests(unittest.TestCase):
    def test_failed_native_start_stops_only_its_prepared_server(self):
        source = (ROOT / 'app/Madeira/ContentView.swift').read_text()
        start = source.index('            if !wineStarted {')
        failed_start = source[start:source.index('\n            // Step 4:', start)]
        start = source.index('            if let externalContext {', source.index('            winios_phase("wine-start")'))
        external_recheck = source[start:source.index('            do { try epicLaunch?.claimForRuntime() }', start)]
        start = source.index('        guard wine_process_is_running() == 0, wineserver_is_running() == 0 else {',
                             source.index('    private func runWineFullSequence('))
        existing_session = source[start:source.index('        let preparation: UUID', start)]
        harness = r'''
import Foundation
import Combine
var wineRunning: Int32 = 0, serverRunning: Int32 = 0, stopCalls = 0
func wine_process_is_running() -> Int32 { wineRunning }
func wineserver_is_running() -> Int32 { serverRunning }
func wineserver_stop() { stopCalls += 1; serverRunning = 0 }
@MainActor final class LibraryModel {
 static let shared = LibraryModel(); var current: UUID?
 func pauseOptionalWork(_ value: Bool) {}
 func launchFailed(_ reason: String) { current = nil; GameSessionWork.shared.reconcile() }
}
@MainActor final class SteamOwnedLibrary {
 static let shared = SteamOwnedLibrary(); var hasInFlightDownload = false; var hasActiveCloudSaveOperation = false
 func sessionChanged(active: Bool) {}
}
@MainActor final class EpicLibraryModel {
 static let shared = EpicLibraryModel(); var hasInFlightDownload = false
 func sessionChanged(active: Bool) {}
}
enum TestFailure: Error { case rejected }
enum Level { case error }
final class Log { var uiPaused = true; func log(_ value: String, level: Level) {} }
struct Profile { var workingDirectory: String? }
final class ExternalContext {
 func validate() throws -> Int { throw TestFailure.rejected }
 func validateWorkingDirectory(_ value: String?) throws {}
 func finish(started: Bool) {}
}
var ws_log_quiet = 1
@MainActor final class Runner {
 let logStore = Log()
 let heartbeat = Timer(timeInterval: 10, repeats: false) { _ in }
 let preparation: UUID
 let epicLaunch: EpicPreparedLaunch?
 init(_ preparation: UUID, _ launch: EpicPreparedLaunch? = nil) { self.preparation = preparation; epicLaunch = launch }
 func nativeResult(_ wineStarted: Bool) {
  let logStore = logStore, heartbeat = heartbeat, preparation = preparation, epicLaunch = epicLaunch
__FAILED_START__
 }
 func externalRecheck() {
  let logStore = logStore, heartbeat = heartbeat, preparation = preparation
  let externalContext: ExternalContext? = ExternalContext(), profile: Profile? = nil
__EXTERNAL_RECHECK__
 }
 func existingSession() {
  let externalContext: ExternalContext? = nil, profile: Profile? = nil, suppliedPreparation: UUID? = nil
  let epicLaunch = epicLaunch
__EXISTING_SESSION__
 }
}
@main struct Main {
 @MainActor static func main() async throws {
  func check(_ condition: Bool, _ message: String = "launch branch assertion failed") throws {
   guard condition else { throw NSError(domain: message, code: 1) }
  }
  let fm = FileManager.default
  let root = fm.temporaryDirectory.appendingPathComponent("madeira-ownership-start-" + UUID().uuidString)
  defer { try? fm.removeItem(at: root) }
  let docs = root.appendingPathComponent("Documents")
  try fm.createDirectory(at: docs, withIntermediateDirectories: true)
  let file = try EpicOwnershipTokenFile(data: Data("synthetic".utf8), documents: docs, trustedContainer: root)
  let path = docs.appendingPathComponent("wine/drive_c/" + file.windowsPath.dropFirst(3).replacingOccurrences(of: "\\", with: "/"))
  let launch = EpicPreparedLaunch(identity: UUID(), arguments: [], secret: "synthetic", ownership: file)
  let prep = try GameSessionWork.shared.beginLaunchPreparation()
  LibraryModel.shared.current = UUID(); serverRunning = 1
  try launch.claimForRuntime()
  Runner(prep, launch).nativeResult(false)
  try await Task.sleep(nanoseconds: 100_000_000)
  try check(serverRunning == 0 && stopCalls == 1 && !fm.fileExists(atPath: path.path), "failed start must stop its server and token")
  try check(LibraryModel.shared.current == nil && !GameSessionWork.shared.ownsPreparation(prep))
  try check(!GameSessionWork.shared.pausesBackgroundWork, "failed start must release coordinator")
  let retry = try GameSessionWork.shared.beginLaunchPreparation()
  LibraryModel.shared.current = UUID(); serverRunning = 1
  Runner(retry).externalRecheck()
  try await Task.sleep(nanoseconds: 100_000_000)
  try check(serverRunning == 0 && stopCalls == 2 && !GameSessionWork.shared.pausesBackgroundWork, "external path failure must release its idle server")
  let successful = try GameSessionWork.shared.beginLaunchPreparation()
  LibraryModel.shared.current = UUID(); serverRunning = 1; wineRunning = 1
  Runner(successful).nativeResult(true)
  try check(serverRunning == 1 && wineRunning == 1 && stopCalls == 2, "successful native start never stops server")
  wineRunning = 0; serverRunning = 0; LibraryModel.shared.current = nil
  GameSessionWork.shared.completePreparation(successful)
  serverRunning = 1
  Runner(UUID()).existingSession()
  try check(serverRunning == 1 && stopCalls == 2, "pre-existing unrelated server must not be stopped")
  serverRunning = 0; GameSessionWork.shared.reconcile()
  print("PASS actual ContentView failure/success/existing-session branches with real session coordinator and token lifetime")
 }
}
'''.replace('__FAILED_START__', failed_start).replace('__EXTERNAL_RECHECK__', external_recheck).replace('__EXISTING_SESSION__', existing_session)
        self.compile_run([str(ROOT / f'app/Madeira/{name}.swift') for name in
                          ('EpicDirectory', 'EpicRecords', 'EpicLaunch', 'GameSessionWork')], harness, 'swift')

    def test_windows_arguments_and_reserved_credentials(self):
        production = ROOT / 'app/Madeira/EpicLaunch.swift'
        self.assertTrue(production.exists(), 'Epic launch argument rules are missing')
        harness = r'''
import Foundation
@main struct Main {
 static func main() throws {
  func check(_ actual: [String], _ expected: [String]) {
    precondition(actual == expected, "Windows argument mismatch")
  }
  check(try EpicLaunchArguments.tokens("--name \"hello world\" \"\" tail"), ["--name", "hello world", "", "tail"])
  check(try EpicLaunchArguments.tokens(#""C:\games\foo\\" \"quoted\""#), [#"C:\games\foo\"#, "\"quoted\""])
  let args = try EpicLaunchArguments.authenticated(command: "-windowed", userArguments: "--size 800",
    exchangeCode: "fixture-0123456789abcdef", appName: "game-a", accountID: "account-a", displayName: "A B")
  precondition(args.prefix(3) == ["-windowed", "--size", "800"])
  precondition(args.contains("-AUTH_PASSWORD=fixture-0123456789abcdef"))
  precondition(args.contains("-epicusername=A B"))
  for input in ["-AUTH_PASSWORD=x", "-auth_type=foo", "-EpicUserId other", "-epicovt=C:\\stolen.ovt"] {
    do { _ = try EpicLaunchArguments.authenticated(command: "", userArguments: input,
      exchangeCode: "fixture-0123456789abcdef", appName: "game-a", accountID: "account-a", displayName: "A")
      fatalError("Reserved credential override accepted")
    } catch {}
  }
  do { _ = try EpicLaunchArguments.tokens("\"unterminated"); fatalError("Unbalanced quote accepted") } catch {}
  do { _ = try EpicLaunchArguments.tokens("bad\u{0}arg"); fatalError("NUL accepted") } catch {}
  EpicLaunchLogPrivacy.shared.set("fixture-0123456789abcdef")
  precondition(EpicLaunchLogPrivacy.shared.redact("token=fixture-0123456789abcdef") == "token=[redacted]")
  precondition(EpicLaunchLogPrivacy.shared.redact("[steam] normal arguments") == "[steam] normal arguments")
  precondition(!EpicLaunchLogPrivacy.shared.redact("-AUTH_PASSWORD=fixture-012345\n").contains("fixture-012345"))
  print("PASS Epic arguments")
 }
}
'''
        self.compile_run([str(ROOT / 'app/Madeira/EpicDirectory.swift'), str(ROOT / 'app/Madeira/EpicRecords.swift'), str(production)], harness, 'swift')

    def test_ownership_token_lifetime_and_confinement(self):
        harness = r'''
import Foundation
import Darwin
@main struct Main {
 static func main() throws {
  func check(_ condition: @autoclosure () throws -> Bool, _ message: String = "ownership condition failed", line: UInt = #line) throws {
   if try !condition() { throw NSError(domain: message, code: Int(line)) }
  }
  let fm = FileManager.default
  // POSIX modes and inode identity require APFS; only this owned fixture is removed.
  let root = fm.temporaryDirectory.appendingPathComponent("madeira-epic-ownership-" + UUID().uuidString)
  try fm.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? fm.removeItem(at: root) }
  let docs = root.appendingPathComponent("Documents")
  try fm.createDirectory(at: docs, withIntermediateDirectories: true)
  let payload = Data([0, 255, 128, 42, 13, 10])
  func token() throws -> EpicOwnershipTokenFile { try EpicOwnershipTokenFile(data: payload, documents: docs, trustedContainer: root) }
  func url(_ file: EpicOwnershipTokenFile) -> URL {
   docs.appendingPathComponent("wine/drive_c/" + file.windowsPath.dropFirst(3).replacingOccurrences(of: "\\", with: "/"))
  }
  func rejected(_ body: () throws -> Void) throws {
   var rejected = false; do { try body() } catch { rejected = true }; try check(rejected, "Unsafe ownership operation accepted")
  }
  let file = try token(), path = url(file)
  try check(try Data(contentsOf: path) == payload)
  try check(try path.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup == true, "token excluded from backup")
  var info = stat(); try check(lstat(path.path, &info) == 0 && info.st_mode & 0o777 == 0o600 && info.st_nlink == 1)
  try file.validate()
  let args = try EpicLaunchArguments.authenticated(command: "", userArguments: "", exchangeCode: "synthetic_exchange_1234", appName: "app", accountID: "owner", displayName: "Player", ownershipTokenPath: file.windowsPath)
  try check(args.contains("-epicovt=" + file.windowsPath))
  let prepared = EpicPreparedLaunch(identity: UUID(), arguments: args, secret: "synthetic", ownership: file)
  try prepared.claimForRuntime(); prepared.cancelBeforeStart()
  try check(fm.fileExists(atPath: path.path), "logout must preserve claimed runtime token")
  prepared.finish(); try check(!fm.fileExists(atPath: path.path), "runtime finish removes token")
  try rejected { try prepared.claimForRuntime() }
  for _ in 0..<20 {
   let racing = try token(), racePath = url(racing)
   let launch = EpicPreparedLaunch(identity: UUID(), arguments: [], secret: "synthetic", ownership: racing)
   DispatchQueue.concurrentPerform(iterations: 2) { i in
    if i == 0 { try? launch.claimForRuntime() } else { launch.cancelBeforeStart() }
   }
   try check(fm.fileExists(atPath: racePath.path) == launch.isClaimed, "atomic claim versus logout cleanup")
   launch.finish(); try check(!fm.fileExists(atPath: racePath.path))
  }
  let cancelled = try token(), cancelledPath = url(cancelled)
  let pending = EpicPreparedLaunch(identity: UUID(), arguments: [], secret: "synthetic", ownership: cancelled)
  pending.cancelBeforeStart(); try check(!fm.fileExists(atPath: cancelledPath.path)); try rejected { try pending.claimForRuntime() }
  let stale = try token(), stalePath = url(stale)
  let kept = stalePath.deletingLastPathComponent().appendingPathComponent("keep.txt")
  try Data("KEEP".utf8).write(to: kept)
  let link = kept.deletingLastPathComponent().appendingPathComponent(UUID().uuidString.lowercased() + ".ovt")
  try check(symlink(kept.path, link.path) == 0)
  try EpicOwnershipTokenFile.removeStale(documents: docs, trustedContainer: root)
  try check(!fm.fileExists(atPath: stalePath.path) && (try Data(contentsOf: kept)) == Data("KEEP".utf8))
  try check(lstat(link.path, &info) == 0 && info.st_mode & S_IFMT == S_IFLNK, "stale cleanup preserves links")
  let replaced = try token(), replacedPath = url(replaced)
  try fm.removeItem(at: replacedPath); try check(symlink(kept.path, replacedPath.path) == 0)
  try rejected { try replaced.validate() }; replaced.remove()
  try check(lstat(replacedPath.path, &info) == 0 && info.st_mode & S_IFMT == S_IFLNK, "cleanup preserves replacement inode")
  try check(try Data(contentsOf: kept) == Data("KEEP".utf8))
  let linkedDocs = root.appendingPathComponent("LinkedDocuments")
  try fm.createDirectory(at: linkedDocs, withIntermediateDirectories: true)
  try check(symlink(docs.appendingPathComponent("wine").path, linkedDocs.appendingPathComponent("wine").path) == 0)
  try rejected { _ = try EpicOwnershipTokenFile(data: payload, documents: linkedDocs, trustedContainer: root) }
  for invalid in [Data(), Data(repeating: 0, count: 65537)] {
   try rejected { _ = try EpicOwnershipTokenFile(data: invalid, documents: docs, trustedContainer: root) }
  }
  print("PASS ownership file payload, confinement, replacement, stale cleanup and atomic runtime lifetime")
 }
}
'''
        self.compile_run([str(ROOT / f'app/Madeira/{name}.swift') for name in
                          ('EpicDirectory', 'EpicRecords', 'EpicLaunch')], harness, 'swift')

    def test_redaction_at_every_stream_boundary(self):
        production = ROOT / 'app/Madeira/EpicLogFilter.h'
        self.assertTrue(production.exists(), 'Epic streaming redaction is missing')
        harness = r'''
#include "EpicLogFilter.h"
#include <assert.h>
#include <string.h>
#include <unistd.h>
int main(void) {
 const char *secret="fixture-0123456789abcdef";
 const char *input="before -AUTH_PASSWORD=fixture-0123456789abcdef after\nfixture-0123456789abcdef\n";
 for (size_t split=0;split<=strlen(input);split++) {
  int fds[2]; assert(pipe(fds)==0);
  MadeiraEpicRedactor state; assert(madeira_epic_redactor_init(&state,secret));
  assert(madeira_epic_redactor_write(&state,fds[1],input,split,0));
  assert(madeira_epic_redactor_write(&state,fds[1],input+split,strlen(input)-split,1));
  close(fds[1]); char result[256]={0}; ssize_t n=read(fds[0],result,sizeof(result)-1); close(fds[0]);
  assert(n>0); assert(!strstr(result,secret));
  assert(!strcmp(result,"before -AUTH_PASSWORD=[redacted] after\n[redacted]\n"));
 }
 {
  int fds[2]; assert(pipe(fds)==0);
  MadeiraEpicRedactor state; assert(madeira_epic_redactor_init(&state,secret));
  const char *partial="tail -AUTH_PASSWORD=fixture-0123456789\n";
  assert(madeira_epic_redactor_write(&state,fds[1],partial,strlen(partial),1));
  close(fds[1]); char result[256]={0}; assert(read(fds[0],result,255)>0); close(fds[0]);
  assert(!strstr(result,"fixture-0123456789"));
 }
 puts("PASS Epic streaming redaction");
}
'''
        self.compile_run(['-I', str(production.parent)], harness, 'c')

    def test_native_launch_binding_and_log_pipe_lifecycle(self):
        bridge = (ROOT / 'app/Madeira/WineProcessBridge.m').read_text()
        production = bridge[bridge.index('static pthread_mutex_t g_epic_launch_lock'):bridge.index('/* Session exit report')]
        harness = r'''
#import <Foundation/Foundation.h>
#include <pthread.h>
#include <poll.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/resource.h>
#include <assert.h>
#include "EpicLogFilter.h"
static volatile int g_wine_running=0;
''' + production + r'''
int main(int argc,char **argv) {
 assert(argc==2);
 @autoreleasepool {
  const char *secret="fixture-0123456789abcdef";
  assert(wine_epic_prepare_launch("[\"hello world\",\"\"]",secret,"C:\\game.exe")==0);
  assert(g_epic_launch_arguments.count==2 && [g_epic_launch_arguments[0] isEqualToString:@"hello world"]);
  assert([g_epic_launch_exe isEqualToString:@"C:\\game.exe"]);
  wine_epic_clear_launch(); assert(!g_epic_launch_arguments && !g_epic_launch_secret && !g_epic_launch_exe);
  assert(wine_epic_prepare_launch("[\"\\u0000\"]",secret,"C:\\game.exe")==-1);
  int originalOut=dup(1),originalErr=dup(2);
  NSString *path=[NSString stringWithFormat:@"%s/lifecycle.log",argv[1]];
  assert(madeira_epic_redirect_log(path.UTF8String,@"fixture-0123456789abcdef"));
  const char *line="-AUTH_PASSWORD = fixture-0123456789\nfixture-0123456789abcdef\n";
  assert(write(2,line,strlen(line))==(ssize_t)strlen(line));
  madeira_epic_end_log();
  assert(g_epic_log_restore==-1 && g_epic_log_stop==-1 && !g_epic_log_group);
  dup2(originalOut,1); dup2(originalErr,2);
  NSString *text=[NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
  assert([text containsString:@"[redacted]"] && ![text containsString:@"fixture-0123456789"]);
  // Force a real sink-write error without filling a disk. The reader must
  // continue draining; otherwise the large write blocks or delivers SIGPIPE.
  struct rlimit saved,limit; assert(getrlimit(RLIMIT_FSIZE,&saved)==0); limit=saved; limit.rlim_cur=0;
  signal(SIGXFSZ,SIG_IGN); signal(SIGPIPE,SIG_DFL);
  NSString *failure=[NSString stringWithFormat:@"%s/failure.log",argv[1]];
  assert(madeira_epic_redirect_log(failure.UTF8String,@"fixture-0123456789abcdef"));
  assert(setrlimit(RLIMIT_FSIZE,&limit)==0);
  char block[4096]; memset(block,'X',sizeof(block)); block[sizeof(block)-1]='\n';
  for(int i=0;i<64;i++) assert(write(2,block,sizeof(block))==sizeof(block));
  madeira_epic_end_log(); assert(setrlimit(RLIMIT_FSIZE,&saved)==0);
  dup2(originalOut,1); dup2(originalErr,2); close(originalOut); close(originalErr);
  puts("PASS Native Epic launch and sink-error drain");
 }
}
'''
        self.compile_run(['-I', str(ROOT / 'app/Madeira')], harness, 'objc')

    def compile_run(self, inputs, harness, language):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='scratch-', dir=ARTIFACTS) as scratch:
            scratch = Path(scratch)
            source = scratch / ('main.swift' if language == 'swift' else 'main.c')
            source.write_text(harness)
            binary = scratch / 'test'
            compiler = ['xcrun', 'swiftc', '-j', '1', '-parse-as-library'] if language == 'swift' else ['xcrun', 'clang', '-Wall', '-Wextra', '-Werror']
            if language == 'objc':
                compiler += ['-x', 'objective-c', '-fobjc-arc', '-framework', 'Foundation']
            result = subprocess.run(compiler + inputs + [str(source), '-o', str(binary)], capture_output=True, text=True, timeout=90)
            (ARTIFACTS / (language + '-compile.txt')).write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary), str(scratch)], capture_output=True, text=True, timeout=20)
            (ARTIFACTS / (language + '-run.txt')).write_text(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
