// SPDX-License-Identifier: GPL-3.0-or-later
// Madeira Converter Exception: see LICENSE-EXCEPTION.md

import Foundation

/// Codable messages shared by Madeira and its JIT helper extension. Each travels as JSON
/// in an extension request's item (userInfo[itemKey]), to the helper and back.
struct MadeiraJITRequest: Codable, Sendable {
    static let itemKey = "madeira-jit-request"

    enum Operation: String, Codable, Sendable {
        case prepare
        case enable
        case resetDDI
    }

    let operation: Operation
    let targetPID: Int32?
    let pairingData: Data?
    let scriptBase64: String?
    let rsdPort: UInt16?

    static func prepare(pairingData: Data, rsdPort: UInt16 = 49152) -> MadeiraJITRequest {
        MadeiraJITRequest(operation: .prepare, targetPID: nil,
                          pairingData: pairingData, scriptBase64: nil, rsdPort: rsdPort)
    }

    static func enable(targetPID: Int32, pairingData: Data,
                       scriptBase64: String, rsdPort: UInt16 = 49152) -> MadeiraJITRequest {
        MadeiraJITRequest(operation: .enable, targetPID: targetPID,
                          pairingData: pairingData, scriptBase64: scriptBase64, rsdPort: rsdPort)
    }

    static var resetDDI: MadeiraJITRequest {
        MadeiraJITRequest(operation: .resetDDI, targetPID: nil,
                          pairingData: nil, scriptBase64: nil, rsdPort: nil)
    }

    struct Response: Codable, Sendable {
        static let itemKey = "madeira-jit-response"
        let success: Bool
        let message: String
        let txmPresent: Bool?
    }
}
