import Foundation
import CryptoKit
import Darwin

/// Installer preparation only. The caller owns Wine lifetime and fresh preflight.
enum RuntimeRepair {
    struct Package: Codable, Hashable, Sendable {
        let requirement: RuntimePreflight.Requirement
        let version: RuntimePreflight.Version
        let url: URL
        let sha512: String
        let fileName: String
    }
    struct Plan: Codable, Hashable, Sendable {
        let identityHash: String
        let nonce: String
        let drivePath: String
        let directoryPath: String
        let packages: [Package]
        let driveDevice: UInt64
        let driveInode: UInt64
        let rootInode: UInt64
        let directoryInode: UInt64
        let scriptInode: UInt64
        let receiptInode: UInt64
        let installerInodes: [UInt64]
        let manifestInode: UInt64
        var scriptURL: URL { URL(fileURLWithPath: directoryPath).appendingPathComponent("repair.cmd") }
        var resultURL: URL { URL(fileURLWithPath: directoryPath).appendingPathComponent("receipt.txt") }
        var cmdScriptWindowsPath: String { "C:\\" + directoryPath.dropFirst(drivePath.count + 1).replacingOccurrences(of: "/", with: "\\") + "\\repair.cmd" }
        func installerURL(at index: Int) -> URL { URL(fileURLWithPath: directoryPath).appendingPathComponent(String(format: "%03d.exe", index)) }
    }
    enum Result: Equatable, Sendable { case completed(restartRequired: Bool), failed(index: Int, exitCode: Int), interrupted }
    struct LiveProgress: Equatable, Sendable {
        enum Stage: Equatable, Sendable { case waiting, installing(index: Int), finishing }
        struct Failure: Equatable, Sendable { let index: Int; let exitCode: Int }
        var stage: Stage = .waiting
        var completed: Int = 0
        var firstFailure: Failure?
    }
    static let metadataLimit = 16 * 1024 * 1024
    static let installerLimit = 256 * 1024 * 1024
    private static let rootName = ".madeira-runtime-repairs"
    enum FileAccessStep: String {
        case openContainer = "open-container", openDirectory = "open-directory", createDirectory = "create-directory"
        case openFile = "open-file", createFile = "create-file", inspectFile = "inspect-file"
    }
    enum RepairError: Error, LocalizedError {
        case unsupported, invalidMetadata, unsafePath, integrity, invalidReceipt, busy, transport
        case fileAccess(step: FileAccessStep, code: Int32)
        var errorDescription: String? {
            switch self {
            case .unsupported: return "No verified Microsoft installer is available for this runtime."
            case .invalidMetadata: return "Microsoft runtime metadata did not contain a verified matching installer."
            case .unsafePath: return "The runtime repair files or Wine prefix changed. Prepare a new repair."
            case .integrity: return "The runtime installer did not match Microsoft's SHA512."
            case .invalidReceipt: return "The installer result did not match this repair."
            case .busy: return "Another runtime repair is being prepared."
            case .transport: return "The Microsoft runtime download failed or exceeded its size limit."
            case let .fileAccess(step, code): return "Runtime repair file access failed (step=\(step.rawValue) errno=\(code))."
            }
        }
    }
    static func metadataURL(for requirement: RuntimePreflight.Requirement) throws -> URL {
        guard requirement.family != .framework, requirement.architecture != .unknown,
              (1...99).contains(requirement.version.major), (0...99).contains(requirement.version.minor) else { throw RepairError.unsupported }
        return URL(string: "https://builds.dotnet.microsoft.com/dotnet/release-metadata/\(requirement.version.major).\(requirement.version.minor)/releases.json")!
    }
    static func allows(_ url: URL) -> Bool {
        let hosts: Set<String> = ["builds.dotnet.microsoft.com", "download.visualstudio.microsoft.com", "dotnetcli.azureedge.net"]
        return url.scheme?.lowercased() == "https" && hosts.contains(url.host?.lowercased() ?? "")
            && (url.port == nil || url.port == 443) && url.user == nil && url.password == nil
            && url.fragment == nil && url.query == nil && !url.path.isEmpty
    }
    static func resolve(data: Data, requirement: RuntimePreflight.Requirement) throws -> Package {
        _ = try metadataURL(for: requirement)
        guard !data.isEmpty, data.count <= metadataLimit,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let releases = object["releases"] as? [[String: Any]], releases.count <= 2048 else { throw RepairError.invalidMetadata }
        let section: String, prefix: String
        switch requirement.family {
        case .netCore: section = "runtime"; prefix = "dotnet-runtime"
        case .desktop: section = "windowsdesktop"; prefix = "windowsdesktop-runtime"
        case .aspnet: section = "aspnetcore-runtime"; prefix = "aspnetcore-runtime"
        case .framework: throw RepairError.unsupported
        }
        let rid = "win-" + requirement.architecture.rawValue
        var packages: [Package] = []
        for release in releases {
            guard let runtime = release[section] as? [String: Any], let text = runtime["version"] as? String,
                  let version = RuntimePreflight.Version(text), requirement.accepts(version: version),
                  let files = runtime["files"] as? [[String: Any]], files.count <= 128 else { continue }
            let name = "\(prefix)-\(version)-\(rid).exe"
            for file in files {
                guard file["rid"] as? String == rid, let link = file["url"] as? String, link.count <= 4096,
                      let url = URL(string: link), allows(url), url.lastPathComponent == name,
                      let hash = file["hash"] as? String, validHash(hash),
                      let declaredName = file["name"] as? String,
                      declaredName == "\(prefix)-\(rid).exe" || declaredName == name else { continue }
                packages.append(Package(requirement: requirement, version: version, url: url, sha512: hash.lowercased(), fileName: name))
            }
        }
        guard let selected = packages.sorted(by: { $0.version > $1.version }).first else { throw RepairError.invalidMetadata }
        return selected
    }

    private static func validHash(_ value: String) -> Bool {
        value.utf8.count == 128 && value.utf8.allSatisfy { (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) }
    }
    private static func identityHash(_ identity: String) -> String {
        SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// Creates only exclusive, private files. A prepared plan never starts Wine.
    static func makePlan(packages: [Package], drive: URL, identity: String) throws -> Plan {
        guard !identity.isEmpty, identity.count <= 4096, (1...8).contains(packages.count),
              packages.allSatisfy({ allows($0.url) && validHash($0.sha512) && $0.fileName == $0.url.lastPathComponent }) else { throw RepairError.unsafePath }
        let canonical = URL(fileURLWithPath: try directoryPath(drive).path, isDirectory: true)
        let driveFD = try openDirectory(canonical); defer { close(driveFD) }
        let driveInfo = try info(driveFD)
        let rootFD = try privateDirectory(at: driveFD, name: rootName); defer { close(rootFD) }
        let rootInfo = try info(rootFD), nonce = UUID().uuidString.lowercased(), hash = identityHash(identity)
        let name = hash + "-" + nonce
        guard mkdirat(rootFD, name, 0o700) == 0 else { throw accessFailure(.createDirectory) }
        let directoryFD = try openDirectory(at: rootFD, name: name); defer { close(directoryFD) }
        let directoryInfo = try info(directoryFD)
        let directory = canonical.appendingPathComponent(rootName).appendingPathComponent(name)
        let windows = "C:\\\(rootName)\\\(name)"
        let script = batch(windowsDirectory: windows, nonce: nonce, count: packages.count)
        let scriptInode = try create(data: Data(script.utf8), directory: directoryFD, name: "repair.cmd")
        let receiptInode = try create(data: Data(), directory: directoryFD, name: "receipt.txt")
        let installerInodes = try packages.indices.map { try create(data: Data(), directory: directoryFD, name: String(format: "%03d.exe", $0)) }
        let manifestInode = try create(data: Data(), directory: directoryFD, name: "plan.json")
        let plan = Plan(identityHash: hash, nonce: nonce, drivePath: canonical.path, directoryPath: directory.path,
                        packages: packages, driveDevice: UInt64(driveInfo.st_dev), driveInode: UInt64(driveInfo.st_ino),
                        rootInode: UInt64(rootInfo.st_ino), directoryInode: UInt64(directoryInfo.st_ino),
                        scriptInode: scriptInode, receiptInode: receiptInode, installerInodes: installerInodes, manifestInode: manifestInode)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let manifest = try openFile(directory: directoryFD, name: "plan.json", limit: 128 * 1024, inode: manifestInode, writing: true)
        defer { close(manifest) }
        try write(encoder.encode(plan), to: manifest)
        guard fsync(manifest) == 0 else { throw RepairError.unsafePath }
        return plan
    }

    private static func batch(windowsDirectory: String, nonce: String, count: Int) -> String {
        let receipt = "\"\(windowsDirectory)\\receipt.txt\""
        var lines = ["@echo off", "setlocal EnableExtensions DisableDelayedExpansion", ">\(receipt) echo BEGIN \(nonce) \(count)",
                     #"start "" /b "C:\windows\system32\services.exe""#]
        for index in 0..<count {
            lines += [">>\(receipt) echo START \(nonce) \(index)",
                      "\"\(windowsDirectory)\\\(String(format: "%03d.exe", index))\" /install /quiet /norestart",
                      "set \"madeira_repair_exit=%errorlevel%\"", ">>\(receipt) echo EXIT \(nonce) \(index) %madeira_repair_exit%"]
        }
        lines += [">>\(receipt) echo END \(nonce) \(count)", #""C:\windows\system32\wineboot.exe" -k -s"#, "exit /b 0", ""]
        return lines.joined(separator: "\r\n")
    }

    /// Derive the installer entry with the same lexical path rules as plan binding.
    static func scriptRelativePath(plan: Plan, drive: URL) throws -> String {
        let path = try directoryPath(drive).path
        guard path == plan.drivePath, plan.identityHash.utf8.count == 64,
              plan.identityHash.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              UUID(uuidString: plan.nonce)?.uuidString.lowercased() == plan.nonce else { throw RepairError.unsafePath }
        let directory = rootName + "/" + plan.identityHash + "-" + plan.nonce
        guard plan.directoryPath == path + "/" + directory else { throw RepairError.unsafePath }
        // Use the same lexical /var alias rule as plan creation and binding.
        // File identities and installer hashes are still validated before Wine.
        return directory + "/repair.cmd"
    }

    /// Call immediately before handing this installer-only script to Wine.
    static func validate(plan: Plan, identity: String, drive: URL) throws {
        let directoryFD = try boundDirectory(plan: plan, identity: identity, drive: drive)
        defer { close(directoryFD) }
        let receipt = try readPrivate(directory: directoryFD, name: "receipt.txt", limit: 16 * 1024, inode: plan.receiptInode)
        guard receipt.isEmpty else { throw RepairError.invalidReceipt }
        for (index, package) in plan.packages.enumerated() {
            let fd = try openFile(directory: directoryFD, name: String(format: "%03d.exe", index), limit: installerLimit, inode: plan.installerInodes[index])
            defer { close(fd) }
            guard try hash(fd) == package.sha512.lowercased() else { throw RepairError.integrity }
        }
    }
    private static func boundDirectory(plan: Plan, identity: String, drive: URL) throws -> Int32 {
        guard identityHash(identity) == plan.identityHash, UUID(uuidString: plan.nonce)?.uuidString.lowercased() == plan.nonce,
              plan.packages.count > 0 && plan.packages.count <= 8,
              plan.installerInodes.count == plan.packages.count,
              try directoryPath(drive).path == plan.drivePath else { throw RepairError.unsafePath }
        let driveFD = try openDirectory(URL(fileURLWithPath: plan.drivePath)); defer { close(driveFD) }
        let driveInfo = try info(driveFD)
        guard UInt64(driveInfo.st_dev) == plan.driveDevice, UInt64(driveInfo.st_ino) == plan.driveInode else { throw RepairError.unsafePath }
        let rootFD = try privateDirectory(at: driveFD, name: rootName, create: false); defer { close(rootFD) }
        guard UInt64(try info(rootFD).st_ino) == plan.rootInode else { throw RepairError.unsafePath }
        let name = plan.identityHash + "-" + plan.nonce
        guard URL(fileURLWithPath: plan.drivePath).appendingPathComponent(rootName).appendingPathComponent(name).path == plan.directoryPath else { throw RepairError.unsafePath }
        let fd = try privateDirectory(at: rootFD, name: name, create: false)
        do {
            guard UInt64(try info(fd).st_ino) == plan.directoryInode,
                  try JSONDecoder().decode(Plan.self, from: readPrivate(directory: fd, name: "plan.json", limit: 128 * 1024, inode: plan.manifestInode)) == plan else { throw RepairError.unsafePath }
            let windows = "C:\\\(rootName)\\\(name)"
            guard try readPrivate(directory: fd, name: "repair.cmd", limit: 16 * 1024, inode: plan.scriptInode) == Data(batch(windowsDirectory: windows, nonce: plan.nonce, count: plan.packages.count).utf8) else { throw RepairError.unsafePath }
            return fd
        } catch { close(fd); throw error }
    }
    /// Read only after Wine and its server have fully stopped. Completion is not proof that a runtime is installed.
    static func result(for plan: Plan, identity: String, drive: URL) throws -> Result {
        let fd = try boundDirectory(plan: plan, identity: identity, drive: drive); defer { close(fd) }
        return try parseReceipt(readPrivate(directory: fd, name: "receipt.txt", limit: 16 * 1024, inode: plan.receiptInode), plan: plan)
    }
    /// Progress may be read while Wine runs. Even END only means the script is
    /// finishing; result and a fresh runtime scan still require the session to stop.
    static func liveProgress(plan: Plan, identity: String, drive: URL) throws -> LiveProgress {
        let fd = try boundDirectory(plan: plan, identity: identity, drive: drive); defer { close(fd) }
        return try parseLiveProgress(readPrivate(directory: fd, name: "receipt.txt", limit: 16 * 1024, inode: plan.receiptInode), plan: plan)
    }
    static func parseLiveProgress(_ data: Data, plan: Plan) throws -> LiveProgress {
        guard data.count <= 16 * 1024, (1...8).contains(plan.packages.count),
              UUID(uuidString: plan.nonce)?.uuidString.lowercased() == plan.nonce else { throw RepairError.invalidReceipt }
        // A concurrent append can stop anywhere, including between CR and LF or
        // within a UTF8 sequence. Only complete lines are eligible for parsing.
        let bytes = [UInt8](data)
        guard let newline = bytes.lastIndex(of: 10) else { return LiveProgress() }
        let complete = bytes.prefix(newline + 1)
        guard complete.allSatisfy({ $0 == 10 || $0 == 13 || (32...126).contains($0) }) else { throw RepairError.invalidReceipt }
        var lines = String(decoding: complete, as: UTF8.self).components(separatedBy: "\n")
        lines.removeLast()
        for index in lines.indices {
            if lines[index].hasSuffix("\r") { lines[index].removeLast() }
            guard !lines[index].contains("\r") else { throw RepairError.invalidReceipt }
        }
        guard lines.first == "BEGIN \(plan.nonce) \(plan.packages.count)" else { throw RepairError.invalidReceipt }
        var progress = LiveProgress(), awaitingExit = false, ended = false
        for line in lines.dropFirst() {
            guard !ended else { throw RepairError.invalidReceipt }
            let index = progress.completed
            if index == plan.packages.count {
                guard line == "END \(plan.nonce) \(plan.packages.count)" else { throw RepairError.invalidReceipt }
                progress.stage = .finishing; ended = true
            } else if !awaitingExit {
                guard line == "START \(plan.nonce) \(index)" else { throw RepairError.invalidReceipt }
                progress.stage = .installing(index: index); awaitingExit = true
            } else {
                let fields = line.components(separatedBy: " ")
                guard fields.count == 4, fields[0] == "EXIT", fields[1] == plan.nonce, fields[2] == String(index),
                      let code = UInt32(fields[3]), String(code) == fields[3] else { throw RepairError.invalidReceipt }
                if code != 0 && code != 3010 && code != 1641 && progress.firstFailure == nil {
                    progress.firstFailure = .init(index: index, exitCode: Int(code))
                }
                progress.completed += 1; awaitingExit = false
            }
        }
        return progress
    }
    /// Call only before handing the script to Wine. This cleanup deliberately ignores task cancellation.
    /// It removes no cache entries and refuses foreign files, replaced inodes and nonempty receipts.
    static func cleanupUnlaunched(plan: Plan, identity: String, drive: URL) throws {
        let directory = try boundDirectory(plan: plan, identity: identity, drive: drive)
        defer { close(directory) }
        let root = try openDirectory(URL(fileURLWithPath: plan.directoryPath).deletingLastPathComponent())
        defer { close(root) }
        guard UInt64(try info(root).st_ino) == plan.rootInode else { throw RepairError.unsafePath }
        let entries: [(String, UInt64)] = plan.packages.indices.map { (String(format: "%03d.exe", $0), plan.installerInodes[$0]) }
            + [("repair.cmd", plan.scriptInode), ("plan.json", plan.manifestInode), ("receipt.txt", plan.receiptInode)]
        guard try childNames(directory) == Set(entries.map { $0.0 }),
              try readPrivate(directory: directory, name: "receipt.txt", limit: 16 * 1024, inode: plan.receiptInode).isEmpty else { throw RepairError.unsafePath }
        // Pin and validate the complete set before removing any entry, including partial installers.
        var pinned: [(String, Int32)] = []
        defer { for (_, fd) in pinned { close(fd) } }
        for (name, inode) in entries {
            let fd = try openFile(directory: directory, name: name, limit: installerLimit, inode: inode)
            pinned.append((name, fd))
        }
        for (name, fd) in pinned { try checkEntry(directory: directory, name: name, matches: info(fd)) }
        guard try readPrivate(directory: directory, name: "receipt.txt", limit: 16 * 1024, inode: plan.receiptInode).isEmpty else { throw RepairError.unsafePath }
        for (name, fd) in pinned { try unlinkOwned(directory: directory, name: name, descriptor: fd) }
        let name = plan.identityHash + "-" + plan.nonce
        try checkEntry(directory: root, name: name, matches: info(directory))
        guard unlinkat(root, name, AT_REMOVEDIR) == 0 else { throw RepairError.unsafePath }
    }
    private static func childNames(_ fd: Int32) throws -> Set<String> {
        let duplicate = dup(fd)
        guard duplicate >= 0 else { throw RepairError.unsafePath }
        guard let stream = fdopendir(duplicate) else { close(duplicate); throw RepairError.unsafePath }
        defer { closedir(stream) }
        var names: Set<String> = []
        while true {
            errno = 0
            guard let entry = readdir(stream) else { if errno != 0 { throw RepairError.unsafePath }; return names }
            let name = withUnsafePointer(to: &entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(NAME_MAX) + 1) { String(cString: $0) }
            }
            if name == "." || name == ".." { continue }
            names.insert(name)
            guard names.count <= 16 else { throw RepairError.unsafePath }
        }
    }
    private static func checkEntry(directory: Int32, name: String, matches expected: stat) throws {
        var current = stat()
        guard fstatat(directory, name, &current, AT_SYMLINK_NOFOLLOW) == 0,
              current.st_dev == expected.st_dev, current.st_ino == expected.st_ino,
              current.st_mode & S_IFMT == expected.st_mode & S_IFMT else { throw RepairError.unsafePath }
    }
    private static func unlinkOwned(directory: Int32, name: String, descriptor: Int32) throws {
        try checkEntry(directory: directory, name: name, matches: info(descriptor))
        guard unlinkat(directory, name, 0) == 0 else { throw RepairError.unsafePath }
    }
    static func parseReceipt(_ data: Data, plan: Plan) throws -> Result {
        guard data.count <= 16 * 1024, let text = String(data: data, encoding: .utf8) else { throw RepairError.invalidReceipt }
        if text.isEmpty { return .interrupted }
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n")
        guard !normalized.contains("\r") else { throw RepairError.invalidReceipt }
        var lines = normalized.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        guard lines.first == "BEGIN \(plan.nonce) \(plan.packages.count)" else { throw RepairError.invalidReceipt }
        var position = 1, failure: Result?, restart = false
        for index in plan.packages.indices {
            if position == lines.count { return .interrupted }
            guard lines[position] == "START \(plan.nonce) \(index)" else { throw RepairError.invalidReceipt }
            position += 1
            if position == lines.count { return .interrupted }
            let fields = lines[position].components(separatedBy: " ")
            guard fields.count == 4, fields[0] == "EXIT", fields[1] == plan.nonce, fields[2] == String(index),
                  let code = Int(fields[3]), code >= 0, code <= Int(UInt32.max), String(code) == fields[3] else { throw RepairError.invalidReceipt }
            if code == 3010 || code == 1641 { restart = true }
            else if code != 0, failure == nil { failure = .failed(index: index, exitCode: code) }
            position += 1
        }
        if position == lines.count { return .interrupted }
        guard position + 1 == lines.count, lines[position] == "END \(plan.nonce) \(plan.packages.count)" else { throw RepairError.invalidReceipt }
        return failure ?? .completed(restartRequired: restart)
    }

    private actor Gate {
        var busy = false
        func begin() throws { guard !busy else { throw RepairError.busy }; busy = true }
        func end() { busy = false }
    }
    private static let gate = Gate()
    static func prepare(requirements: [RuntimePreflight.Requirement], gameRoot: URL, drive: URL, identity: String,
                        progress: @escaping @Sendable (String) -> Void) async throws -> Plan {
        try await gate.begin()
        var ownedPlan: Plan?
        do {
            try Task.checkCancellation()
            guard (1...8).contains(requirements.count) else { throw RepairError.unsupported }
            var packages: [Package] = []
            for requirement in requirements {
                progress("Checking Microsoft metadata: " + requirement.title)
                let data = try await fetch(url: metadataURL(for: requirement), destination: nil, maximum: metadataLimit)
                let package = try resolve(data: data, requirement: requirement)
                if !packages.contains(where: { $0.sha512 == package.sha512 }) { packages.append(package) }
            }
            try Task.checkCancellation()
            let plan = try makePlan(packages: packages, drive: drive, identity: identity)
            ownedPlan = plan
            let planFD = try boundDirectory(plan: plan, identity: identity, drive: drive); defer { close(planFD) }
            let rootFD = try openDirectory(URL(fileURLWithPath: plan.directoryPath).deletingLastPathComponent()); defer { close(rootFD) }
            let cacheFD = try privateDirectory(at: rootFD, name: "cache"); defer { close(cacheFD) }
            for (index, package) in packages.enumerated() {
                try Task.checkCancellation()
                progress("Preparing " + package.fileName)
                let name = String(format: "%03d.exe", index)
                let destination = try openFile(directory: planFD, name: name, limit: installerLimit, inode: plan.installerInodes[index], writing: true)
                do {
                    guard try info(destination).st_size == 0 else { throw RepairError.unsafePath }
                    var copied = false
                    let cached = openat(cacheFD, package.sha512 + ".exe", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
                    if cached >= 0 {
                        defer { close(cached) }
                        if (try? hash(cached)) == package.sha512 { try copy(cached, to: destination); copied = true }
                    }
                    if !copied, let local = try localInstaller(package: package, root: gameRoot) {
                        defer { close(local) }; try copy(local, to: destination); copied = true
                    }
                    if !copied { _ = try await fetch(url: package.url, destination: destination, maximum: installerLimit) }
                    guard fsync(destination) == 0 else { throw RepairError.unsafePath }
                    close(destination)
                } catch { close(destination); throw error }
                let installer = try openFile(directory: planFD, name: name, limit: installerLimit, inode: plan.installerInodes[index]); defer { close(installer) }
                guard try hash(installer) == package.sha512 else { throw RepairError.integrity }
                let cache = openat(cacheFD, package.sha512 + ".exe", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
                if cache >= 0 {
                    defer { close(cache) }
                    do { try copy(installer, to: cache); guard fsync(cache) == 0 else { throw RepairError.unsafePath } }
                    catch {
                        // An existing verified cache was never opened for writing. Reclaim only this new partial inode.
                        try? unlinkOwned(directory: cacheFD, name: package.sha512 + ".exe", descriptor: cache)
                        throw error
                    }
                }
            }
            try Task.checkCancellation()
            try validate(plan: plan, identity: identity, drive: drive)
            await gate.end()
            return plan
        } catch {
            // fetch() resumes only after its final write has drained; cleanup must run even on a cancelled task.
            if let ownedPlan { try? cleanupUnlaunched(plan: ownedPlan, identity: identity, drive: drive) }
            await gate.end(); throw error
        }
    }

    private static func localInstaller(package: Package, root: URL) throws -> Int32? {
        // A local candidate is never trusted by name: its complete official SHA512 must match.
        // gameRoot is an explicit caller-owned library anchor, including an active external-folder lease.
        // Pin it once; no discovered descendant can become a new trusted root.
        let anchor = try openDirectory(root, trustedRoot: root)
        defer { close(anchor) }
        var remaining = 128
        func search(_ directory: Int32, depth: Int) throws -> Int32? {
            let duplicate = dup(directory)
            guard duplicate >= 0 else { throw accessFailure(.openDirectory) }
            guard let children = fdopendir(duplicate) else { let failure = accessFailure(.openDirectory); close(duplicate); throw failure }
            defer { closedir(children) }
            while remaining > 0 {
                try Task.checkCancellation()
                errno = 0
                guard let entry = readdir(children) else {
                    if errno != 0 { throw accessFailure(.openDirectory) }
                    return nil
                }
                let name = withUnsafePointer(to: &entry.pointee.d_name) {
                    $0.withMemoryRebound(to: CChar.self, capacity: Int(NAME_MAX) + 1) { String(cString: $0) }
                }
                if name.hasPrefix(".") { continue }
                remaining -= 1
                var value = stat()
                guard fstatat(directory, name, &value, AT_SYMLINK_NOFOLLOW) == 0 else { continue }
                if value.st_mode & S_IFMT == S_IFDIR, depth < 3 {
                    guard let child = try? openDirectory(at: directory, name: name) else { continue }
                    defer { close(child) }
                    if let match = try search(child, depth: depth + 1) { return match }
                } else if value.st_mode & S_IFMT == S_IFREG, name == package.fileName {
                    let fd = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
                    if fd >= 0 { if (try? hash(fd)) == package.sha512 { return fd }; close(fd) }
                }
            }
            return nil
        }
        return try search(anchor, depth: 0)
    }

    private static func directoryPath(_ url: URL) throws -> (path: String, parts: [String]) {
        guard url.isFileURL else { throw RepairError.unsafePath }
        var path = url.path(percentEncoded: false)
        guard path.hasPrefix("/"), !path.contains("\0") else { throw RepairError.unsafePath }
        // /var is a protected OS alias. Never resolve links in writable descendants.
        if path == "/var" || path.hasPrefix("/var/") { path = "/private" + path }
        let parts = path.split(separator: "/").map(String.init)
        guard !parts.isEmpty, parts.count <= 64, !parts.contains("."), !parts.contains("..") else { throw RepairError.unsafePath }
        // Directory URL APIs disagree about a trailing slash. Persist and compare
        // the same lexical spelling without resolving any writable descendant.
        return ("/" + parts.joined(separator: "/"), parts)
    }
    private static func accessFailure(_ step: FileAccessStep) -> RepairError {
        let code = errno
        if code == ELOOP || code == ENOTDIR { return .unsafePath }
        return .fileAccess(step: step, code: code)
    }
    private static func openDirectory(_ url: URL, trustedRoot: URL = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)) throws -> Int32 {
        let anchor = try directoryPath(trustedRoot), target = try directoryPath(url)
        guard target.parts.starts(with: anchor.parts) else { throw RepairError.unsafePath }
        var fd = open(anchor.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw accessFailure(.openContainer) }
        do {
            guard try info(fd).st_mode & S_IFMT == S_IFDIR else { throw RepairError.unsafePath }
            for part in target.parts.dropFirst(anchor.parts.count) {
                let next = try openDirectory(at: fd, name: part); close(fd); fd = next
            }
            return fd
        } catch { close(fd); throw error }
    }
    private static func openDirectory(at parent: Int32, name: String) throws -> Int32 {
        let fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw accessFailure(.openDirectory) }; return fd
    }
    private static func privateDirectory(at parent: Int32, name: String, create: Bool = true) throws -> Int32 {
        if create, mkdirat(parent, name, 0o700) != 0, errno != EEXIST { throw accessFailure(.createDirectory) }
        let fd = try openDirectory(at: parent, name: name)
        do {
            let value = try info(fd)
            guard value.st_uid == getuid(), value.st_mode & 0o777 == 0o700 else { throw RepairError.unsafePath }
            return fd
        } catch { close(fd); throw error }
    }
    private static func info(_ fd: Int32) throws -> stat {
        var value = stat(); guard fstat(fd, &value) == 0 else { throw accessFailure(.inspectFile) }; return value
    }
    private static func openFile(directory: Int32, name: String, limit: Int, inode: UInt64? = nil, writing: Bool = false) throws -> Int32 {
        let fd = openat(directory, name, (writing ? O_RDWR : O_RDONLY) | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard fd >= 0 else { throw accessFailure(.openFile) }
        do {
            let value = try info(fd)
            guard value.st_mode & S_IFMT == S_IFREG, value.st_nlink == 1, value.st_uid == getuid(), value.st_mode & 0o077 == 0,
                  value.st_size >= 0, value.st_size <= limit, inode == nil || inode == UInt64(value.st_ino) else { throw RepairError.unsafePath }
            return fd
        } catch { close(fd); throw error }
    }
    private static func create(data: Data, directory: Int32, name: String) throws -> UInt64 {
        let fd = openat(directory, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw accessFailure(.createFile) }; defer { close(fd) }
        try write(data, to: fd)
        guard fsync(fd) == 0 else { throw RepairError.unsafePath }
        return UInt64(try info(fd).st_ino)
    }
    private static func readPrivate(directory: Int32, name: String, limit: Int, inode: UInt64? = nil) throws -> Data {
        let fd = try openFile(directory: directory, name: name, limit: limit, inode: inode); defer { close(fd) }
        var result = Data(), buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0 { if errno == EINTR { continue }; throw RepairError.unsafePath }
            if count == 0 { return result }
            guard count <= limit - result.count else { throw RepairError.unsafePath }
            result.append(contentsOf: buffer.prefix(count))
        }
    }
    private static func hash(_ fd: Int32) throws -> String {
        let value = try info(fd)
        guard value.st_mode & S_IFMT == S_IFREG, value.st_size > 0, value.st_size <= installerLimit, lseek(fd, 0, SEEK_SET) == 0 else { throw RepairError.integrity }
        var digest = SHA512(), buffer = [UInt8](repeating: 0, count: 1024 * 1024), total = 0
        while true {
            try Task.checkCancellation()
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0 { if errno == EINTR { continue }; throw RepairError.integrity }
            if count == 0 { break }
            total += count; guard total <= installerLimit else { throw RepairError.integrity }
            digest.update(data: Data(buffer.prefix(count)))
        }
        guard total == value.st_size else { throw RepairError.integrity }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }
    private static func copy(_ source: Int32, to destination: Int32) throws {
        guard lseek(source, 0, SEEK_SET) == 0 else { throw RepairError.unsafePath }
        var buffer = [UInt8](repeating: 0, count: 1024 * 1024), total = 0
        while true {
            try Task.checkCancellation()
            let count = Darwin.read(source, &buffer, buffer.count)
            if count < 0 { if errno == EINTR { continue }; throw RepairError.unsafePath }
            if count == 0 { return }
            total += count; guard total <= installerLimit else { throw RepairError.integrity }
            try write(Data(buffer.prefix(count)), to: destination)
        }
    }
    private static func write(_ data: Data, to fd: Int32) throws {
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0 { if errno == EINTR { continue }; throw RepairError.unsafePath }
                guard count > 0 else { throw RepairError.unsafePath }; offset += count
            }
        }
    }

    private static func fetch(url: URL, destination: Int32?, maximum: Int) async throws -> Data {
        guard allows(url) else { throw RepairError.transport }
        let transfer = Transfer(destination: destination, maximum: maximum)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil; configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil; configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 60; configuration.timeoutIntervalForResource = 600
        let session = URLSession(configuration: configuration, delegate: transfer, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        return try await withTaskCancellationHandler(operation: {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { transfer.start(session: session, url: url, continuation: $0) }
        }, onCancel: { transfer.cancel() })
    }
    /// URLSession delivers bounded chunks to disk; installer payloads are never accumulated in Data.
    private final class Transfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        let destination: Int32?, maximum: Int
        let lock = NSLock()
        var task: URLSessionDataTask?, continuation: CheckedContinuation<Data, Error>?
        var finished = false, accepted = false, received = 0, redirects = 0
        var content = Data()
        init(destination: Int32?, maximum: Int) { self.destination = destination; self.maximum = maximum }
        func start(session: URLSession, url: URL, continuation: CheckedContinuation<Data, Error>) {
            var request = URLRequest(url: url); request.httpMethod = "GET"; request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
            let task = session.dataTask(with: request)
            lock.lock()
            guard !finished else { lock.unlock(); continuation.resume(throwing: CancellationError()); return }
            self.task = task; self.continuation = continuation; lock.unlock(); task.resume()
        }
        func cancel() { finish(.failure(CancellationError())) }
        func finish(_ result: Swift.Result<Data, Error>) {
            lock.lock(); guard !finished else { lock.unlock(); return }; finished = true
            let continuation = self.continuation, task = self.task; self.continuation = nil; self.task = nil
            lock.unlock()
            if case .failure = result { task?.cancel() }
            continuation?.resume(with: result)
        }
        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let url = response.url, allows(url),
                  response.expectedContentLength == -1 || (response.expectedContentLength >= 0 && response.expectedContentLength <= maximum) else {
                completionHandler(.cancel); finish(.failure(RepairError.transport)); return
            }
            lock.lock(); let allow = !finished; accepted = allow; lock.unlock(); completionHandler(allow ? .allow : .cancel)
        }
        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            lock.lock(); guard !finished else { lock.unlock(); return }
            do {
                guard accepted, data.count <= maximum - received else { throw RepairError.transport }
                received += data.count
                if let destination { try RuntimeRepair.write(data, to: destination) } else { content.append(data) }
                lock.unlock()
            } catch { lock.unlock(); finish(.failure(error)) }
        }
        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            if let error { finish(.failure(error)); return }
            lock.lock(); let valid = accepted && received > 0, data = content; lock.unlock()
            finish(valid ? .success(data) : .failure(RepairError.transport))
        }
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            lock.lock(); redirects += 1; let allowed = redirects <= 5 && !finished; lock.unlock()
            guard allowed, let url = request.url, allows(url) else { completionHandler(nil); finish(.failure(RepairError.transport)); return }
            var clean = URLRequest(url: url); clean.httpMethod = "GET"; clean.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
            completionHandler(clean)
        }
        func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust { completionHandler(.performDefaultHandling, nil) }
            else { completionHandler(.cancelAuthenticationChallenge, nil) }
        }
    }
}
