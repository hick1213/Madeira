#!/usr/bin/env python3
"""Verify actual shallow Steam partial discovery in owned temporary fixtures."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "app/Madeira/SteamOwnedLibrary.swift").read_text()
start = source.index("enum SteamIncompleteDownloads {")
end = source.index("\n// MARK: - Model", start)
production = source[start:end]


def declaration(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f"Unclosed declaration: {marker}")


folder_validation = declaration((ROOT / "app/Madeira/MadeiraDock.swift").read_text(),
                                "    static func validFolderName(")
download_value = declaration(source, "    struct Download: Equatable {")
restore_method = declaration(source, "    private func restoreIncompleteDownloads()")
progress_value = declaration((ROOT / "app/Madeira/SwiftSteam/Content/DepotDownloader.swift").read_text(),
                             "struct SteamDownloadProgress: Equatable, Sendable {")
model = r'''
struct SteamOwnedGame { let id: Int; var buildID: Int }
final class DownloadRowsModel {
    static var steamApps = URL(fileURLWithPath: "/unused-download-rows-fixture")
    var signedIn = true
    var owned: [SteamOwnedGame]
    var downloads: [Int: Download] = [:]
    var queue: [Int] = []
    var active: (id: Int, task: Task<Void, Never>)?
    init(_ apps: URL, build: Int) {
        Self.steamApps = apps
        owned = [SteamOwnedGame(id: 1086940, buildID: build)]
    }
    func restore() { restoreIncompleteDownloads() }
__DOWNLOAD_VALUE__
__RESTORE_METHOD__
}
'''.replace('__DOWNLOAD_VALUE__', download_value).replace('__RESTORE_METHOD__', restore_method)
runner = r'''
import Foundation
func check(_ value: Bool, _ message: String) {
    guard value else { fatalError(message) }; print("PASS: " + message)
}
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let fm = FileManager.default
let journals = root.appendingPathComponent("downloading")
for id in ["42", "43", "44", "999", "-1", "042", "4294967296"] {
    let path = journals.appendingPathComponent(id)
    try fm.createDirectory(at: path, withIntermediateDirectories: true)
    if id != "43" { try Data("owned temporary journal".utf8).write(to: path.appendingPathComponent("one.journal")) }
}
try Data("not a journal".utf8).write(to: journals.appendingPathComponent("43/readme"))
try fm.createSymbolicLink(at: journals.appendingPathComponent("91"), withDestinationURL: journals.appendingPathComponent("999"))
try fm.createDirectory(at: journals.appendingPathComponent("92"), withIntermediateDirectories: true)
try fm.createSymbolicLink(at: journals.appendingPathComponent("92/link.journal"), withDestinationURL: journals.appendingPathComponent("999/one.journal"))
let owned: Set<Int> = [42, 43, 44, 91, 92, -1, 4294967296]
check(SteamIncompleteDownloads.discover(steamApps: root, owned: owned) == [42, 44],
      "unfinished owned journals restore as rows; empty, foreign and invalid IDs do not")
try fm.removeItem(at: journals.appendingPathComponent("42"))
check(SteamIncompleteDownloads.discover(steamApps: root, owned: owned) == [44],
      "successful cleanup removes the finished row on rediscovery")
check(SteamIncompleteDownloads.discover(steamApps: root, owned: []) == [],
      "another or signed-out account never sees the former account rows")
check(try Data(contentsOf: journals.appendingPathComponent("999/one.journal")) == Data("owned temporary journal".utf8),
      "discovery does not remove or modify other task data")
check(SteamIncompleteDownloads.discover(steamApps: root.appendingPathComponent("missing"), owned: owned) == [],
      "absent downloading directory stays absent")
check(!fm.fileExists(atPath: root.appendingPathComponent("missing").path), "discovery never creates directories")

// A completed game may retain an empty journal for its already-installed depot.
// Such a file alone is not evidence that another game download is paused.
let appID = 1086940
let currentGID = "8364979414431994193"
let currentJournal = "depot_1086941_8364979414431994193.journal"
func completeRecord(flags: String = "4", build: String = "1000", target: String? = "1000",
                    download: String = "0", stage: String = "0", downloaded: String = "0", staged: String = "0", folder: String = "Fixture Game",
                    app: String = "1086940") -> Data {
    let targetLine = target.map { "\"TargetBuildID\" \"\($0)\"" } ?? ""
    return Data("""
    "AppState"
    {
        "appid" "\(app)"
        "name" "Fixture Game"
        "StateFlags" "\(flags)"
        "installdir" "\(folder)"
        "buildid" "\(build)"
        "BytesToDownload" "\(download)"
        "BytesToStage" "\(stage)"
        "BytesDownloaded" "\(downloaded)"
        "BytesStaged" "\(staged)"
        \(targetLine)
        "InstalledDepots"
        {
            "1086941" { "manifest" "\(currentGID)" "size" "200" }
            "1086942" { "manifest" "12345" "size" "100" }
        }
    }
    """.utf8)
}
func fixture(_ name: String, record: Data? = completeRecord(), physicalFolder: Bool = true,
             journalName: String = "depot_1086941_8364979414431994193.journal", bytes: Data = Data()) throws -> URL {
    let apps = root.appendingPathComponent("regressions/" + name)
    let partial = apps.appendingPathComponent("downloading/1086940")
    try fm.createDirectory(at: partial, withIntermediateDirectories: true)
    try bytes.write(to: partial.appendingPathComponent(journalName))
    if let record { try record.write(to: apps.appendingPathComponent("appmanifest_1086940.acf")) }
    if physicalFolder { try fm.createDirectory(at: apps.appendingPathComponent("common/Fixture Game"), withIntermediateDirectories: true) }
    return apps
}
func expect(_ apps: URL, restored: Bool, currentBuilds: [Int: Int] = [1086940: 1000], _ message: String) {
    check(SteamIncompleteDownloads.discover(steamApps: apps, owned: [appID], currentBuilds: currentBuilds) == (restored ? [appID] : []), message)
}
let completed = try fixture("completed")
expect(completed, restored: false, "complete installed record plus matching empty depot journal does not restore a download row")
let completedJournal = completed.appendingPathComponent("downloading/1086940/" + currentJournal)
check(try Data(contentsOf: completedJournal).isEmpty && Data(contentsOf: completed.appendingPathComponent("appmanifest_1086940.acf")) == completeRecord(),
      "completed-journal detection preserves the journal and install record unchanged")
let multiple = try fixture("multiple-completed")
try Data().write(to: multiple.appendingPathComponent("downloading/1086940/depot_1086942_12345.journal"))
expect(multiple, restored: false, "all empty depot journals matching the complete installed record are harmless")
expect(try fixture("completed-without-target", record: completeRecord(target: nil)), restored: false,
       "a complete installed record may omit TargetBuildID")
expect(try fixture("first-install", record: nil, physicalFolder: false), restored: true,
       "an empty journal without a completed install record still restores a first install")
expect(try fixture("unknown-current-build"), restored: true, currentBuilds: [:],
       "a completed old record cannot dismiss an empty journal without known current build metadata")
expect(try fixture("newer-current-build"), restored: true, currentBuilds: [1086940: 1001],
       "a same-GID empty journal remains an update when the current Steam build is newer")
expect(try fixture("different-current-build"), restored: true, currentBuilds: [1086940: 999],
       "different current build metadata cannot certify that this empty journal is completed")
expect(try fixture("updated-depot", journalName: "depot_1086941_8364979414431994194.journal"), restored: true,
       "a different depot manifest retains an update row even when its journal is empty")
expect(try fixture("nonempty-current", bytes: Data([0])), restored: true,
       "any nonempty matching depot journal retains the download row")
expect(try fixture("unknown-journal", journalName: "unknown.journal"), restored: true,
       "an empty journal with an unrecognized name is not dismissed as completed")
let mixed = try fixture("mixed-current-and-update")
try Data().write(to: mixed.appendingPathComponent("downloading/1086940/depot_1086941_8364979414431994194.journal"))
expect(mixed, restored: true, "one matching empty journal cannot hide a different update journal")
let mixedNonempty = try fixture("mixed-nonempty")
try Data("unfinished".utf8).write(to: mixedNonempty.appendingPathComponent("downloading/1086940/depot_1086942_12345.journal"))
expect(mixedNonempty, restored: true, "one matching empty journal cannot hide another nonempty depot journal")
for (name, record) in [
    ("pending-download", completeRecord(download: "1")),
    ("pending-stage", completeRecord(stage: "1")),
    ("downloaded-bytes", completeRecord(downloaded: "1")),
    ("staged-bytes", completeRecord(staged: "1")),
    ("different-target", completeRecord(target: "1001")),
    ("zero-build", completeRecord(build: "0", target: "0")),
    ("flags-not-installed", completeRecord(flags: "0")),
    ("flags-additional-state", completeRecord(flags: "5")),
    ("wrong-app-id", completeRecord(app: "1086941")),
    ("invalid-folder", completeRecord(folder: "../outside")),
    ("malformed-record", Data("\"AppState\" { \"StateFlags\" \"4\"".utf8))
] {
    expect(try fixture(name, record: record), restored: true,
           "\(name) cannot certify a completed download from an empty journal")
}
expect(try fixture("absent-folder", physicalFolder: false), restored: true,
       "an installed record without its physical game folder retains the download row")
let fileFolder = try fixture("file-as-folder", physicalFolder: false)
try fm.createDirectory(at: fileFolder.appendingPathComponent("common"), withIntermediateDirectories: true)
try Data("not a directory".utf8).write(to: fileFolder.appendingPathComponent("common/Fixture Game"))
expect(fileFolder, restored: true, "a regular file cannot stand in for the installed game folder")
let linkedFolder = try fixture("linked-folder", physicalFolder: false)
try fm.createDirectory(at: linkedFolder.appendingPathComponent("common"), withIntermediateDirectories: true)
try fm.createSymbolicLink(at: linkedFolder.appendingPathComponent("common/Fixture Game"),
                          withDestinationURL: completed.appendingPathComponent("common/Fixture Game"))
expect(linkedFolder, restored: true, "a symbolic-link game folder cannot certify completed installation")
let linkedCommon = try fixture("linked-common", physicalFolder: false)
try fm.createSymbolicLink(at: linkedCommon.appendingPathComponent("common"), withDestinationURL: completed.appendingPathComponent("common"))
expect(linkedCommon, restored: true, "a symbolic-link common ancestor cannot certify completed installation")
let linkedManifest = try fixture("linked-manifest", record: nil)
try fm.createSymbolicLink(at: linkedManifest.appendingPathComponent("appmanifest_1086940.acf"),
                          withDestinationURL: completed.appendingPathComponent("appmanifest_1086940.acf"))
expect(linkedManifest, restored: true, "a symbolic-link manifest cannot dismiss an empty journal")
check(try Data(contentsOf: completedJournal).isEmpty && Data(contentsOf: completed.appendingPathComponent("appmanifest_1086940.acf")) == completeRecord(),
      "all shallow discovery remains read-only for completed records and link targets")

// Disk recovery runs once using cached metadata and again after Steam refreshes
// the build. The existing in-memory row must converge with the fresh evidence.
let recovered = DownloadRowsModel(completed, build: 0)
recovered.restore()
check(recovered.downloads[appID]?.state == .paused,
      "unknown cached build metadata conservatively restores the paused download row")
recovered.owned[0].buildID = 1000
recovered.restore()
check(recovered.downloads[appID] == nil,
      "fresh matching current build removes the previously recovered empty-journal row")
let newer = DownloadRowsModel(completed, build: 0)
newer.restore()
newer.owned[0].buildID = 1001
newer.restore()
check(newer.downloads[appID]?.state == .paused,
      "fresh newer current build preserves the recovered update row")
// Rows created by user download actions are not merely disk-recovery hints.
for state in [DownloadRowsModel.Download.State.paused, .queued, .active] {
    let manual = DownloadRowsModel(completed, build: 1000)
    var item = DownloadRowsModel.Download(state: state)
    item.progress.networkBytes = 17
    manual.downloads[appID] = item
    if state == .queued { manual.queue = [appID] }
    if state == .active { manual.active = (appID, Task {}) }
    manual.restore()
    check(manual.downloads[appID] == item,
          "fresh metadata preserves a manually created \(state) download and its progress")
}
// Recovery provenance alone must not erase a row that became queued or active.
let queuedRecovery = DownloadRowsModel(completed, build: 0)
queuedRecovery.restore()
queuedRecovery.queue = [appID]
queuedRecovery.owned[0].buildID = 1000
queuedRecovery.restore()
check(queuedRecovery.downloads[appID]?.state == .paused,
      "a recovered row in the live queue is preserved while queue work owns it")
let activeRecovery = DownloadRowsModel(completed, build: 0)
activeRecovery.restore()
activeRecovery.active = (appID, Task {})
activeRecovery.owned[0].buildID = 1000
activeRecovery.restore()
check(activeRecovery.downloads[appID]?.state == .paused,
      "a recovered row with live active ownership is preserved")
let changedState = DownloadRowsModel(completed, build: 0)
changedState.restore()
changedState.downloads[appID]?.state = .queued
changedState.owned[0].buildID = 1000
changedState.restore()
check(changedState.downloads[appID]?.state == .queued,
      "a recovered row whose state advanced beyond paused is preserved")
let unavailableApps = try fixture("temporarily-unavailable-journals", record: nil, physicalFolder: false,
                                  bytes: Data("unfinished first install".utf8))
let unavailable = DownloadRowsModel(unavailableApps, build: 1000)
unavailable.restore()
check(unavailable.downloads[appID]?.state == .paused,
      "a first install with nonempty journal is recovered before the I/O-unavailable check")
let rowBeforeUnavailable = unavailable.downloads[appID]
let downloadingPath = unavailableApps.appendingPathComponent("downloading")
let heldDownloadingPath = unavailableApps.appendingPathComponent("held-downloading")
try fm.moveItem(at: downloadingPath, to: heldDownloadingPath)
unavailable.restore()
let rowWhileUnavailable = unavailable.downloads[appID]
try fm.moveItem(at: heldDownloadingPath, to: downloadingPath)
check(rowWhileUnavailable == rowBeforeUnavailable,
      "temporarily missing downloading directory is unknown evidence and preserves the recovered unfinished row")
check(try Data(contentsOf: completedJournal).isEmpty && Data(contentsOf: completed.appendingPathComponent("appmanifest_1086940.acf")) == completeRecord(),
      "in-memory row convergence never deletes or modifies the journal or installed record")
'''
with tempfile.TemporaryDirectory(prefix="madeira-download-rows-") as temp:
    folder = Path(temp)
    code = folder / "main.swift"
    code.write_text("import Foundation\nimport Darwin\n" + production + "\nenum MadeiraDock {\n" + folder_validation + "\n}\n" + progress_value + "\n" + model + "\n" + runner)
    executable = folder / "check"
    subprocess.run(["xcrun", "swiftc", "-j", "1", str(ROOT / "app/Madeira/SteamKeyValues.swift"), str(code), "-o", str(executable)],
                   check=True, timeout=60)
    subprocess.run([str(executable), str(folder / "SteamApps")], check=True, timeout=10)
