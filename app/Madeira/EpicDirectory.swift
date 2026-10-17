// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Darwin

/// The app container comes from the OS, not from an install or manifest path.
/// Pin that permitted directory, then reject links in every writable component.
enum EpicDirectory {
    static let applicationContainer = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)

    enum Failure: Error {
        case unsafePath
        case operation(step: String, code: Int32)

        var diagnostic: String {
            switch self {
            case .unsafePath: return "unsafe-path"
            case let .operation(step, code): return "step=\(step) errno=\(code)"
            }
        }
    }

    private static func path(_ url: URL) throws -> (String, [String]) {
        guard url.isFileURL else { throw Failure.unsafePath }
        var path = url.path(percentEncoded: false)
        guard path.hasPrefix("/"), !path.contains("\0") else { throw Failure.unsafePath }
        // /var is a protected system alias; do not resolve user-controlled paths.
        if path == "/var" || path.hasPrefix("/var/") { path = "/private" + path }
        let components = path.split(separator: "/").map(String.init)
        guard !components.isEmpty, components.count <= 64,
              !components.contains("."), !components.contains("..") else { throw Failure.unsafePath }
        return (path, components)
    }

    static func openDirectory(at target: URL, trustedContainer: URL, create: Bool) throws -> Int32 {
        let (containerPath, containerParts) = try path(trustedContainer)
        let (_, targetParts) = try path(target)
        guard targetParts.starts(with: containerParts) else { throw Failure.unsafePath }
        var fd = Darwin.open(containerPath, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw Failure.operation(step: "open-container", code: errno) }
        do {
            var status = stat()
            guard fstat(fd, &status) == 0, status.st_mode & S_IFMT == S_IFDIR else { throw Failure.unsafePath }
            for (index, component) in targetParts.dropFirst(containerParts.count).enumerated() {
                if create, mkdirat(fd, component, 0o700) != 0, errno != EEXIST {
                    throw Failure.operation(step: "create-directory-\(index)", code: errno)
                }
                let next = openat(fd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw Failure.operation(step: "open-directory-\(index)", code: errno) }
                close(fd)
                fd = next
            }
            return fd
        } catch {
            close(fd)
            throw error
        }
    }
}
