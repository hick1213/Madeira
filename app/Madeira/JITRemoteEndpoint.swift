// SPDX-License-Identifier: GPL-3.0-or-later
// Madeira Converter Exception: see LICENSE-EXCEPTION.md

import Foundation

/// iOS can move Remote Pairing away from 49152. Bonjour identifies the current
/// port; the pairing record's IRK authenticates the peer before that port is used
/// with LocalDevVPN. Never connect to an advertised device's LAN address.
@MainActor enum JITRemoteEndpoint {
    static let defaultPort: UInt16 = 49152
    private static var active: [UUID: Lookup] = [:]

    /// Pure selection, also exercised with the upstream authenticated-peer vector.
    nonisolated static func matchedPort(pairingData: Data, txt: [String: Data], port: Int) -> UInt16? {
        guard let port = UInt16(exactly: port), port != 0,
              let key = peerKey(pairingData),
              let identifierBytes = txt["identifier"], let tagBytes = txt["authTag"],
              let identifier = String(data: identifierBytes, encoding: .utf8),
              let tag = String(data: tagBytes, encoding: .utf8),
              !identifier.utf8.contains(0), !tag.utf8.contains(0) else { return nil }
        let matches = key.withUnsafeBytes { bytes in
            identifier.withCString { identifier in
                tag.withCString { tag in
                    madeira_rppairing_validate_auth_tag(bytes.bindMemory(to: UInt8.self).baseAddress,
                                                       key.count, identifier, tag) == 1
                }
            }
        }
        return matches ? port : nil
    }

    nonisolated private static func peerKey(_ pairingData: Data) -> Data? {
        guard let plist = try? PropertyListSerialization.propertyList(from: pairingData, format: nil),
              let dictionary = plist as? [String: Any],
              let key = dictionary["alt_irk"] as? Data, key.count == 16 else { return nil }
        return key
    }

    static func resolve(pairingData: Data, completion: @escaping (UInt16) -> Void) {
        // Older imported records can lack the IRK. Retain the existing default
        // path rather than selecting another peer or replacing their credential.
        guard peerKey(pairingData) != nil else {
            completion(defaultPort)
            return
        }
        let lookup = Lookup(pairingData: pairingData) { id, port in
            active.removeValue(forKey: id)
            completion(port)
        }
        active[lookup.id] = lookup
        lookup.start()
    }

    @MainActor private final class Lookup: NSObject, @preconcurrency NetServiceBrowserDelegate, @preconcurrency NetServiceDelegate {
        let id = UUID()
        private let pairingData: Data
        private var completion: ((UUID, UInt16) -> Void)?
        private let browser = NetServiceBrowser()
        private var services: [NetService] = []
        private var deadline: Timer?

        init(pairingData: Data, completion: @escaping (UUID, UInt16) -> Void) {
            self.pairingData = pairingData
            self.completion = completion
            super.init()
        }

        func start() {
            browser.delegate = self
            browser.includesPeerToPeer = true
            // Bounded even when Wi-Fi, Bonjour or Local Network access is absent.
            deadline = Timer.scheduledTimer(withTimeInterval: 4, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated { self?.finish(defaultPort) }
            }
            browser.searchForServices(ofType: "_remotepairing._tcp.", inDomain: "local.")
        }

        func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
            guard completion != nil else { return }
            service.delegate = self
            service.includesPeerToPeer = true
            services.append(service)
            service.resolve(withTimeout: 2)
        }

        func netServiceDidResolveAddress(_ sender: NetService) {
            guard completion != nil, let data = sender.txtRecordData(),
                  let port = matchedPort(pairingData: pairingData,
                                         txt: NetService.dictionary(fromTXTRecord: data), port: sender.port) else { return }
            finish(port)
        }

        func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
            finish(defaultPort)
        }

        private func finish(_ port: UInt16) {
            guard let completion else { return }
            self.completion = nil
            deadline?.invalidate()
            deadline = nil
            browser.delegate = nil
            browser.stop()
            for service in services {
                service.delegate = nil
                service.stop()
            }
            services.removeAll()
            completion(id, port)
        }
    }
}
