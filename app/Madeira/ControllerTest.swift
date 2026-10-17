import Foundation
import SwiftUI

/// Values copied from GamepadInput's existing sampler; no GameController object
/// or callback crosses into the diagnostic UI.
struct ControllerDiagnosticDevice: Identifiable, Equatable, Sendable {
    var id: UUID
    var name: String
    var category: String
    var supported: Bool
    var availableButtons: UInt16
    var slot: Int?
    var connected = true
    var raw = GamepadSample()
    var seenButtons: UInt16 = 0
    var seenLeftTrigger = false
    var seenRightTrigger = false
    var route: ControllerDiagnosticRoute = .disconnected
    var transmitted: GamepadSample?
    var packet: UInt32 = 0
}

enum ControllerDiagnosticRoute: String, Sendable {
    case disconnected, disabled, unsupported, noSlot, inactive, library, keyboardMouse, hid, xinput
    static func resolve(enabled: Bool, active: Bool, device: ControllerDiagnosticDevice,
                        libraryOwns: Bool, keyboardMouse: Bool, hid: Bool) -> Self {
        guard device.connected else { return .disconnected }
        guard enabled else { return .disabled }
        guard device.supported else { return .unsupported }
        guard let slot = device.slot else { return .noSlot }
        guard active else { return .inactive }
        if slot == 0 {
            if libraryOwns { return .library }
            if keyboardMouse { return .keyboardMouse }
            if hid { return .hid }
        }
        return .xinput
    }
    var explanation: String {
        switch self {
        case .disconnected: return "Controller disconnected"
        case .disabled: return "Controller translation is disabled by MADEIRA_XINPUT=0"
        case .unsupported: return "The system recognizes this device but supplies no extended gamepad profile"
        case .noSlot: return "All four Windows controller slots are already occupied"
        case .inactive: return "Madeira is inactive; Windows input is neutral"
        case .library: return "Library owns player 1; Windows input stays neutral during this test"
        case .keyboardMouse: return "Player 1 is translated to keyboard and mouse"
        case .hid: return "Player 1 uses the Windows HID controller"
        case .xinput: return "This controller uses Windows XInput"
        }
    }
}

struct ControllerDiagnosticSnapshot: Equatable, Sendable {
    var devices: [ControllerDiagnosticDevice] = []
    var enabled = true
    var active = false
    var configuredMode = "xinput"
}

/// Queue-owned observation history. Short presses are remembered at the input
/// sampler's rate, rather than depending on the UI's slower refresh rate.
struct ControllerDiagnosticState {
    private(set) var owner: UUID?
    private(set) var devices: [ControllerDiagnosticDevice] = []
    mutating func begin(_ token: UUID) {
        owner = token
        devices.removeAll { !$0.connected }
        clearValues(history: true)
    }
    mutating func end(_ token: UUID) {
        guard owner == token else { return }
        owner = nil
        clearValues(history: true)
        devices.removeAll { !$0.connected }
    }
    mutating func reset(_ token: UUID) {
        guard owner == token else { return }
        for i in devices.indices {
            devices[i].seenButtons = 0
            devices[i].seenLeftTrigger = false
            devices[i].seenRightTrigger = false
        }
    }
    mutating func replaceDevices(_ current: [ControllerDiagnosticDevice]) {
        let old = devices
        devices = current.map { device in
            guard owner != nil, let previous = old.first(where: { $0.id == device.id }) else { return device }
            var result = device
            result.raw = previous.connected ? previous.raw : GamepadSample()
            result.seenButtons = previous.seenButtons
            result.seenLeftTrigger = previous.seenLeftTrigger
            result.seenRightTrigger = previous.seenRightTrigger
            return result
        }
        if owner != nil {
            // Keep recent disconnections visible without growing an unbounded
            // history when devices reconnect repeatedly during a long test.
            let removed = old.filter { old in !current.contains(where: { $0.id == old.id }) }.suffix(8)
            for var device in removed {
                device.connected = false
                device.raw = GamepadSample()
                device.transmitted = nil
                device.route = .disconnected
                devices.append(device)
            }
        }
    }
    mutating func record(id: UUID, raw: GamepadSample) {
        guard owner != nil, let i = devices.firstIndex(where: { $0.id == id && $0.connected && $0.supported && $0.slot != nil }) else { return }
        devices[i].raw = raw
        devices[i].seenButtons |= raw.buttons
        devices[i].seenLeftTrigger = devices[i].seenLeftTrigger || raw.lt > 0
        devices[i].seenRightTrigger = devices[i].seenRightTrigger || raw.rt > 0
    }
    private mutating func clearValues(history: Bool) {
        for i in devices.indices {
            devices[i].raw = GamepadSample()
            devices[i].transmitted = nil
            if history {
                devices[i].seenButtons = 0
                devices[i].seenLeftTrigger = false
                devices[i].seenRightTrigger = false
            }
        }
    }
}

// MARK: - Controller test UI

@MainActor private final class ControllerTestModel: ObservableObject {
    @Published var snapshot = ControllerDiagnosticSnapshot()
    @Published var selection: UUID?
    @Published var sessionStarted = false
    private var token: UUID?
    private var timer: Timer?
    private var observationGeneration: UInt64 = 0
    static var available: Bool {
        LibraryModel.shared.current == nil && wine_process_is_running() == 0 && wineserver_is_running() == 0
    }
    func begin() -> Bool {
        guard Self.available else { return false }
        guard token == nil else { return true }
        let token = LibraryController.shared.beginControllerTest()
        self.token = token
        GamepadInput.shared.beginControllerTest(token)
        LogStore.shared.log("[controller-test] opened; Windows session is idle")
        setActive(true)
        return true
    }
    func setActive(_ active: Bool) {
        timer?.invalidate(); timer = nil
        observationGeneration &+= 1
        guard active, token != nil else {
            for i in snapshot.devices.indices {
                snapshot.devices[i].raw = GamepadSample()
                snapshot.devices[i].transmitted = nil
            }
            return
        }
        refresh()
        guard token != nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 15, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }
    private func refresh() {
        guard let token else { return }
        guard Self.available else { sessionStarted = true; end(); return }
        let generation = observationGeneration
        GamepadInput.shared.controllerTestSnapshot(token) { [weak self] value in
            guard let self, self.token == token, self.observationGeneration == generation else { return }
            let old = self.snapshot
            self.snapshot = value
            if self.selection == nil { self.selection = value.devices.first?.id }
            let connected = value.devices.filter(\.connected)
            if old.devices.map(\.id) != value.devices.map(\.id) || old.devices.map(\.connected) != value.devices.map(\.connected) {
                LogStore.shared.log("[controller-test] connected=\(connected.count) supported=\(connected.filter(\.supported).count) enabled=\(value.enabled ? 1 : 0)")
            }
            if let selected = value.devices.first(where: { $0.id == self.selection }),
               let previous = old.devices.first(where: { $0.id == selected.id }), selected.seenButtons != previous.seenButtons {
                LogStore.shared.log("[controller-test] slot=\(selected.slot.map(String.init) ?? "none") seen-buttons=\(String(selected.seenButtons, radix: 16))")
            }
        }
    }
    func reset() { if let token { GamepadInput.shared.resetControllerTest(token) } }
    func end() {
        timer?.invalidate(); timer = nil
        observationGeneration &+= 1
        guard let token else { return }
        self.token = nil
        GamepadInput.shared.endControllerTest(token)
        LibraryController.shared.endControllerTest(token)
        LogStore.shared.log("[controller-test] closed")
    }
}

/// Settings-only testing deliberately starts neither Wine nor JIT.
struct ControllerTestView: View {
    @StateObject private var model = ControllerTestModel()
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    private var selected: ControllerDiagnosticDevice? { model.snapshot.devices.first { $0.id == model.selection } }
    private static let buttons: [(String, UInt16)] = [
        ("A", 0x1000), ("B", 0x2000), ("X", 0x4000), ("Y", 0x8000),
        ("D↑", 1), ("D↓", 2), ("D←", 4), ("D→", 8),
        ("LB", 0x100), ("RB", 0x200), ("L3", 0x40), ("R3", 0x80),
        ("Menu", 0x10), ("View", 0x20), ("Guide", 0x400)
    ]
    var body: some View {
        Form {
            Section {
                if model.snapshot.devices.isEmpty {
                    ContentUnavailableView("No controller detected", systemImage: "gamecontroller",
                        description: Text("Connect your controller, then press a button. If the system responds but no device appears here, check the controller in system Settings."))
                } else {
                    Picker("Controller", selection: $model.selection) {
                        ForEach(model.snapshot.devices) { device in
                            Text(device.name + (device.connected ? "" : " (disconnected)")).tag(Optional(device.id))
                        }
                    }
                    LabeledContent("Connected devices", value: String(model.snapshot.devices.filter(\.connected).count))
                    LabeledContent("Translation", value: model.snapshot.enabled ? "Enabled" : "Disabled")
                }
            } header: { Text("Device") }
            if let device = selected {
                Section {
                    LabeledContent("Profile", value: device.supported ? "Extended gamepad" : "Unsupported")
                    LabeledContent("Category", value: device.category)
                    LabeledContent("Windows player", value: device.slot.map { String($0 + 1) } ?? "Unassigned")
                    Text(device.route.explanation).font(.subheadline).foregroundStyle(.secondary)
                } header: { Text("Connection") }
                if device.connected && device.supported && device.slot != nil {
                    Section {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 62))], spacing: 12) {
                            ForEach(Self.buttons, id: \.1) { name, mask in
                                button(name, mask: mask, device: device)
                            }
                        }.padding(.vertical, 8)
                    } header: { Text("Buttons") } footer: {
                        Text("Filled: pressed now. Outlined: seen during this test. Grey: not supplied by this controller. Some buttons may be reserved by the system.")
                    }
                    Section {
                        HStack {
                            stick("Left", x: device.raw.lx, y: device.raw.ly)
                            Spacer()
                            stick("Right", x: device.raw.rx, y: device.raw.ry)
                        }.padding(.vertical, 8)
                        trigger("LT", value: device.raw.lt, seen: device.seenLeftTrigger)
                        trigger("RT", value: device.raw.rt, seen: device.seenRightTrigger)
                    } header: { Text("Sticks & triggers") }
                }
                Section {
                    LabeledContent("Configured mode", value: model.snapshot.configuredMode)
                    if let transmitted = device.transmitted {
                        LabeledContent("Snapshot packet", value: String(device.packet))
                        LabeledContent("Buttons", value: String(format: "0x%04X", transmitted.buttons))
                        Text("LT \(transmitted.lt) · RT \(transmitted.rt) · LX \(transmitted.lx) · LY \(transmitted.ly) · RX \(transmitted.rx) · RY \(transmitted.ry)")
                            .font(.caption.monospacedDigit())
                    } else { Text("No controller snapshot on this route").foregroundStyle(.secondary) }
                } header: { Text("Windows transport") } footer: {
                    Text("This reads Madeira's published snapshot. No game is running, so it does not verify a game's controller support. The configured mode applies when the next Windows session starts.")
                }
            }
        }
        .navigationTitle("Controller test")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
            ToolbarItem(placement: .primaryAction) { Button("Reset") { model.reset() }.disabled(selected == nil) }
        }
        .onAppear { if !model.begin() { dismiss() } }
        .onDisappear { model.end() }
        .onChange(of: scenePhase) { _, phase in model.setActive(phase == .active) }
        .onChange(of: model.sessionStarted) { _, started in if started { dismiss() } }
        .modifier(ControllerTestEventClaim())
    }
    private func button(_ name: String, mask: UInt16, device: ControllerDiagnosticDevice) -> some View {
        let available = device.availableButtons & mask != 0
        let down = device.raw.buttons & mask != 0
        let seen = device.seenButtons & mask != 0
        return Text(name).font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 40)
            .foregroundStyle(available ? (down ? Color.white : Color.primary) : Color.secondary)
            .background(down ? Color.accentColor : Color(uiColor: .tertiarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(seen ? Color.accentColor : .clear, lineWidth: 2))
            .accessibilityLabel(name).accessibilityValue(!available ? "Unsupported" : down ? "Pressed" : seen ? "Seen" : "Not yet pressed")
    }
    private func stick(_ label: String, x: Int16, y: Int16) -> some View {
        VStack(spacing: 6) {
            Text(label).font(.subheadline)
            ZStack {
                Circle().stroke(Color.secondary.opacity(0.4), lineWidth: 1)
                Rectangle().fill(Color.secondary.opacity(0.2)).frame(width: 1)
                Rectangle().fill(Color.secondary.opacity(0.2)).frame(height: 1)
                Circle().fill(Color.accentColor).frame(width: 14, height: 14)
                    .offset(x: CGFloat(x) / (x < 0 ? 32768 : 32767) * 40,
                            y: -CGFloat(y) / (y < 0 ? 32768 : 32767) * 40)
            }.frame(width: 100, height: 100)
            Text("X \(x)  Y \(y)").font(.caption.monospacedDigit())
        }.accessibilityElement(children: .combine)
    }
    private func trigger(_ label: String, value: UInt8, seen: Bool) -> some View {
        HStack {
            Text(label).frame(width: 28, alignment: .leading)
            ProgressView(value: Double(value), total: 255).tint(seen ? .accentColor : .secondary)
            Image(systemName: seen ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(seen ? Color.accentColor : Color.secondary)
                .accessibilityLabel(seen ? "Seen" : "Not yet pressed")
            Text(String(value)).font(.caption.monospacedDigit()).frame(width: 30, alignment: .trailing)
        }.accessibilityElement(children: .combine)
    }
}

private struct ControllerTestEventClaim: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) { content.handlesGameControllerEvents(matching: .gamepad) }
        else { content }
    }
}
