// Deterministic syscall-boundary scheduling only. The production mount code,
// real directory FDs, Files-like competing writes and final Darwin syscalls run.
var beforeMountPublish: ((Int32, String) throws -> Void)?
var beforeMountSwap: ((Int32, String, String) throws -> Void)?
func performHook(_ operation: () throws -> Void) {
    do { try operation() } catch { print("INTERPOSITION ERROR: \(error)"); exit(2) }
}
func symlinkat(_ target: String, _ fd: Int32, _ name: String) -> Int32 {
    if !name.hasPrefix("."), let hook = beforeMountPublish {
        beforeMountPublish = nil; performHook { try hook(fd, name) }
    }
    return target.withCString { value in name.withCString { Darwin.symlinkat(value, fd, $0) } }
}
func renameatx_np(_ sourceFD: Int32, _ source: String, _ targetFD: Int32, _ target: String, _ flags: UInt32) -> Int32 {
    if flags == UInt32(RENAME_SWAP), let hook = beforeMountSwap {
        beforeMountSwap = nil; performHook { try hook(targetFD, source, target) }
    }
    return source.withCString { value in target.withCString { Darwin.renameatx_np(sourceFD, value, targetFD, $0, flags) } }
}
// Keep the old unsafe implementation executable for the initial RED proof.
func rename(_ source: String, _ target: String) -> Int32 {
    if let hook = beforeMountPublish {
        beforeMountPublish = nil
        let parent = URL(fileURLWithPath: target).deletingLastPathComponent()
        let fd = open(parent.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard fd >= 0 else { exit(2) }; defer { close(fd) }
        performHook { try hook(fd, URL(fileURLWithPath: target).lastPathComponent) }
    }
    return source.withCString { value in target.withCString { Darwin.rename(value, $0) } }
}
func fdWrite(_ fd: Int32, name: String, text: String) throws {
    let file = openat(fd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600))
    guard file >= 0 else { throw ExternalGameLibrary.Failure.collision }; defer { close(file) }
    let bytes = Array(text.utf8)
    let count = bytes.withUnsafeBytes { Darwin.write(file, $0.baseAddress, $0.count) }
    guard count == bytes.count else { throw ExternalGameLibrary.Failure.collision }
}
func testMountRaces(fixture: URL) throws {
    let fm = FileManager.default
    let usb = fixture.appendingPathComponent("race-usb", isDirectory: true)
    let replacement = fixture.appendingPathComponent("race-replacement", isDirectory: true)
    for folder in [usb, replacement] {
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        try Data("@echo off".utf8).write(to: folder.appendingPathComponent("game.cmd"))
    }
    func newStore(_ name: String) throws -> (ExternalGameLibrary, ExternalGameRoot, URL, URL) {
        let drive = fixture.appendingPathComponent(name + "/drive_c", isDirectory: true)
        try fm.createDirectory(at: drive, withIntermediateDirectories: true)
        let config = fixture.appendingPathComponent(name + ".json")
        let store = ExternalGameLibrary(configuration: config, drive: drive, startScope: { _ in true }, stopScope: { _ in })
        return (store, try store.register(usb), drive, config)
    }
    let (fileStore, fileRoot, fileDrive, fileConfig) = try newStore("fresh-file-race")
    let originalFileConfig = try Data(contentsOf: fileConfig)
    let fileMount = fileDrive.appendingPathComponent("madeira-external/" + fileRoot.id.uuidString)
    beforeMountPublish = { fd, name in try fdWrite(fd, name: name, text: "INTERVENING FILE") }
    rejects("fresh mount rejects intervening regular file at actual syscall boundary") { _ = try fileStore.prepare(fileRoot.id, relative: "game.cmd") }
    expect(beforeMountPublish == nil && (try? Data(contentsOf: fileMount)) == Data("INTERVENING FILE".utf8), "fresh mount never overwrites raced-in file bytes")
    let originalFileAuthorization = try ExternalGameLibrary.decodeRoots(originalFileConfig).first!
    let failedFileIntent = try ExternalGameLibrary.decodeRoots(Data(contentsOf: fileConfig)).first!
    expect(failedFileIntent.id == originalFileAuthorization.id && failedFileIntent.bookmark == originalFileAuthorization.bookmark && failedFileIntent.ownedMountTarget == usb.resolvingSymlinksInPath().path, "failed fresh file publication preserves root authorization and recorded intent")

    let (parentStore, parentRoot, parentDrive, parentConfig) = try newStore("fresh-parent-race")
    let originalParentConfig = try Data(contentsOf: parentConfig)
    let parent = parentDrive.appendingPathComponent("madeira-external", isDirectory: true)
    let movedParent = parentDrive.appendingPathComponent("preserved-parent", isDirectory: true)
    let outside = fixture.appendingPathComponent("fresh-outside", isDirectory: true)
    try fm.createDirectory(at: outside, withIntermediateDirectories: true)
    let outsideFile = outside.appendingPathComponent(parentRoot.id.uuidString)
    try Data("OUTSIDE ORIGINAL".utf8).write(to: outsideFile)
    beforeMountPublish = { _, _ in
        try fm.moveItem(at: parent, to: movedParent)
        try fm.createSymbolicLink(at: parent, withDestinationURL: outside)
        // The legacy rename source must exist at the redirected path to expose
        // its real overwrite. The new exclusive symlinkat has no temp source.
        let iterator = fm.enumerator(at: movedParent, includingPropertiesForKeys: nil, options: .skipsSubdirectoryDescendants)!
        while let file = iterator.nextObject() as? URL {
            if file.lastPathComponent.hasPrefix(".mount-") {
                try fm.moveItem(at: file, to: outside.appendingPathComponent(file.lastPathComponent))
            }
        }
    }
    rejects("fresh mount rejects parent replaced by outside symlink at syscall boundary") { _ = try parentStore.prepare(parentRoot.id, relative: "game.cmd") }
    expect(beforeMountPublish == nil && (try? Data(contentsOf: outsideFile)) == Data("OUTSIDE ORIGINAL".utf8), "FD-bound publication preserves outside-drive file")
    let originalParentAuthorization = try ExternalGameLibrary.decodeRoots(originalParentConfig).first!
    let failedParentIntent = try ExternalGameLibrary.decodeRoots(Data(contentsOf: parentConfig)).first!
    expect(failedParentIntent.id == originalParentAuthorization.id && failedParentIntent.bookmark == originalParentAuthorization.bookmark && failedParentIntent.ownedMountTarget == usb.resolvingSymlinksInPath().path, "failed parent publication preserves root authorization and recorded intent")

    let (swapStore, swapRoot, swapDrive, swapConfig) = try newStore("relink-file-race")
    let prepared = try swapStore.prepare(swapRoot.id, relative: "game.cmd"); prepared.finish(started: false)
    let originalSwapConfig = try Data(contentsOf: swapConfig)
    let swapMount = swapDrive.appendingPathComponent("madeira-external/" + swapRoot.id.uuidString)
    beforeMountSwap = { fd, _, name in
        let incoming = ".incoming-" + UUID().uuidString
        try fdWrite(fd, name: incoming, text: "RELINK INTERVENING FILE")
        guard renameat(fd, incoming, fd, name) == 0 else { throw ExternalGameLibrary.Failure.collision }
    }
    rejects("relink detects swapped-out unknown file before config commit") { _ = try swapStore.register(replacement, relinking: swapRoot.id, requiredPaths: ["game.cmd"]) }
    expect(beforeMountSwap == nil && (try? Data(contentsOf: swapMount)) == Data("RELINK INTERVENING FILE".utf8), "relink restores intervening file without deleting or overwriting it")
    expect(try Data(contentsOf: swapConfig) == originalSwapConfig && swapStore.roots.first?.bookmark == swapRoot.bookmark, "failed raced relink preserves previous bookmark and config bytes")

    let (swapParentStore, swapParentRoot, swapParentDrive, swapParentConfig) = try newStore("relink-parent-race")
    let swapParentPrepared = try swapParentStore.prepare(swapParentRoot.id, relative: "game.cmd"); swapParentPrepared.finish(started: false)
    let originalSwapParentConfig = try Data(contentsOf: swapParentConfig)
    let swapParent = swapParentDrive.appendingPathComponent("madeira-external", isDirectory: true)
    let swapMovedParent = swapParentDrive.appendingPathComponent("preserved-parent", isDirectory: true)
    let swapOutside = fixture.appendingPathComponent("relink-outside", isDirectory: true)
    try fm.createDirectory(at: swapOutside, withIntermediateDirectories: true)
    let swapOutsideFile = swapOutside.appendingPathComponent(swapParentRoot.id.uuidString)
    try Data("RELINK OUTSIDE ORIGINAL".utf8).write(to: swapOutsideFile)
    beforeMountSwap = { _, _, _ in
        try fm.moveItem(at: swapParent, to: swapMovedParent)
        try fm.createSymbolicLink(at: swapParent, withDestinationURL: swapOutside)
    }
    rejects("relink rejects replaced parent after FD-bound atomic exchange") { _ = try swapParentStore.register(replacement, relinking: swapParentRoot.id, requiredPaths: ["game.cmd"]) }
    expect(beforeMountSwap == nil && (try? Data(contentsOf: swapOutsideFile)) == Data("RELINK OUTSIDE ORIGINAL".utf8), "relink never changes outside-drive file through replaced parent")
    expect(try Data(contentsOf: swapParentConfig) == originalSwapParentConfig, "failed relink parent race preserves root config")
    expect((try? fm.destinationOfSymbolicLink(atPath: swapMovedParent.appendingPathComponent(swapParentRoot.id.uuidString).path)) == usb.resolvingSymlinksInPath().path, "FD rollback restores old owned mount in original directory")

    let (reuseStore, reuseRoot, reuseDrive, _) = try newStore("owned-reuse")
    let firstContext = try reuseStore.prepare(reuseRoot.id, relative: "game.cmd"); firstContext.finish(started: false)
    let reuseMount = reuseDrive.appendingPathComponent("madeira-external/" + reuseRoot.id.uuidString)
    var firstIdentity = stat(), reusedIdentity = stat()
    expect(lstat(reuseMount.path, &firstIdentity) == 0, "normal fresh mount exists as actual symlink")
    beforeMountPublish = { _, _ in throw ExternalGameLibrary.Failure.collision }
    beforeMountSwap = { _, _, _ in throw ExternalGameLibrary.Failure.collision }
    let reused = try reuseStore.prepare(reuseRoot.id, relative: "game.cmd"); reused.finish(started: false)
    expect(lstat(reuseMount.path, &reusedIdentity) == 0 && firstIdentity.st_ino == reusedIdentity.st_ino && beforeMountPublish != nil && beforeMountSwap != nil, "same owned target reuses inode with no creation or exchange")
    beforeMountPublish = nil; beforeMountSwap = nil

    let (recoveryStore, recoveryRoot, recoveryDrive, recoveryConfig) = try newStore("rollback-second-writer")
    let recoveryPrepared = try recoveryStore.prepare(recoveryRoot.id, relative: "game.cmd"); recoveryPrepared.finish(started: false)
    let originalRecoveryConfig = try Data(contentsOf: recoveryConfig)
    let recoveryParent = recoveryDrive.appendingPathComponent("madeira-external", isDirectory: true)
    beforeMountSwap = { fd, _, name in
        let incoming = ".incoming-" + UUID().uuidString
        try fdWrite(fd, name: incoming, text: "FIRST RACED FILE")
        guard renameat(fd, incoming, fd, name) == 0 else { throw ExternalGameLibrary.Failure.collision }
        beforeMountSwap = { rollbackFD, _, rollbackName in
            let second = ".incoming-" + UUID().uuidString
            try fdWrite(rollbackFD, name: second, text: "SECOND RACED FILE")
            guard renameat(rollbackFD, second, rollbackFD, rollbackName) == 0 else { throw ExternalGameLibrary.Failure.collision }
        }
    }
    rejects("relink rejects both publication and rollback competing writers") { _ = try recoveryStore.register(replacement, relinking: recoveryRoot.id, requiredPaths: ["game.cmd"]) }
    var preservedBytes = Set<Data>()
    let recoveryIterator = fm.enumerator(at: recoveryParent, includingPropertiesForKeys: [.isRegularFileKey], options: .skipsSubdirectoryDescendants)!
    while let file = recoveryIterator.nextObject() as? URL {
        if (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true, let data = try? Data(contentsOf: file) { preservedBytes.insert(data) }
    }
    expect(beforeMountSwap == nil && preservedBytes.contains(Data("FIRST RACED FILE".utf8)) && preservedBytes.contains(Data("SECOND RACED FILE".utf8)), "rollback retains both unknown file byte sequences and never blindly cleans recovery slot")
    expect(try Data(contentsOf: recoveryConfig) == originalRecoveryConfig, "multiple-writer relink keeps prior root configuration")

    let failingDirectory = fixture.appendingPathComponent("commit-directory", isDirectory: true)
    let preservedDirectory = fixture.appendingPathComponent("preserved-commit-directory", isDirectory: true)
    let failingDrive = fixture.appendingPathComponent("commit-failure/drive_c", isDirectory: true)
    try fm.createDirectory(at: failingDirectory, withIntermediateDirectories: true)
    try fm.createDirectory(at: failingDrive, withIntermediateDirectories: true)
    let failingConfig = failingDirectory.appendingPathComponent("roots.json")
    let failingStore = ExternalGameLibrary(configuration: failingConfig, drive: failingDrive, startScope: { _ in true }, stopScope: { _ in })
    let failingRoot = try failingStore.register(usb)
    let failingPrepared = try failingStore.prepare(failingRoot.id, relative: "game.cmd"); failingPrepared.finish(started: false)
    let beforeFailedCommit = try Data(contentsOf: failingConfig)
    try fm.moveItem(at: failingDirectory, to: preservedDirectory)
    try Data("CONFIG PARENT OCCUPIED".utf8).write(to: failingDirectory)
    rejects("actual configuration write failure rolls back relink mount") { _ = try failingStore.register(replacement, relinking: failingRoot.id, requiredPaths: ["game.cmd"]) }
    let failingMount = failingDrive.appendingPathComponent("madeira-external/" + failingRoot.id.uuidString)
    expect((try? fm.destinationOfSymbolicLink(atPath: failingMount.path)) == usb.resolvingSymlinksInPath().path && failingStore.roots.first?.bookmark == failingRoot.bookmark, "failed commit restores old owned target and old registered bookmark")
    expect(try Data(contentsOf: preservedDirectory.appendingPathComponent("roots.json")) == beforeFailedCommit && Data(contentsOf: failingDirectory) == Data("CONFIG PARENT OCCUPIED".utf8), "failed relink commit preserves prior config bytes and competing parent file")

    let freshDirectory = fixture.appendingPathComponent("fresh-commit-directory", isDirectory: true)
    let preservedFreshDirectory = fixture.appendingPathComponent("preserved-fresh-commit-directory", isDirectory: true)
    let freshDrive = fixture.appendingPathComponent("fresh-commit-failure/drive_c", isDirectory: true)
    try fm.createDirectory(at: freshDirectory, withIntermediateDirectories: true)
    try fm.createDirectory(at: freshDrive, withIntermediateDirectories: true)
    let freshConfig = freshDirectory.appendingPathComponent("roots.json")
    var freshStarts = 0, freshStops = 0
    let freshStore = ExternalGameLibrary(configuration: freshConfig, drive: freshDrive,
        startScope: { _ in freshStarts += 1; return true }, stopScope: { _ in freshStops += 1 })
    let freshRoot = try freshStore.register(usb)
    let originalFreshConfig = try Data(contentsOf: freshConfig)
    let freshMount = freshDrive.appendingPathComponent("madeira-external/" + freshRoot.id.uuidString)
    try fm.moveItem(at: freshDirectory, to: preservedFreshDirectory)
    try Data("FRESH CONFIG PARENT BLOCKED".utf8).write(to: freshDirectory)
    rejects("fresh launch with actual config write failure refuses publication") { _ = try freshStore.prepare(freshRoot.id, relative: "game.cmd") }
    var absentMount = stat()
    expect(lstat(freshMount.path, &absentMount) != 0 && errno == ENOENT, "failed fresh intent save creates no unowned link")
    expect(freshStarts == freshStops && freshStore.roots.first?.ownedMountTarget == nil && freshStore.roots.first?.bookmark == freshRoot.bookmark, "failed fresh intent releases scope and preserves registered authorization")
    expect(try Data(contentsOf: preservedFreshDirectory.appendingPathComponent("roots.json")) == originalFreshConfig, "failed fresh intent preserves prior config bytes")
    // Restore only this test's exact owned blocked-file and moved directory.
    expect(try Data(contentsOf: freshDirectory) == Data("FRESH CONFIG PARENT BLOCKED".utf8), "owned blocked config-parent fixture verified before recovery")
    try fm.removeItem(at: freshDirectory)
    try fm.moveItem(at: preservedFreshDirectory, to: freshDirectory)
    let coldRecovered = ExternalGameLibrary(configuration: freshConfig, drive: freshDrive, startScope: { _ in true }, stopScope: { _ in })
    do {
        let recovered = try coldRecovered.prepare(freshRoot.id, relative: "game.cmd")
        expect(try recovered.validate().path == usb.appendingPathComponent("game.cmd").resolvingSymlinksInPath().path, "cold first launch recovers after config write access returns")
        recovered.finish(started: false)
    } catch { expect(false, "cold first launch recovers after config write access returns") }
    do {
        let reselected = try coldRecovered.register(usb, relinking: freshRoot.id, requiredPaths: ["game.cmd"])
        let reselectedContext = try coldRecovered.prepare(freshRoot.id, relative: "game.cmd")
        let reselectedFile = try reselectedContext.validate()
        expect(reselected.id == freshRoot.id && reselectedFile.path == usb.appendingPathComponent("game.cmd").resolvingSymlinksInPath().path, "same-root reselection and launch recover without UUID change")
        reselectedContext.finish(started: false)
    } catch { expect(false, "same-root reselection and launch recover without UUID change") }

    let (noAliasStore, noAliasRoot, noAliasDrive, _) = try newStore("relink-absent-alias")
    let noAliasFirst = try noAliasStore.prepare(noAliasRoot.id, relative: "game.cmd"); noAliasFirst.finish(started: false)
    let noAliasMount = noAliasDrive.appendingPathComponent("madeira-external/" + noAliasRoot.id.uuidString)
    // Simulate removing the exact app-created link in this owned fixture.
    try fm.removeItem(at: noAliasMount)
    let noAliasRelinked = try noAliasStore.register(replacement, relinking: noAliasRoot.id, requiredPaths: ["game.cmd"])
    var stillAbsent = stat()
    expect(noAliasRelinked.id == noAliasRoot.id && lstat(noAliasMount.path, &stillAbsent) != 0 && errno == ENOENT, "relink with absent old alias commits bookmark and defers publication to Play")
    let noAliasPrepared = try noAliasStore.prepare(noAliasRoot.id, relative: "game.cmd")
    expect(try noAliasPrepared.validate().path == replacement.appendingPathComponent("game.cmd").resolvingSymlinksInPath().path, "deferred fresh mount uses newly registered bookmark authorization")
    noAliasPrepared.finish(started: false)
}
