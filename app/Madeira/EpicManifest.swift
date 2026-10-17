// SPDX-License-Identifier: GPL-3.0-or-later
// BuildPatch wire-format reference: Legendary contributors,
// https://github.com/legendary-gl/legendary/tree/master/legendary/models
// Native Swift implementation; no Madeira Converter Exception is applied.

import Foundation
import CryptoKit
import zlib

enum EpicDownloadError: Error, LocalizedError {
    case invalidManifest, unsupportedManifest, invalidPath, invalidChunk, hashMismatch
    case unsafeDestination, differentInstall, existingFile, filesystem, contentServer

    var errorDescription: String? {
        switch self {
        case .invalidManifest: return "The Epic game manifest is damaged or exceeds supported limits."
        case .unsupportedManifest: return "This Epic manifest uses unsupported encrypted or file-based content."
        case .invalidPath: return "The Epic manifest contains an unsafe or conflicting installation path."
        case .invalidChunk: return "An Epic download chunk is damaged or has an unsupported format."
        case .hashMismatch: return "Downloaded Epic content failed its integrity check."
        case .unsafeDestination: return "The Epic installation folder contains an unsafe link or unexpected file."
        case .differentInstall: return "This folder belongs to another Epic installation or build."
        case .existingFile: return "An existing game file differs from this download. It has been preserved."
        case .filesystem: return "Epic could not write the installation files. Check available storage."
        case .contentServer: return "Epic content servers did not return a valid download. Try again later."
        }
    }
}

struct EpicChunkPart: Sendable {
    let guid: String
    let offset: Int
    let size: Int
}

struct EpicFileManifest: Sendable {
    let filename: String
    let fileHash: Data
    let chunkParts: [EpicChunkPart]
    var fileSize: Int64 { chunkParts.reduce(0) { $0 + Int64($1.size) } }
}

struct EpicChunkInfo: Sendable {
    let guid: String
    let rollingHash: UInt64
    let sha1: Data
    let group: Int
    let windowSize: Int
    let fileSize: Int
    let featureLevel: UInt32

    var path: String {
        if featureLevel >= 22 {
            let words = stride(from: 0, to: 32, by: 8).map {
                UInt32(guid.dropFirst($0).prefix(8), radix: 16)!
            }
            var guidBytes = Data()
            for word in words { var little = word.littleEndian; guidBytes.append(Data(bytes: &little, count: 4)) }
            var littleHash = rollingHash.littleEndian
            let hashBytes = Data(bytes: &littleHash, count: 8)
            return "ChunksV5/plain/\(String(format: "%02d", group))/\(Self.base64(hashBytes))_\(Self.base64(guidBytes)).chunk"
        }
        let directory = featureLevel >= 15 ? "ChunksV4" : featureLevel >= 6 ? "ChunksV3"
            : featureLevel >= 3 ? "ChunksV2" : "Chunks"
        return "\(directory)/\(String(format: "%02d", group))/\(String(format: "%016llX", rollingHash))_\(guid).chunk"
    }

    private static func base64(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}

struct EpicManifest: Sendable {
    static let maximumManifestBytes = 64 * 1024 * 1024
    static let maximumChunkBytes = 16 * 1024 * 1024
    let appName: String
    let launchExecutable: String
    let launchCommand: String
    let fileManifestList: [EpicFileManifest]
    let totalBytes: Int64
    let chunks: [String: EpicChunkInfo]

    static func parse(_ data: Data) throws -> EpicManifest {
        guard !data.isEmpty, data.count <= maximumManifestBytes else { throw EpicDownloadError.invalidManifest }
        let raw: (String, String, String, [EpicFileManifest], [String: EpicChunkInfo])
        if data.first(where: { ![UInt8(9), 10, 13, 32].contains($0) }) == UInt8(ascii: "{") {
            raw = try parseJSON(data)
        } else { raw = try parseBinary(data) }
        guard !raw.0.isEmpty, raw.0.utf8.count <= 256, !raw.0.unicodeScalars.contains(where: { $0.value < 32 }),
              raw.2.utf8.count <= 32767, !raw.2.contains("\0"), !raw.3.isEmpty,
              raw.3.count <= 250_000, raw.4.count <= 500_000 else { throw EpicDownloadError.invalidManifest }
        let launch = try safePath(raw.1)
        var names: [String: String] = [:]
        var files = Set<String>()
        var directories = Set<String>()
        var normalized: [EpicFileManifest] = []
        var total: Int64 = 0
        var partCount = 0
        for file in raw.3 {
            let path = try safePath(file.filename)
            let components = path.split(separator: "/").map(String.init)
            for index in components.indices {
                let prefix = components[...index].joined(separator: "/")
                let folded = caseKey(prefix)
                if let old = names[folded], old != prefix { throw EpicDownloadError.invalidPath }
                names[folded] = prefix
                if index < components.count - 1 {
                    guard !files.contains(folded) else { throw EpicDownloadError.invalidPath }
                    directories.insert(folded)
                } else {
                    guard !directories.contains(folded), files.insert(folded).inserted else {
                        throw EpicDownloadError.invalidPath
                    }
                }
            }
            guard file.fileHash.count == 20 else { throw EpicDownloadError.invalidManifest }
            var length: Int64 = 0
            for part in file.chunkParts {
                guard let chunk = raw.4[part.guid], part.offset >= 0, part.size > 0,
                      part.offset <= chunk.windowSize, part.size <= chunk.windowSize - part.offset else {
                    throw EpicDownloadError.invalidManifest
                }
                length += Int64(part.size)
                partCount += 1
                guard length <= 512 * 1024 * 1024 * 1024, partCount <= 2_000_000 else {
                    throw EpicDownloadError.invalidManifest
                }
            }
            total += length
            guard total <= 2 * 1024 * 1024 * 1024 * 1024 else { throw EpicDownloadError.invalidManifest }
            normalized.append(EpicFileManifest(filename: path, fileHash: file.fileHash, chunkParts: file.chunkParts))
        }
        guard let executable = normalized.first(where: { caseKey($0.filename) == caseKey(launch) }) else {
            throw EpicDownloadError.invalidManifest
        }
        for chunk in raw.4.values {
            guard chunk.sha1.count == 20, chunk.windowSize > 0, chunk.windowSize <= maximumChunkBytes,
                  chunk.fileSize >= 41, chunk.fileSize <= maximumChunkBytes + 65536,
                  chunk.group >= 0, chunk.group <= 99 else { throw EpicDownloadError.invalidManifest }
        }
        return EpicManifest(appName: raw.0, launchExecutable: executable.filename, launchCommand: raw.2,
                            fileManifestList: normalized, totalBytes: total, chunks: raw.4)
    }

    static func caseKey(_ path: String) -> String {
        path.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
    }

    static func safePath(_ input: String) throws -> String {
        let path = input.replacingOccurrences(of: "\\", with: "/").precomposedStringWithCanonicalMapping
        guard !path.isEmpty, path.utf8.count <= 2400, !path.hasPrefix("/"),
              !path.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else {
            throw EpicDownloadError.invalidPath
        }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count <= 64 else { throw EpicDownloadError.invalidPath }
        for part in parts {
            guard !part.isEmpty, part != ".", part != "..", part.utf8.count <= 255,
                  !part.hasSuffix("."), !part.hasSuffix(" "),
                  !part.contains(where: { ":*?\"<>|".contains($0) }) else { throw EpicDownloadError.invalidPath }
            let base = part.split(separator: ".").first?.uppercased() ?? ""
            let reserved = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"]
            guard !reserved.contains(base) else { throw EpicDownloadError.invalidPath }
        }
        guard caseKey(String(parts[0])) != ".madeira-epic" else { throw EpicDownloadError.invalidPath }
        return path
    }

    private static func parseJSON(_ data: Data) throws -> (String, String, String, [EpicFileManifest], [String: EpicChunkInfo]) {
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let app = json["AppNameString"] as? String, let exe = json["LaunchExeString"] as? String,
              let command = json["LaunchCommand"] as? String,
              let sizes = json["ChunkFilesizeList"] as? [String: String],
              let hashes = json["ChunkHashList"] as? [String: String],
              let shas = json["ChunkShaList"] as? [String: String],
              let groups = json["DataGroupList"] as? [String: String],
              let fileList = json["FileManifestList"] as? [[String: Any]],
              sizes.count <= 500_000, fileList.count <= 250_000 else { throw EpicDownloadError.invalidManifest }
        guard json["bIsFileData"] as? Bool != true else { throw EpicDownloadError.unsupportedManifest }
        let versionValue = try blobNumber(json["ManifestFileVersion"] as? String ?? "013000000000", bytes: 4)
        guard versionValue <= 24 else { throw EpicDownloadError.unsupportedManifest }
        let version = UInt32(versionValue)
        var chunks: [String: EpicChunkInfo] = [:]
        for (id, size) in sizes {
            let guid = try validGUID(id)
            guard chunks[guid] == nil, let rolling = hashes[id], let sha = shas[id], let group = groups[id] else {
                throw EpicDownloadError.invalidManifest
            }
            chunks[guid] = EpicChunkInfo(guid: guid, rollingHash: try blobNumber(rolling, bytes: 8),
                                         sha1: try hexData(sha, count: 20), group: Int(try blobNumber(group, bytes: 1)),
                                         windowSize: 1024 * 1024, fileSize: try boundedInt(blobNumber(size, bytes: 8)),
                                         featureLevel: version)
        }
        var files: [EpicFileManifest] = []
        for file in fileList {
            guard let name = file["Filename"] as? String, let hash = file["FileHash"] as? String,
                  let parts = file["FileChunkParts"] as? [[String: String]], parts.count <= 2_000_000 else {
                throw EpicDownloadError.invalidManifest
            }
            guard (file["SymlinkTarget"] as? String ?? "").isEmpty else { throw EpicDownloadError.invalidPath }
            let parsedParts = try parts.map { part -> EpicChunkPart in
                guard let guid = part["Guid"], let offset = part["Offset"], let size = part["Size"] else {
                    throw EpicDownloadError.invalidManifest
                }
                return EpicChunkPart(guid: try validGUID(guid), offset: Int(try blobNumber(offset, bytes: 4)),
                                     size: Int(try blobNumber(size, bytes: 4)))
            }
            files.append(EpicFileManifest(filename: name, fileHash: try blobBytes(hash, count: 20), chunkParts: parsedParts))
        }
        return (app, exe, command, files, chunks)
    }

    private static func parseBinary(_ data: Data) throws -> (String, String, String, [EpicFileManifest], [String: EpicChunkInfo]) {
        var header = EpicBinaryReader(data)
        guard try header.u32() == 0x44BEC00C else { throw EpicDownloadError.invalidManifest }
        let headerSize = Int(try header.u32()), expanded = Int(try header.u32()), storedSize = Int(try header.u32())
        let digest = try header.bytes(20), storedAs = try header.u8(), version = try header.u32()
        guard version <= 24, storedAs & ~1 == 0 else { throw EpicDownloadError.unsupportedManifest }
        guard headerSize >= (version >= 22 ? 73 : 41), headerSize <= 4096,
              storedSize == data.count - headerSize, expanded <= maximumManifestBytes * 2 else {
            throw EpicDownloadError.invalidManifest
        }
        try header.skipTo(headerSize)
        let stored = try header.bytes(storedSize)
        let body = storedAs & 1 != 0 ? try EpicCompression.inflate(stored, expectedSize: expanded) : stored
        guard body.count == expanded, Data(Insecure.SHA1.hash(data: body)) == digest else { throw EpicDownloadError.hashMismatch }
        var reader = EpicBinaryReader(body)
        var meta = try reader.section()
        let metaVersion = try meta.u8(), feature = try meta.u32(), fileData = try meta.u8()
        guard metaVersion <= 2, feature <= 24, fileData == 0 else { throw EpicDownloadError.unsupportedManifest }
        _ = try meta.u32()
        let app = try meta.string(); _ = try meta.string()
        let exe = try meta.string(), command = try meta.string()
        let prerequisites = try meta.count(maximum: 4096)
        for _ in 0..<prerequisites { _ = try meta.string() }
        for _ in 0..<3 { _ = try meta.string() }
        if metaVersion >= 1 { _ = try meta.string() }
        if metaVersion >= 2 { _ = try meta.string(); _ = try meta.string() }
        try meta.finish()

        var cdl = try reader.section()
        guard try cdl.u8() == 0 else { throw EpicDownloadError.unsupportedManifest }
        let chunkCount = try cdl.count(maximum: 500_000)
        var guids: [String] = [], rolling: [UInt64] = [], shas: [Data] = [], groups: [Int] = [], windows: [Int] = [], sizes: [Int] = []
        for _ in 0..<chunkCount { guids.append(try cdl.guid()) }
        for _ in 0..<chunkCount { rolling.append(try cdl.u64()) }
        for _ in 0..<chunkCount { shas.append(try cdl.bytes(20)) }
        for _ in 0..<chunkCount { groups.append(Int(try cdl.u8())) }
        for _ in 0..<chunkCount { windows.append(Int(try cdl.u32())) }
        for _ in 0..<chunkCount { sizes.append(try boundedInt(cdl.u64())) }
        if feature >= 22 {
            for _ in 0..<chunkCount {
                guard try cdl.bytes(16) == Data(repeating: 0, count: 16) else { throw EpicDownloadError.unsupportedManifest }
            }
            for _ in 0..<chunkCount { _ = try cdl.u32() }
            for _ in 0..<chunkCount { _ = try cdl.bytes(16) }
        }
        try cdl.finish()
        var chunks: [String: EpicChunkInfo] = [:]
        for index in 0..<chunkCount {
            guard chunks[guids[index]] == nil else { throw EpicDownloadError.invalidManifest }
            chunks[guids[index]] = EpicChunkInfo(guid: guids[index], rollingHash: rolling[index], sha1: shas[index],
                                                group: groups[index], windowSize: windows[index], fileSize: sizes[index], featureLevel: feature)
        }

        var fml = try reader.section()
        let fileVersion = try fml.u8()
        guard fileVersion <= 2 else { throw EpicDownloadError.unsupportedManifest }
        let fileCount = try fml.count(maximum: 250_000)
        var names: [String] = [], fileHashes: [Data] = [], fileParts: [[EpicChunkPart]] = []
        for _ in 0..<fileCount { names.append(try fml.string()) }
        for _ in 0..<fileCount { guard try fml.string().isEmpty else { throw EpicDownloadError.invalidPath } }
        for _ in 0..<fileCount { fileHashes.append(try fml.bytes(20)) }
        for _ in 0..<fileCount { _ = try fml.u8() }
        for _ in 0..<fileCount {
            let count = try fml.count(maximum: 4096)
            for _ in 0..<count { _ = try fml.string() }
        }
        var totalParts = 0
        for _ in 0..<fileCount {
            let count = try fml.count(maximum: 2_000_000)
            totalParts += count
            guard totalParts <= 2_000_000 else { throw EpicDownloadError.invalidManifest }
            var parts: [EpicChunkPart] = []
            for _ in 0..<count {
                var part = try fml.section()
                parts.append(EpicChunkPart(guid: try part.guid(), offset: Int(try part.u32()), size: Int(try part.u32())))
                try part.finish()
            }
            fileParts.append(parts)
        }
        if fileVersion >= 1 {
            for _ in 0..<fileCount { if try fml.u32() != 0 { _ = try fml.bytes(16) } }
            for _ in 0..<fileCount { _ = try fml.string() }
        }
        if fileVersion >= 2 { for _ in 0..<fileCount { _ = try fml.bytes(32) } }
        try fml.finish()
        var custom = try reader.section()
        guard try custom.u8() == 0 else { throw EpicDownloadError.unsupportedManifest }
        let fields = try custom.count(maximum: 4096)
        for _ in 0..<(fields * 2) { _ = try custom.string() }
        try custom.finish()
        // Feature 22+ may append an empty encrypted-data descriptor even for plain manifests.
        if reader.remaining > 0, feature >= 22 { var extra = try reader.section(); _ = try extra.bytes(extra.remaining) }
        try reader.finish()
        return (app, exe, command, (0..<fileCount).map {
            EpicFileManifest(filename: names[$0], fileHash: fileHashes[$0], chunkParts: fileParts[$0])
        }, chunks)
    }

    private static func validGUID(_ text: String) throws -> String {
        _ = try hexData(text, count: 16)
        return text.uppercased()
    }

    private static func hexData(_ text: String, count: Int) throws -> Data {
        guard text.utf8.count == count * 2, text.utf8.allSatisfy({
            ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 70) || ($0 >= 97 && $0 <= 102)
        }) else { throw EpicDownloadError.invalidManifest }
        var result = Data()
        var index = text.startIndex
        for _ in 0..<count {
            let end = text.index(index, offsetBy: 2)
            guard let value = UInt8(text[index..<end], radix: 16) else { throw EpicDownloadError.invalidManifest }
            result.append(value); index = end
        }
        return result
    }

    private static func blobBytes(_ text: String, count: Int) throws -> Data {
        guard text.utf8.count == count * 3, text.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }) else {
            throw EpicDownloadError.invalidManifest
        }
        let chars = Array(text.utf8)
        var result = Data()
        for index in stride(from: 0, to: chars.count, by: 3) {
            let number = Int(chars[index] - 48) * 100 + Int(chars[index + 1] - 48) * 10 + Int(chars[index + 2] - 48)
            guard number <= 255 else { throw EpicDownloadError.invalidManifest }
            result.append(UInt8(number))
        }
        return result
    }

    private static func blobNumber(_ text: String, bytes: Int) throws -> UInt64 {
        let data = try blobBytes(text, count: bytes)
        return data.enumerated().reduce(UInt64(0)) { $0 | (UInt64($1.element) << ($1.offset * 8)) }
    }

    private static func boundedInt(_ value: UInt64) throws -> Int {
        guard value <= UInt64(Int.max) else { throw EpicDownloadError.invalidManifest }
        return Int(value)
    }
}

struct EpicBinaryReader {
    private let data: Data
    private var offset = 0
    var remaining: Int { data.count - offset }
    init(_ data: Data) { self.data = Data(data) }
    mutating func bytes(_ count: Int) throws -> Data {
        guard count >= 0, count <= remaining else { throw EpicDownloadError.invalidManifest }
        defer { offset += count }
        return data.subdata(in: offset..<(offset + count))
    }
    mutating func u8() throws -> UInt8 { try bytes(1)[0] }
    mutating func u32() throws -> UInt32 {
        try bytes(4).enumerated().reduce(UInt32(0)) { $0 | UInt32($1.element) << ($1.offset * 8) }
    }
    mutating func u64() throws -> UInt64 {
        try bytes(8).enumerated().reduce(UInt64(0)) { $0 | UInt64($1.element) << ($1.offset * 8) }
    }
    mutating func count(maximum: Int) throws -> Int {
        let value = Int(try u32())
        guard value <= maximum, value <= remaining else { throw EpicDownloadError.invalidManifest }
        return value
    }
    mutating func guid() throws -> String {
        var result = ""
        for _ in 0..<4 { result += String(format: "%08X", try u32()) }
        return result
    }
    mutating func string() throws -> String {
        let length = Int(Int32(bitPattern: try u32()))
        if length == 0 { return "" }
        guard length != Int(Int32.min), abs(length) <= 32768 else { throw EpicDownloadError.invalidManifest }
        let unicode = length < 0
        let raw = try bytes(abs(length) * (unicode ? 2 : 1))
        let terminator = unicode ? 2 : 1
        guard raw.suffix(terminator).allSatisfy({ $0 == 0 }),
              let value = String(data: raw.dropLast(terminator), encoding: unicode ? .utf16LittleEndian : .utf8),
              !value.contains("\0") else { throw EpicDownloadError.invalidManifest }
        return value
    }
    mutating func section() throws -> EpicBinaryReader {
        let size = Int(try u32())
        guard size >= 4 else { throw EpicDownloadError.invalidManifest }
        return EpicBinaryReader(try bytes(size - 4))
    }
    mutating func skipTo(_ target: Int) throws {
        guard target >= offset else { throw EpicDownloadError.invalidManifest }
        _ = try bytes(target - offset)
    }
    func finish() throws { guard remaining == 0 else { throw EpicDownloadError.invalidManifest } }
}

enum EpicCompression {
    static func inflate(_ input: Data, expectedSize: Int) throws -> Data {
        guard expectedSize >= 0, expectedSize <= EpicManifest.maximumManifestBytes * 2 else {
            throw EpicDownloadError.invalidManifest
        }
        var output = Data(count: max(1, expectedSize))
        var stream = z_stream()
        guard inflateInit_(&stream, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) == Z_OK else {
            throw EpicDownloadError.invalidChunk
        }
        defer { inflateEnd(&stream) }
        let status = input.withUnsafeBytes { source in
            output.withUnsafeMutableBytes { destination in
                stream.next_in = UnsafeMutablePointer(mutating: source.bindMemory(to: Bytef.self).baseAddress)
                stream.avail_in = uInt(input.count)
                stream.next_out = destination.bindMemory(to: Bytef.self).baseAddress
                stream.avail_out = uInt(destination.count)
                return zlib.inflate(&stream, Z_FINISH)
            }
        }
        guard status == Z_STREAM_END, stream.total_out == expectedSize, stream.avail_in == 0 else {
            throw EpicDownloadError.invalidChunk
        }
        output.count = expectedSize
        return output
    }
}
