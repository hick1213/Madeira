import Foundation

enum GlobalPerformanceOverlay {
    static let enabledKey = "madeiraPerformanceOverlayEnabled"
    static let fieldsKey = "madeiraPerformanceOverlayFields"
    static let allowedFields = ["FPS", "Frame time", "CPU", "GPU", "RAM", "Battery", "Thermal"]
    static let defaultFields = ["FPS", "Frame time", "RAM"]
    private static let maxEncodedBytes = 256
    private static let maxFieldCount = 64

    struct Selection: Equatable, Sendable {
        let enabled: Bool
        let fields: [String]
    }

    /// Read at launch. In-game changes apply only to that session.
    static func read(defaults: UserDefaults = .standard) -> Selection {
        Selection(enabled: defaults.bool(forKey: enabledKey),
                  fields: decode(defaults.string(forKey: fieldsKey) ?? ""))
    }

    static func normalize(_ fields: [String]) -> [String] {
        guard fields.count <= maxFieldCount,
              fields.allSatisfy({ field in
                  field.utf8.count <= 64 && !field.utf8.contains(where: { $0 < 32 || $0 == 127 })
              }) else { return defaultFields }
        let result = allowedFields.filter { fields.contains($0) }
        return result.isEmpty ? defaultFields : result
    }

    static func decode(_ stored: String) -> [String] {
        guard !stored.isEmpty, stored.utf8.count <= maxEncodedBytes else { return defaultFields }
        let fields = stored.split(separator: "|", omittingEmptySubsequences: false).map(String.init)
        guard !fields.contains("") else { return defaultFields }
        return normalize(fields)
    }

    static func encode(_ fields: [String]) -> String {
        normalize(fields).joined(separator: "|")
    }

    static func settingField(_ field: String, enabled: Bool, in fields: [String]) -> [String] {
        let current = normalize(fields)
        guard allowedFields.contains(field) else { return current }
        if enabled { return normalize(current + [field]) }
        if !enabled && current.count == 1 { return current }
        return normalize(current.filter { $0 != field })
    }
}

struct FrameRateSample: Equatable, Sendable {
    let fps: Double
    let frames: UInt64
    let elapsed: Double
}

/// Samples submitted game frames using a monotonic clock such as CACurrentMediaTime.
struct PerformanceFrameSampler: Sendable {
    private var previous: (count: UInt64, time: Double)?

    mutating func reset(count: UInt64, time: Double) {
        previous = time.isFinite && time > 0 ? (count, time) : nil
    }

    mutating func sample(count: UInt64, time: Double) -> FrameRateSample? {
        guard time.isFinite, time > 0 else {
            previous = nil
            return nil
        }
        guard let previous, time > previous.time, count >= previous.count else {
            reset(count: count, time: time)
            return nil
        }
        let frames = count - previous.count
        let elapsed = time - previous.time
        let fps = Double(frames) / elapsed
        reset(count: count, time: time)
        guard elapsed.isFinite, elapsed > 0, fps.isFinite else { return nil }
        return FrameRateSample(fps: fps, frames: frames, elapsed: elapsed)
    }
}

#if canImport(UIKit)
import SwiftUI

struct PerformanceOverlaySettingsSection: View {
    @AppStorage(GlobalPerformanceOverlay.enabledKey) private var enabled = false
    @AppStorage(GlobalPerformanceOverlay.fieldsKey) private var encodedFields = "FPS|Frame time|RAM"

    private var fields: [String] { GlobalPerformanceOverlay.decode(encodedFields) }

    var body: some View {
        Section {
            Toggle("Show performance information", isOn: $enabled)
            if enabled {
                ForEach(GlobalPerformanceOverlay.allowedFields, id: \.self) { field in
                    Toggle(field, isOn: Binding(get: { fields.contains(field) }, set: { on in
                        encodedFields = GlobalPerformanceOverlay.encode(
                            GlobalPerformanceOverlay.settingField(field, enabled: on, in: fields))
                    }))
                    .disabled(fields.count == 1 && fields.contains(field))
                }
            }
        } header: {
            Text("Performance information")
        } footer: {
            Text("Applies to every game at its next launch. Changes in the game menu last for that session. FPS measures the game's submitted frames and does not include generated frames; frame time is the average interval between submissions. CPU and RAM refer to the Madeira process. GPU is an estimate from this app's command-buffer busy time, not system GPU utilization.")
        }
    }
}

struct PerformanceOverlaySettingsPage: View {
    var body: some View {
        Form { PerformanceOverlaySettingsSection() }
            .navigationTitle("Performance information")
    }
}
#endif
