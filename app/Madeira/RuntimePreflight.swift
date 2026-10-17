// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import CoreFoundation
import Darwin

enum RuntimePreflight {
    enum Family: String, Codable, Sendable { case netCore, desktop, aspnet, framework }
    enum Architecture: String, Codable, Sendable { case x86, x64, arm64, unknown }
    struct Version: Codable, Hashable, Comparable, Sendable, CustomStringConvertible {
        let major: Int
        let minor: Int
        let patch: Int
        init(major: Int, minor: Int, patch: Int) { self.major = major; self.minor = minor; self.patch = patch }
        init?(_ text: String) {
            let parts = text.split(separator: ".", omittingEmptySubsequences: false)
            guard (2...3).contains(parts.count), parts.allSatisfy({ !$0.isEmpty && $0.utf8.allSatisfy { (48...57).contains($0) } }) else { return nil }
            let values = parts.compactMap { Int($0) }
            guard values.count == parts.count, values.allSatisfy({ $0 <= 9999 }) else { return nil }
            self.init(major: values[0], minor: values[1], patch: values.count == 3 ? values[2] : 0)
        }
        var description: String { "\(major).\(minor).\(patch)" }
        static func < (lhs: Self, rhs: Self) -> Bool { (lhs.major, lhs.minor, lhs.patch) < (rhs.major, rhs.minor, rhs.patch) }
    }
    enum RollForward: String, Codable, Sendable {
        case disable = "Disable", latestPatch = "LatestPatch", minor = "Minor", major = "Major", latestMinor = "LatestMinor", latestMajor = "LatestMajor"
    }
    struct Requirement: Hashable, Codable, Sendable {
        let family: Family
        let version: Version
        let architecture: Architecture
        let rollForward: RollForward
        let applyPatches: Bool?
        init(family: Family, version: Version, architecture: Architecture, rollForward: RollForward = .minor, applyPatches: Bool? = nil) {
            self.family = family; self.version = version; self.architecture = architecture
            self.rollForward = rollForward; self.applyPatches = applyPatches
        }
        func accepts(version candidate: Version) -> Bool {
            guard candidate >= version else { return false }
            if family == .framework { return candidate.major == version.major }
            switch rollForward {
            case .disable: return candidate == version
            case .latestPatch: return candidate.major == version.major && candidate.minor == version.minor && (applyPatches != false || candidate.patch == version.patch)
            case .minor, .latestMinor:
                return candidate.major == version.major && (candidate.minor > version.minor || applyPatches != false || candidate.patch == version.patch)
            case .major, .latestMajor:
                return candidate.major > version.major || candidate.minor > version.minor || applyPatches != false || candidate.patch == version.patch
            }
        }
        var title: String {
            let name: String
            switch family {
            case .netCore: name = ".NET Runtime"
            case .desktop: name = ".NET Desktop Runtime"
            case .aspnet: name = "ASP.NET Core Runtime"
            case .framework: name = ".NET Framework"
            }
            return "\(name) \(version) (\(architecture.rawValue))"
        }
    }
    enum State: Sendable, Equatable { case satisfied, missing, unknown, unsupported }
    struct Finding: Identifiable, Sendable {
        let id: String
        let requirement: Requirement?
        let state: State
        let message: String
    }
    struct Report: Sendable {
        let findings: [Finding]
        var missingRequirements: [Requirement] { findings.compactMap { $0.state == .missing ? $0.requirement : nil } }
        var requiresAttention: Bool { findings.contains { $0.state != .satisfied } }
    }
    static func scan(executable: URL, gameRoot: URL, drive: URL, prefix: URL) throws -> Report {
        try Task.checkCancellation()
        var findings: [Finding] = []
        func unknown(_ message: String) {
            findings.append(Finding(id: "metadata-\(findings.count)", requirement: nil, state: .unknown, message: message))
        }
        do {
            let files = try ReadRoot(gameRoot)
            let exePath = try files.relative(executable)
            let image = try files.image(exePath)
            let architecture = image.architecture
            let directory = (exePath as NSString).deletingLastPathComponent
            let stem = ((exePath as NSString).lastPathComponent as NSString).deletingPathExtension
            let runtimePath = joined(directory, stem + ".runtimeconfig.json")
            let configPath = exePath + ".config"
            var requirements: [Requirement] = []
            var declared = false
            for (path, modern) in [(runtimePath, true), (configPath, false)] {
                try Task.checkCancellation()
                do {
                    guard let data = try files.read(path, limit: 128 * 1024) else { continue }
                    declared = true
                    let parsed = modern ? try modernRequirements(data, architecture: architecture) : try legacyRequirements(data, architecture: architecture)
                    requirements.append(contentsOf: parsed)
                } catch is CancellationError { throw CancellationError() }
                catch { declared = true; unknown("Cannot safely interpret \((path as NSString).lastPathComponent): \(error.localizedDescription)") }
            }
            if !declared && image.managed { unknown("Managed executable has no explicit runtime declaration; its required .NET version is unknown.") }
            let distinct = Array(Set(requirements)).sorted { $0.title < $1.title }
            if !distinct.isEmpty && architecture == .unknown {
                for requirement in distinct {
                    findings.append(Finding(id: requirement.title, requirement: requirement, state: .unknown,
                                            message: "Managed AnyCPU executable does not identify a single runtime architecture."))
                }
            } else {
                let runtimeFiles = try? ReadRoot(drive)
                let prefixFiles = try? ReadRoot(prefix)
                for requirement in distinct {
                    try Task.checkCancellation()
                    if architecture == .arm64 || (requirement.family == .framework && !supportedFramework(requirement.version)) {
                        findings.append(Finding(id: requirement.title, requirement: requirement, state: .unsupported, message: "This declared runtime is not supported by automatic repair."))
                        continue
                    }
                    do {
                        guard let runtimeFiles else { throw Failure.unreadable }
                        let present = requirement.family == .framework
                            ? try hasFramework(requirement, files: runtimeFiles, prefix: prefixFiles)
                            : try hasModern(requirement, files: runtimeFiles)
                        findings.append(Finding(id: requirement.title, requirement: requirement, state: present ? .satisfied : .missing,
                                                message: present ? "Matching native runtime files are installed in this Wine prefix." : "No matching complete runtime was found in this Wine prefix."))
                    } catch is CancellationError { throw CancellationError() }
                    catch {
                        findings.append(Finding(id: requirement.title, requirement: requirement, state: .unknown,
                                                message: "Runtime installation could not be safely verified."))
                    }
                }
            }
        } catch is CancellationError { throw CancellationError() }
        catch { unknown("Executable could not be safely inspected: \(error.localizedDescription)") }
        return Report(findings: findings)
    }

    private enum Failure: LocalizedError {
        case unreadable, unsafePath, oversized, malformed, unsupportedPolicy
        var errorDescription: String? {
            switch self {
            case .unreadable: return "file unreadable"
            case .unsafePath: return "unsafe file path"
            case .oversized: return "metadata exceeds inspection limit"
            case .malformed: return "invalid or unsupported runtime metadata"
            case .unsupportedPolicy: return "unsupported runtime resolution policy"
            }
        }
    }
    private static func joined(_ parent: String, _ name: String) -> String { parent.isEmpty ? name : parent + "/" + name }
    private static func family(_ name: String) -> Family? {
        switch name {
        case "Microsoft.NETCore.App": return .netCore
        case "Microsoft.WindowsDesktop.App": return .desktop
        case "Microsoft.AspNetCore.App": return .aspnet
        default: return nil
        }
    }
    private static func modernRequirements(_ data: Data, architecture: Architecture) throws -> [Requirement] {
        // Limit nesting before JSONSerialization allocates recursive container graphs.
        var depth = 0, string = false, escape = false
        for byte in data {
            if string {
                if escape { escape = false } else if byte == 92 { escape = true } else if byte == 34 { string = false }
            } else if byte == 34 { string = true }
            else if byte == 123 || byte == 91 { depth += 1; if depth > 32 { throw Failure.malformed } }
            else if byte == 125 || byte == 93 { depth -= 1 }
        }
        guard let document = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let options = document["runtimeOptions"] as? [String: Any] else { throw Failure.malformed }
        // Legacy rollForwardOnNoCandidateFx and development probing change resolution;
        // do not infer a repair from policies this detector does not model.
        guard options["rollForwardOnNoCandidateFx"] == nil, options["additionalProbingPaths"] == nil else { throw Failure.unsupportedPolicy }
        func declarations(_ value: Any) throws -> [[String: Any]] {
            guard let values = value as? [[String: Any]], !values.isEmpty, values.count <= 16 else { throw Failure.malformed }
            return values
        }
        var declared: [[String: Any]] = []
        if let single = options["framework"] {
            guard let value = single as? [String: Any] else { throw Failure.malformed }
            declared.append(value)
        }
        if let many = options["frameworks"] {
            guard declared.isEmpty else { throw Failure.malformed }
            declared += try declarations(many)
        }
        let policyObjects = [options] + declared
        let hasModernPolicy = policyObjects.contains { $0["rollForward"] != nil }
        let hasLegacyPolicy = policyObjects.contains { $0["applyPatches"] != nil || $0["rollForwardOnNoCandidateFx"] != nil }
        // .NET 6-10 rejects mixing these settings anywhere in one config,
        // including across separate framework entries.
        guard !(hasModernPolicy && hasLegacyPolicy) else { throw Failure.unsupportedPolicy }
        if let included = options["includedFrameworks"] {
            let values = try declarations(included)
            for value in values {
                guard let name = value["name"] as? String, family(name) != nil,
                      let text = value["version"] as? String, Version(text) != nil else { throw Failure.malformed }
            }
            guard declared.isEmpty else { throw Failure.malformed }
            return [] // Bundled frameworks are not global runtime requirements.
        }
        guard !declared.isEmpty, declared.count <= 16 else { throw Failure.malformed }
        let names = declared.compactMap { $0["name"] as? String }
        guard Set(names).count == declared.count else { throw Failure.malformed }
        return try declared.map { entry in
            guard entry["rollForwardOnNoCandidateFx"] == nil else { throw Failure.unsupportedPolicy }
            guard let name = entry["name"] as? String, let family = family(name),
                  let text = entry["version"] as? String, let version = Version(text) else { throw Failure.malformed }
            let rawPolicy = entry["rollForward"] ?? options["rollForward"]
            let policy: RollForward
            if let rawPolicy {
                guard let text = rawPolicy as? String,
                      let value = [RollForward.disable, .latestPatch, .minor, .major, .latestMinor, .latestMajor].first(where: { $0.rawValue.caseInsensitiveCompare(text) == .orderedSame }) else { throw Failure.unsupportedPolicy }
                policy = value
            } else { policy = .minor }
            let rawPatches = entry["applyPatches"] ?? options["applyPatches"]
            var patches: Bool?
            if let rawPatches {
                guard let number = rawPatches as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { throw Failure.unsupportedPolicy }
                patches = number.boolValue
            }
            return Requirement(family: family, version: version, architecture: architecture, rollForward: policy, applyPatches: patches)
        }
    }

    private final class LegacyParser: NSObject, XMLParserDelegate {
        var versions: [Version] = []
        var invalid = false
        var depth = 0
        var count = 0
        func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?, qualifiedName qName: String?, attributes: [String: String]) {
            depth += 1; count += 1
            if depth > 32 || count > 1024 { invalid = true; parser.abortParsing(); return }
            if elementName == "supportedRuntime" {
                guard versions.count < 16, let sku = attributes["sku"], sku.hasPrefix(".NETFramework,Version=v"),
                      let version = Version(String(sku.dropFirst(".NETFramework,Version=v".count))) else {
                    invalid = true; return
                }
                versions.append(version)
            }
        }
        func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) { depth -= 1 }
    }
    private static func legacyRequirements(_ data: Data, architecture: Architecture) throws -> [Requirement] {
        guard let text = String(data: data, encoding: .utf8), !text.localizedCaseInsensitiveContains("<!DOCTYPE"), !text.localizedCaseInsensitiveContains("<!ENTITY") else { throw Failure.malformed }
        let parser = XMLParser(data: data), delegate = LegacyParser()
        parser.shouldResolveExternalEntities = false; parser.delegate = delegate
        guard parser.parse(), !delegate.invalid, delegate.versions.count == 1 else { throw Failure.malformed }
        return delegate.versions.map { Requirement(family: .framework, version: $0, architecture: architecture) }
    }

    private static func hasModern(_ requirement: Requirement, files: ReadRoot, installationRoot: String? = nil) throws -> Bool {
        let roots: [String]
        if let installationRoot { roots = [installationRoot] }
        else {
            switch requirement.architecture {
            case .x64: roots = ["Program Files/dotnet", "Program Files/dotnet/x64"]
            case .x86: roots = ["Program Files (x86)/dotnet", "Program Files/dotnet"]
            case .arm64, .unknown: return false
            }
        }
        let name: String
        let marker: String
        switch requirement.family {
        case .netCore: name = "Microsoft.NETCore.App"; marker = "coreclr.dll"
        case .desktop: name = "Microsoft.WindowsDesktop.App"; marker = "PresentationFramework.dll"
        case .aspnet: name = "Microsoft.AspNetCore.App"; marker = "Microsoft.AspNetCore.dll"
        case .framework: return false
        }
        for root in roots {
            try Task.checkCancellation()
            guard try files.native(joined(root, "dotnet.exe"), architecture: requirement.architecture) else { continue }
            let hostRoot = joined(root, "host/fxr")
            let hosts = try files.names(hostRoot)
            var hasHost = false
            for host in hosts where Version(host) != nil {
                if try files.native(joined(hostRoot, host + "/hostfxr.dll"), architecture: requirement.architecture) { hasHost = true; break }
            }
            guard hasHost else { continue }
            let shared = joined(root, "shared/" + name)
            for candidate in try files.names(shared) {
                try Task.checkCancellation()
                guard let version = Version(candidate), requirement.accepts(version: version) else { continue }
                let directory = joined(shared, candidate)
                guard try files.marker(joined(directory, name + ".deps.json")) else { continue }
                if requirement.family == .netCore {
                    guard try files.native(joined(directory, marker), architecture: requirement.architecture),
                          try files.native(joined(directory, "hostpolicy.dll"), architecture: requirement.architecture),
                          try files.validImage(joined(directory, "System.Private.CoreLib.dll"), managed: true) else { continue }
                    return true
                } else {
                    // Desktop/ASP.NET assemblies may be AnyCPU. Native CoreCLR and
                    // hostfxr establish bitness, never a managed assembly machine alone.
                    guard try files.validImage(joined(directory, marker)) else { continue }
                    let core = Requirement(family: .netCore, version: version, architecture: requirement.architecture,
                                           rollForward: .latestPatch, applyPatches: true)
                    if try hasModern(core, files: files, installationRoot: root) { return true }
                }
            }
        }
        return false
    }
    private static func supportedFramework(_ version: Version) -> Bool {
        guard version.major == 4 else { return false }
        switch (version.minor, version.patch) {
        case (5...7, 0...2), (8, 0...1): return true
        default: return false
        }
    }
    private static func hasFramework(_ requirement: Requirement, files: ReadRoot, prefix: ReadRoot?) throws -> Bool {
        guard let prefix, let data = try prefix.read("system.reg", limit: 8 * 1024 * 1024), let registry = String(data: data, encoding: .utf8) else { return false }
        let key = requirement.architecture == .x86
            ? "software\\wow6432node\\microsoft\\net framework setup\\ndp\\v4\\full"
            : "software\\microsoft\\net framework setup\\ndp\\v4\\full"
        var inSection = false
        var release: Int?
        for line in registry.split(whereSeparator: \.isNewline) {
            try Task.checkCancellation()
            let value = line.trimmingCharacters(in: .whitespaces).lowercased()
            if value.hasPrefix("[") {
                let section = value.split(separator: "]", maxSplits: 1).first.map(String.init) ?? ""
                inSection = section.dropFirst().replacingOccurrences(of: "\\\\", with: "\\") == key
            } else if inSection && value.hasPrefix("\"release\"=dword:") {
                release = Int(value.dropFirst("\"release\"=dword:".count), radix: 16)
            }
        }
        let minimum: Int
        switch (requirement.version.major, requirement.version.minor, requirement.version.patch) {
        case (4, 5, 0): minimum = 378389
        case (4, 5, 1): minimum = 378675
        case (4, 5, _): minimum = 379893
        case (4, 6, 0): minimum = 393295
        case (4, 6, 1): minimum = 394254
        case (4, 6, _): minimum = 394802
        case (4, 7, 0): minimum = 460798
        case (4, 7, 1): minimum = 461308
        case (4, 7, _): minimum = 461808
        case (4, 8, 0): minimum = 528040
        case (4, 8, 1): minimum = 533320
        default: return false
        }
        guard let release, release >= minimum else { return false }
        let framework = requirement.architecture == .x86 ? "Framework" : "Framework64"
        return try files.native("windows/Microsoft.NET/" + framework + "/v4.0.30319/clr.dll", architecture: requirement.architecture)
    }

    private struct Image {
        let architecture: Architecture
        let managed: Bool
    }
    /// All untrusted descendants are opened component by component, anchored to
    /// the trusted root FD; links and non-regular files never become read targets.
    private final class ReadRoot {
        let root: URL
        let original: URL
        let fd: Int32
        init(_ url: URL) throws {
            original = url.standardizedFileURL
            root = url.resolvingSymlinksInPath().standardizedFileURL
            fd = open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard fd >= 0 else { throw Failure.unreadable }
        }
        deinit { close(fd) }
        func relative(_ url: URL) throws -> String {
            let target = url.standardizedFileURL.path
            for base in [original.path + "/", root.path + "/"] where target.hasPrefix(base) {
                return String(target.dropFirst(base.count))
            }
            throw Failure.unsafePath
        }
        func opened(_ path: String, directory: Bool = false) throws -> Int32? {
            try Task.checkCancellation()
            let parts = path.split(separator: "/", omittingEmptySubsequences: false)
            guard !parts.isEmpty, parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." && !$0.utf8.contains(0) }) else { throw Failure.unsafePath }
            var parent = dup(fd)
            guard parent >= 0 else { throw Failure.unreadable }
            for (index, part) in parts.enumerated() {
                let isDirectory = index < parts.count - 1 || directory
                let next = openat(parent, String(part), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK | (isDirectory ? O_DIRECTORY : 0))
                let code = errno
                close(parent)
                guard next >= 0 else {
                    if code == ENOENT { return nil }
                    throw code == ELOOP || code == ENOTDIR ? Failure.unsafePath : Failure.unreadable
                }
                parent = next
            }
            return parent
        }
        func regular(_ fd: Int32) throws -> Int64 {
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_size >= 0 else { throw Failure.unsafePath }
            return info.st_size
        }
        func bytes(_ fd: Int32, offset: Int, count: Int) throws -> Data {
            try Task.checkCancellation()
            guard offset >= 0, count >= 0, count <= 128 * 1024 else { throw Failure.malformed }
            var buffer = [UInt8](repeating: 0, count: count)
            var done = 0
            while done < count {
                try Task.checkCancellation()
                let amount = buffer.withUnsafeMutableBytes { Darwin.pread(fd, $0.baseAddress!.advanced(by: done), count - done, off_t(offset + done)) }
                if amount < 0 && errno == EINTR { continue }
                guard amount > 0 else { throw Failure.unreadable }
                done += amount
            }
            return Data(buffer)
        }
        func read(_ path: String, limit: Int) throws -> Data? {
            guard let file = try opened(path) else { return nil }
            defer { close(file) }
            let size = try regular(file)
            guard size <= limit else { throw Failure.oversized }
            var result = Data()
            while result.count < Int(size) {
                let amount = min(128 * 1024, Int(size) - result.count)
                result.append(try bytes(file, offset: result.count, count: amount))
            }
            return result
        }
        func names(_ path: String) throws -> [String] {
            guard let directory = try opened(path, directory: true) else { return [] }
            guard let stream = fdopendir(directory) else { close(directory); throw Failure.unreadable }
            defer { closedir(stream) }
            var names: [String] = []
            while let entry = readdir(stream) {
                try Task.checkCancellation()
                let name = withUnsafePointer(to: &entry.pointee.d_name) {
                    $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXNAMLEN) + 1) { String(cString: $0) }
                }
                if name == "." || name == ".." { continue }
                guard names.count < 256 else { throw Failure.oversized }
                names.append(name)
            }
            return names
        }
        func marker(_ path: String) throws -> Bool {
            guard let file = try opened(path) else { return false }
            defer { close(file) }
            return try regular(file) > 0
        }
        func native(_ path: String, architecture: Architecture) throws -> Bool {
            guard let file = try opened(path) else { return false }
            defer { close(file) }
            let image = try image(file)
            return !image.managed && image.architecture == architecture
        }
        func validImage(_ path: String, managed: Bool = false) throws -> Bool {
            guard let file = try opened(path) else { return false }
            defer { close(file) }
            let parsed = try image(file)
            return !managed || parsed.managed
        }
        func image(_ path: String) throws -> Image {
            guard let file = try opened(path) else { throw Failure.unreadable }
            defer { close(file) }
            return try image(file)
        }
        func image(_ file: Int32) throws -> Image {
            let size = try regular(file)
            func u16(_ data: Data, _ at: Int) -> Int { Int(data[at]) | Int(data[at + 1]) << 8 }
            func u32(_ data: Data, _ at: Int) -> Int { u16(data, at) | u16(data, at + 2) << 16 }
            guard size >= 64 else { throw Failure.malformed }
            let dos = try bytes(file, offset: 0, count: 64)
            guard dos[0] == 0x4d, dos[1] == 0x5a else { throw Failure.malformed }
            let offset = u32(dos, 0x3c)
            guard offset >= 64, offset <= 16 * 1024 * 1024, Int64(offset + 24) <= size else { throw Failure.malformed }
            let coff = try bytes(file, offset: offset, count: 24)
            guard Array(coff.prefix(4)) == [0x50, 0x45, 0, 0] else { throw Failure.malformed }
            let machine = u16(coff, 4), sectionCount = u16(coff, 6), optionalSize = u16(coff, 20)
            guard sectionCount <= 96, optionalSize >= 96, optionalSize <= 512, Int64(offset + 24 + optionalSize + sectionCount * 40) <= size else { throw Failure.malformed }
            let optional = try bytes(file, offset: offset + 24, count: optionalSize)
            let pe32 = u16(optional, 0) == 0x10b
            guard pe32 || u16(optional, 0) == 0x20b else { throw Failure.malformed }
            var architecture: Architecture
            switch machine { case 0x14c: architecture = .x86; case 0x8664: architecture = .x64; case 0xaa64: architecture = .arm64; default: architecture = .unknown }
            let directoryBase = pe32 ? 96 : 112
            var managed = false
            if optional.count >= directoryBase + 15 * 8 && u32(optional, directoryBase - 4) >= 15 {
                let rva = u32(optional, directoryBase + 14 * 8), length = u32(optional, directoryBase + 14 * 8 + 4)
                if rva != 0 {
                    guard length >= 20 else { throw Failure.malformed }
                    managed = true
                    let sections = try bytes(file, offset: offset + 24 + optionalSize, count: sectionCount * 40)
                    var clrOffset: Int?
                    for index in 0..<sectionCount {
                        let at = index * 40, address = u32(sections, at + 12), rawSize = u32(sections, at + 16), raw = u32(sections, at + 20)
                        if rva >= address && rva - address <= rawSize && rawSize - (rva - address) >= 20 { clrOffset = raw + rva - address; break }
                    }
                    guard let clrOffset, Int64(clrOffset + 20) <= size else { throw Failure.malformed }
                    let clr = try bytes(file, offset: clrOffset, count: 20), flags = u32(clr, 16)
                    if machine == 0x14c && flags & 1 != 0 && flags & 2 == 0 && flags & 0x20000 == 0 { architecture = .unknown }
                }
            }
            return Image(architecture: architecture, managed: managed)
        }
    }
}
