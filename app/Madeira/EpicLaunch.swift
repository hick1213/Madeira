// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

/// Cancellation may revoke preparation, but must not delete a credential that
/// Wine has begun using. The native-start claim and revocation share this lock.
final class EpicPreparedLaunch: @unchecked Sendable {
    let identity: UUID
    let arguments: [String]
    let secret: String
    private enum State { case prepared, claimed, finished }
    private let lock = NSLock()
    private var state = State.prepared
    private var ownership: EpicOwnershipTokenFile?

    init(identity: UUID, arguments: [String], secret: String, ownership: EpicOwnershipTokenFile?) {
        self.identity = identity; self.arguments = arguments; self.secret = secret; self.ownership = ownership
    }
    var isClaimed: Bool {
        lock.lock(); defer { lock.unlock() }
        return state == .claimed
    }
    func claimForRuntime() throws {
        lock.lock(); defer { lock.unlock() }
        guard state == .prepared else { throw EpicLaunchArguments.Invalid(reason: "The Epic launch was cancelled. Try again with the current account.") }
        try ownership?.validate()
        state = .claimed
    }
    func cancelBeforeStart() {
        lock.lock(); defer { lock.unlock() }
        guard state == .prepared else { return }
        state = .finished; ownership?.remove(); ownership = nil
    }
    /// Only call after the native start failed, or the session coordinator says
    /// both Wine and its server have ended (debugger detach does not qualify).
    func finish() {
        lock.lock(); defer { lock.unlock() }
        state = .finished; ownership?.remove(); ownership = nil
    }
}

/// The native stderr filter covers Wine/guest output; this covers callbacks
/// and Swift's direct log writer, before either the UI or file receives text.
final class EpicLaunchLogPrivacy: @unchecked Sendable {
    static let shared = EpicLaunchLogPrivacy()
    private let lock = NSLock()
    private var secret: String?
    private var partial: NSRegularExpression?
    private static let field = try? NSRegularExpression(pattern: #"(?i)(AUTH_PASSWORD\s*=\s*)(?:"[^"\r\n]*(?:"|$)|[^\s]*)"#)
    func set(_ value: String?) {
        var regex: NSRegularExpression?
        if let value, !value.isEmpty {
            let prefixes = (1...value.count).reversed().map { NSRegularExpression.escapedPattern(for: String(value.prefix($0))) }.joined(separator: "|")
            regex = try? NSRegularExpression(pattern: "(?<![A-Za-z0-9_.~-])(?:" + prefixes + ")(?=$|[^A-Za-z0-9_.~-])")
        }
        lock.lock(); secret = value; partial = regex; lock.unlock()
    }
    func redact(_ text: String) -> String {
        lock.lock(); let value = secret, partial = partial; lock.unlock()
        guard let value, !value.isEmpty else { return text }
        var result = text.replacingOccurrences(of: value, with: "[redacted]")
        // A fixed-size Wine callback may truncate the credential. Redact the
        // field by its key as well, including a missing closing quote.
        if let partial {
            result = partial.stringByReplacingMatches(in: result, range: NSRange(result.startIndex..., in: result), withTemplate: "[redacted]")
        }
        if let regex = Self.field {
            result = regex.stringByReplacingMatches(in: result, range: NSRange(result.startIndex..., in: result), withTemplate: "$1[redacted]")
        }
        return result
    }
}

enum EpicLaunchArguments {
    struct Invalid: LocalizedError {
        let reason: String
        var errorDescription: String? { reason }
    }

    /// Windows CRT quoting, including backslashes before a quote. The bridge
    /// receives these as a JSON array, so Steam's existing tokenizer is untouched.
    static func tokens(_ text: String) throws -> [String] {
        guard text.utf8.count <= 4096, !text.unicodeScalars.contains(where: { $0.value == 0 || $0.value == 10 || $0.value == 13 }) else {
            throw Invalid(reason: "Epic launch arguments are invalid or too long.")
        }
        let input = Array(text)
        var result: [String] = [], index = 0
        while index < input.count {
            while index < input.count && (input[index] == " " || input[index] == "\t") { index += 1 }
            if index == input.count { break }
            var value = "", quoted = false
            while index < input.count && (quoted || (input[index] != " " && input[index] != "\t")) {
                if input[index] == "\\" {
                    var count = 0
                    while index < input.count && input[index] == "\\" { count += 1; index += 1 }
                    if index < input.count && input[index] == "\"" {
                        value += String(repeating: "\\", count: count / 2)
                        if count % 2 == 1 { value.append("\"") }
                        else { quoted.toggle() }
                        index += 1
                    } else { value += String(repeating: "\\", count: count) }
                } else if input[index] == "\"" {
                    quoted.toggle(); index += 1
                } else { value.append(input[index]); index += 1 }
            }
            guard !quoted else { throw Invalid(reason: "Close the quote in the game's launch arguments.") }
            result.append(value)
            guard result.count <= 48 else { throw Invalid(reason: "Too many Epic launch arguments.") }
        }
        return result
    }

    static func authenticated(command: String, userArguments: String, exchangeCode: String,
                              appName: String, accountID: String, displayName: String,
                              ownershipTokenPath: String? = nil) throws -> [String] {
        var arguments = try tokens(command) + tokens(userArguments)
        let reserved = ["-auth_", "-epicapp", "-epicenv", "-epicuserid", "-epicusername", "-epicportal", "-epiclocale", "-epicovt"]
        guard arguments.count <= 48,
              !arguments.contains(where: { arg in reserved.contains(where: { arg.lowercased().hasPrefix($0) }) }),
              (16...256).contains(exchangeCode.utf8.count),
              exchangeCode.utf8.allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 90) || ($0 >= 97 && $0 <= 122) || $0 == 45 || $0 == 95 }),
              !appName.isEmpty, !accountID.isEmpty,
              [appName, accountID, displayName].allSatisfy({ $0.utf8.count <= 256 && !$0.unicodeScalars.contains(where: { $0.value < 32 }) }) else {
            throw Invalid(reason: "Epic launch metadata or reserved account arguments are invalid.")
        }
        arguments += ["-AUTH_LOGIN=unused", "-AUTH_PASSWORD=" + exchangeCode, "-AUTH_TYPE=exchangecode",
                      "-epicapp=" + appName, "-epicenv=Prod", "-EpicPortal", "-epicusername=" + displayName,
                      "-epicuserid=" + accountID, "-epiclocale=" + Locale.current.identifier.replacingOccurrences(of: "_", with: "-")]
        if let path = ownershipTokenPath {
            let prefix = "C:\\" + EpicOwnershipTokenFile.directoryName + "\\"
            let name = String(path.dropFirst(prefix.count))
            guard path.hasPrefix(prefix), name.count == 40, name.hasSuffix(".ovt"),
                  UUID(uuidString: String(name.dropLast(4))) != nil else {
                throw Invalid(reason: "Epic ownership token path is invalid.")
            }
            arguments.append("-epicovt=" + path)
        }
        return arguments
    }
}
