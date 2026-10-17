import Foundation
setbuf(stdout, nil)

var failed = 0
func expect(_ ok: Bool, _ text: String) { print((ok ? "PASS: " : "FAIL: ") + text); if !ok { failed += 1 } }
func rejects(_ text: String, _ operation: () throws -> Void) {
    do { try operation(); expect(false, text) } catch { expect(true, text) }
}
let fm = FileManager.default
let fixture = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let drive = fixture.appendingPathComponent("drive_c", isDirectory: true)
let usb = fixture.appendingPathComponent("usb", isDirectory: true)
let replacement = fixture.appendingPathComponent("replacement", isDirectory: true)
let sibling = fixture.appendingPathComponent("usb-sibling", isDirectory: true)
for folder in [drive, usb, replacement, sibling] { try fm.createDirectory(at: folder, withIntermediateDirectories: true) }
for folder in [usb, replacement, sibling] { try Data("@echo off".utf8).write(to: folder.appendingPathComponent("game.cmd")) }
var starts = 0, stops = 0
let config = fixture.appendingPathComponent("roots.json")
let store = ExternalGameLibrary(configuration: config, drive: drive, startScope: { _ in starts += 1; return true }, stopScope: { _ in stops += 1 })
let root = try store.register(usb)
expect(starts == stops, "registration releases scope")
let stable = ExternalGameLibrary.windowsPath(root: root.id, relative: "game.cmd")
expect(stable.hasPrefix("C:\\madeira-external\\"), "stable C: alias")
for path in ["", "/game.cmd", "../game.cmd", "a/../game.cmd", "a//game.cmd", "a\\game.cmd", "C:/game.cmd", "a\0.cmd", "a\n.cmd", "a\".cmd", "bad./game.cmd", "bad /game.cmd", "CON.cmd", "x:stream.cmd"] {
    rejects("reject ambiguous path \(path.debugDescription)") { try ExternalGameLibrary.validateRelative(path) }
}
try fm.createSymbolicLink(at: usb.appendingPathComponent("escape"), withDestinationURL: sibling)
rejects("reject real symlink escape") { _ = try ExternalGameLibrary.resolve("escape/game.cmd", inside: usb, directory: false) }
rejects("reject sibling prefix") { _ = try ExternalGameLibrary.relative(sibling.appendingPathComponent("game.cmd"), inside: usb) }
let mount = drive.appendingPathComponent("madeira-external/" + root.id.uuidString)
try fm.createDirectory(at: mount, withIntermediateDirectories: true)
rejects("mount preserves existing directory") { _ = try store.prepare(root.id, relative: "game.cmd") }
expect((try? mount.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true, "collision directory survives")
try fm.removeItem(at: mount)
try Data("occupied".utf8).write(to: mount)
rejects("mount preserves existing regular file") { _ = try store.prepare(root.id, relative: "game.cmd") }
expect(try Data(contentsOf: mount) == Data("occupied".utf8), "collision file bytes survive")
try fm.removeItem(at: mount)
try fm.createSymbolicLink(at: mount, withDestinationURL: sibling)
rejects("mount preserves unknown symlink") { _ = try store.prepare(root.id, relative: "game.cmd") }
expect((try? fm.destinationOfSymbolicLink(atPath: mount.path)) == sibling.path, "unknown symlink survives")
try fm.removeItem(at: mount)
do {
    let context = try store.prepare(root.id, relative: "game.cmd")
    expect(try context.validate().path == usb.appendingPathComponent("game.cmd").resolvingSymlinksInPath().path, "preflight resolves actual external file")
    context.finish(started: false)
    expect(starts == stops, "failed preflight/native start releases scope")
    rejects("released context cannot be reused") { _ = try context.validate() }
}
rejects("busy preparation refuses relink") { _ = try store.register(replacement, relinking: root.id, requiredPaths: ["game.cmd"], busy: true) }
rejects("relink missing executable preserves root") { _ = try store.register(replacement, relinking: root.id, requiredPaths: ["missing.exe"]) }
let relinked = try store.register(replacement, relinking: root.id, requiredPaths: ["game.cmd"])
expect(relinked.id == root.id && stable == ExternalGameLibrary.windowsPath(root: relinked.id, relative: "game.cmd"), "relink preserves root and shortcut identity")
do {
    let context = try store.prepare(root.id, relative: "game.cmd")
    expect(try fm.destinationOfSymbolicLink(atPath: mount.path) == replacement.resolvingSymlinksInPath().path, "owned mount atomically follows relink")
    context.finish(started: true)
    context.finish(started: false)
}
expect(starts == stops + 1, "actual start retains scope despite context destruction")
rejects("retained successful scope refuses relink until restart") { _ = try store.register(usb, relinking: root.id) }
let cold = ExternalGameLibrary(configuration: config, drive: drive, startScope: { _ in true }, stopScope: { _ in })
expect(cold.roots.first?.id == root.id, "cold bookmark configuration preserves ID")
let coldContext = try cold.prepare(root.id, relative: "game.cmd")
expect(try coldContext.validate().path == replacement.appendingPathComponent("game.cmd").resolvingSymlinksInPath().path, "cold start resolves bookmark and registered mount")
coldContext.finish(started: false)
for index in 0..<600 { try Data().write(to: replacement.appendingPathComponent("item\(index).cmd")) }
let browsing = try cold.acquire(root.id)
let listing = try ExternalGameLibrary.browse("", lease: browsing, limit: 32)
expect(listing.truncated && listing.items.count <= 32, "immediate browser stops iterator at cap")
browsing.release()
try fm.moveItem(at: replacement, to: fixture.appendingPathComponent("disconnected"))
// Bookmark tracking on a local APFS volume may find a moved folder. A denied
// authorization still must preserve persisted identities.
let denied = ExternalGameLibrary(configuration: config, drive: drive, startScope: { _ in false }, stopScope: { _ in })
rejects("denied authorization never uses persisted mount target") { _ = try denied.acquire(root.id) }
expect(denied.roots.first?.id == root.id, "unavailable drive preserves registered IDs")
for invalid in [Data("{}".utf8), Data(repeating: 32, count: 4 * 1024 * 1024 + 1), Data("{\"version\":2,\"roots\":[]}".utf8)] {
    rejects("bounded malformed root config rejected") { _ = try ExternalGameLibrary.decodeRoots(invalid) }
}
func configData(_ roots: [ExternalGameRoot]) throws -> Data {
    let data = try JSONEncoder().encode(roots)
    return Data("{\"version\":1,\"roots\":".utf8) + data + Data("}".utf8)
}
rejects("duplicate root IDs reject configuration") { _ = try ExternalGameLibrary.decodeRoots(configData([root, root])) }
rejects("65 roots reject bounded configuration") {
    _ = try ExternalGameLibrary.decodeRoots(configData((0..<65).map { _ in var copy = root; copy.id = UUID(); return copy }))
}
var malformedBookmark = root; malformedBookmark.bookmark = Data(repeating: 1, count: 64 * 1024 + 1)
rejects("oversized bookmark rejected before resolution") { _ = try ExternalGameLibrary.decodeRoots(configData([malformedBookmark])) }
let brokenConfig = fixture.appendingPathComponent("broken.json")
let originalBroken = Data("{\"broken\":true}".utf8); try originalBroken.write(to: brokenConfig)
let broken = ExternalGameLibrary(configuration: brokenConfig, drive: drive, startScope: { _ in true }, stopScope: { _ in })
rejects("malformed saved roots cannot be silently overwritten") { _ = try broken.register(usb) }
expect(try Data(contentsOf: brokenConfig) == originalBroken, "malformed saved configuration bytes preserved")
let parentDrive = fixture.appendingPathComponent("parent-collision/drive_c", isDirectory: true)
try fm.createDirectory(at: parentDrive, withIntermediateDirectories: true)
let parentStore = ExternalGameLibrary(configuration: fixture.appendingPathComponent("parent-roots.json"), drive: parentDrive, startScope: { _ in true }, stopScope: { _ in })
let parentRoot = try parentStore.register(usb)
let unsafeParent = parentDrive.appendingPathComponent("madeira-external", isDirectory: true)
try fm.createSymbolicLink(at: unsafeParent, withDestinationURL: sibling)
rejects("symlink mount parent is never followed") { _ = try parentStore.prepare(parentRoot.id, relative: "game.cmd") }
expect((try? fm.destinationOfSymbolicLink(atPath: unsafeParent.path)) == sibling.path, "mount parent symlink preserved")
var deferred: (() -> Void)?
let deferredStore = ExternalGameLibrary(configuration: fixture.appendingPathComponent("deferred-roots.json"), drive: drive, startScope: { _ in starts += 1; return true }, stopScope: { _ in stops += 1 })
let deferredRoot = try deferredStore.register(usb)
let deferredStarts = starts, deferredStops = stops
do {
    let context = try deferredStore.prepare(deferredRoot.id, relative: "game.cmd")
    deferred = { _ = try? context.validate() }
}
expect(starts == deferredStarts + 1 && stops == deferredStops, "JIT deferral keeps preparation scope alive")
deferred = nil
expect(stops == deferredStops + 1, "cancelled JIT closure releases unsuccessful preparation scope")
var old = LibraryEntry(title: "Internal", relativePath: "Games/test.cmd", bits: 0)
var external = old; external.externalLibraryID = root.id
expect(old.windowsPath == "C:\\Games\\test.cmd", "internal Windows identity unchanged")
expect(external.windowsPath == ExternalGameLibrary.windowsPath(root: root.id, relative: old.relativePath), "external Windows identity maps stable root")
expect(external.launchWindowsPath == external.windowsPath, "external launch and shortcut identities agree")
external.launchMode = "desktop"
expect(external.launchArguments.contains(external.windowsPath), "desktop command contains C: external alias")
external.startServices = true
expect(external.servicesScript?.contains(external.windowsPath) == true && external.servicesScript?.contains("cd /d \"C:\\madeira-external\\") == true, "services/batch preserve external alias and CWD")
external.startServices = false
let encoded = try JSONEncoder().encode(old)
let decoded = try JSONDecoder().decode(LibraryEntry.self, from: encoded)
expect(decoded.externalLibraryID == nil && decoded.id == old.id, "old Codable entries remain compatible")
external.steamAppID = 123
rejects("mixed external Steam identity rejected") { try external.validate() }
external.steamAppID = nil; external.epicAppName = "Game"
rejects("mixed external Epic identity rejected") { try external.validate() }
let savedEntries = EntrySaveHarness()
savedEntries.save(old)
var externalA = old; externalA.id = UUID(); externalA.externalLibraryID = root.id
var externalB = externalA; externalB.id = UUID(); externalB.externalLibraryID = UUID()
savedEntries.save(externalA); savedEntries.save(externalB)
expect(savedEntries.entries.count == 3 && Set(savedEntries.entries.map(\.id)).count == 3, "production save keeps same relative paths separate across internal and external roots")
var reused = externalA; reused.externalLibraryID = externalB.externalLibraryID
savedEntries.save(reused)
expect(savedEntries.entries.first { $0.id == externalA.id }?.externalLibraryID == root.id, "production save refuses root change under reused UUID")
var steamEntry = old; steamEntry.id = UUID(); steamEntry.steamAppID = 42
savedEntries.save(steamEntry)
var mixed = externalA; mixed.steamAppID = 42
savedEntries.save(mixed)
expect(savedEntries.entries.first { $0.steamAppID == 42 }?.id == steamEntry.id && savedEntries.entries.count == 4, "mixed external identity cannot upsert over existing Steam game")
print("Physical USB / iOS Files / Wine sibling assets: UNTESTED")
try testMountRaces(fixture: fixture)
exit(failed == 0 ? 0 : 1)
