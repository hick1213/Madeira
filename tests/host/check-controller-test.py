#!/usr/bin/env python3
"""Run production diagnostic state and library navigation ownership on the host.

These regressions catch dropped short presses, detached-controller ghost input,
wrong/stale test-session tokens, and queued A/LB commands escaping into Library.
GameController and SwiftUI rendering still require the real iPad/device build.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
model_path = ROOT / 'app/Madeira/ControllerTest.swift'
if not model_path.exists():
    raise AssertionError('Controller test is missing: no raw input/session diagnostic model')
model = model_path.read_text().split('// MARK: - Controller test UI', 1)[0]
model = '\n'.join(line for line in model.splitlines() if not line.startswith('import '))
touch = (ROOT / 'app/Madeira/TouchGamepad.swift').read_text()
sample = touch[touch.index('struct GamepadSample'):touch.index('\nenum TouchPadAction')]
library = (ROOT / 'app/Madeira/Library.swift').read_text()
navigation = library[library.index('final class LibraryController:'):library.index('\n/// Which interface Madeira starts with:')]
stubs = '''
import Foundation
import Combine
enum MadeiraConfig {
    static var frontendAllowed = true
    static func flag(_ key: String) -> Bool { frontendAllowed }
}
'''
tests = r'''
func check(_ value: @autoclosure () -> Bool, _ message: String) {
    precondition(value(), message)
}
let id = UUID(), overflowID = UUID(), unsupportedID = UUID()
let token = UUID(), newer = UUID()
var device = ControllerDiagnosticDevice(id: id, name: "Xbox", category: "Xbox",
                                       supported: true, availableButtons: 0xF33F, slot: 0)
var state = ControllerDiagnosticState()
state.replaceDevices([device])
state.record(id: id, raw: GamepadSample(buttons: 0x1000))
check(state.devices[0].raw.buttons == 0, "Hidden diagnostic must not collect input")
state.begin(token)
state.record(id: id, raw: GamepadSample(buttons: 0x1000, lt: 255, lx: -32768, ry: 32767))
state.record(id: id, raw: GamepadSample())
check(state.devices[0].raw == GamepadSample(), "Released input stays neutral")
check(state.devices[0].seenButtons == 0x1000 && state.devices[0].seenLeftTrigger,
      "A press/release between UI ticks remains visible")
state.record(id: UUID(), raw: GamepadSample(buttons: 0x8000))
check(state.devices[0].seenButtons == 0x1000, "Unknown/stale device must not write another device")
state.reset(UUID())
check(state.devices[0].seenButtons == 0x1000, "Wrong owner cannot clear a live test")
state.reset(token)
check(state.devices[0].seenButtons == 0 && !state.devices[0].seenLeftTrigger,
      "Reset clears observation history")
state.record(id: id, raw: GamepadSample(buttons: 0x2000, rt: 200, lx: 100))
state.replaceDevices([])
check(state.devices.count == 1 && !state.devices[0].connected,
      "Disconnected device stays identifiable while test is open")
check(state.devices[0].raw == GamepadSample() && state.devices[0].seenButtons == 0x2000,
      "Disconnect clears live input while preserving the result")
state.record(id: id, raw: GamepadSample(buttons: 0x1000))
check(state.devices[0].raw == GamepadSample(), "Delayed callback cannot resurrect disconnected input")
let overflow = ControllerDiagnosticDevice(id: overflowID, name: "Fifth pad", category: "Generic",
                                         supported: true, availableButtons: 0xFFFF, slot: nil)
let unsupported = ControllerDiagnosticDevice(id: unsupportedID, name: "Other controller", category: "Other",
                                            supported: false, availableButtons: 0, slot: nil)
state.replaceDevices([device, overflow, unsupported])
check(state.devices.count == 3 && state.devices[1].slot == nil && !state.devices[2].supported,
      "Recognized devices without a supported profile or slot remain visible")
check(state.devices[0].seenButtons == 0x2000, "Reconnect retains same test history")
state.begin(newer)
state.end(token)
check(state.owner == newer && state.devices[0].seenButtons == 0,
      "An old page cannot stop or contaminate a new diagnostic session")
state.end(newer)
check(state.owner == nil && state.devices.allSatisfy { $0.raw == GamepadSample() },
      "Leaving the test releases diagnostic ownership and live values")
check(ControllerDiagnosticRoute.resolve(enabled: false, active: true, device: device,
      libraryOwns: true, keyboardMouse: true, hid: true) == .disabled,
      "MADEIRA_XINPUT=0 remains disabled while raw testing")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: false, device: device,
      libraryOwns: false, keyboardMouse: false, hid: false) == .inactive,
      "Background input is neutral")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: unsupported,
      libraryOwns: false, keyboardMouse: false, hid: false) == .unsupported,
      "Unsupported profile must not look like a failed button")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: overflow,
      libraryOwns: false, keyboardMouse: false, hid: false) == .noSlot,
      "A fifth physical controller cannot be mislabeled as player 1")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: device,
      libraryOwns: true, keyboardMouse: true, hid: true) == .library,
      "Library ownership takes precedence over Windows routing")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: device,
      libraryOwns: false, keyboardMouse: true, hid: true) == .keyboardMouse,
      "Keyboard/mouse mode is not incorrectly reported as XInput or HID")
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: device,
      libraryOwns: false, keyboardMouse: false, hid: true) == .hid,
      "HID mode reports the real session path")
device.slot = 1
check(ControllerDiagnosticRoute.resolve(enabled: true, active: true, device: device,
      libraryOwns: true, keyboardMouse: true, hid: true) == .xinput,
      "Player-1-only routing must not mislabel player 2")
let nav = LibraryController()
nav.configure(enabled: true, ownsInput: true)
var commands: [String] = []
let subscription = nav.commands.sink { commands.append($0) }
func drain() { RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.025)) }
nav.sample(buttons: 0x1000, lx: 0, ly: 0) // queued A before sheet presentation
let pause = nav.beginControllerTest()
drain()
check(commands.isEmpty && nav.ownsInput && !nav.acceptsCommands,
      "Already queued A must not open a game while testing")
nav.configure(enabled: false, ownsInput: false)
check(nav.ownsInput, "Testing must continue to neutralize input if configuration changes")
nav.sample(buttons: 0x200, lx: 32767, ly: 0)
drain()
check(commands.isEmpty, "Shoulders and sticks must not switch tabs while testing")
nav.endControllerTest(UUID())
check(nav.ownsInput && !nav.acceptsCommands, "Wrong token cannot restore navigation")
let replacement = nav.beginControllerTest()
nav.endControllerTest(pause)
check(!nav.acceptsCommands, "Stale page disappearance cannot restore a replacement page")
nav.configure(enabled: true, ownsInput: true)
nav.endControllerTest(replacement)
nav.sample(buttons: 0x1000, lx: 0, ly: 0)
drain()
check(commands.isEmpty, "Held test input cannot activate Library after closing")
nav.sample(buttons: 0, lx: 0, ly: 0)
nav.sample(buttons: 0x1000, lx: 0, ly: 0)
drain()
check(commands == ["accept"], "After release, ordinary navigation must resume once")
MadeiraConfig.frontendAllowed = false
let noFrontend = LibraryController()
noFrontend.configure(enabled: true, ownsInput: false)
let disabledPause = noFrontend.beginControllerTest()
check(noFrontend.ownsInput, "Testing remains isolated even with frontend navigation disabled")
noFrontend.endControllerTest(disabledPause)
check(!noFrontend.ownsInput, "The token must not silently enable disabled navigation")
print("PASS: raw press history, reset/disconnect, supported/overflow routing, diagnostic tokens, navigation isolation")
'''
ui_source = model_path.read_text().split('// MARK: - Controller test UI', 1)[1]
observer = ui_source[ui_source.index('@MainActor private final class ControllerTestModel:'):
                     ui_source.index('/// Settings-only testing')]
# The real model owns timer lifetime, main-actor state and stale-response guards.
# Substitute only the asynchronous GameController/Wine I/O boundary; callbacks
# are deliberately delivered late to exercise observable model behavior.
observer_boundary = r'''
var testWineRunning: Int32 = 0
func wine_process_is_running() -> Int32 { testWineRunning }
func wineserver_is_running() -> Int32 { 0 }
final class LibraryModel { static let shared = LibraryModel(); var current: UUID? }
final class LogStore { static let shared = LogStore(); func log(_ value: String) {} }
@MainActor final class GamepadInput {
    static let shared = GamepadInput()
    var pending: [@MainActor @Sendable (ControllerDiagnosticSnapshot) -> Void] = []
    func beginControllerTest(_ token: UUID) {}
    func endControllerTest(_ token: UUID) {}
    func resetControllerTest(_ token: UUID) {}
    func controllerTestSnapshot(_ token: UUID,
        completion: @escaping @MainActor @Sendable (ControllerDiagnosticSnapshot) -> Void) {
        pending.append(completion)
    }
    func takeResponse() -> @MainActor @Sendable (ControllerDiagnosticSnapshot) -> Void {
        pending.removeFirst()
    }
}
'''
observer_tests = r'''
MainActor.assumeIsolated {
    let ui = ControllerTestModel()
    let nav = LibraryController.shared
    nav.configure(enabled: false, ownsInput: false)
    check(ui.begin() && nav.ownsInput, "Opening idle test reserves input ownership")
    let backgroundResponse = GamepadInput.shared.takeResponse()
    ui.setActive(false)
    var shown = ControllerDiagnosticDevice(id: UUID(), name: "Xbox", category: "Xbox",
        supported: true, availableButtons: 0xF33F, slot: 0)
    shown.raw.buttons = 0x1000
    let pressedA = ControllerDiagnosticSnapshot(devices: [shown], enabled: true, active: true)
    backgroundResponse(pressedA)
    check(ui.snapshot.devices.isEmpty,
          "A response queued before backgrounding cannot restore stale pressed values")
    ui.setActive(true)
    GamepadInput.shared.takeResponse()(pressedA)
    check(ui.snapshot.devices.first?.raw.buttons == 0x1000 && ui.selection == shown.id,
          "Foreground response updates the actual UI model and default selection")
    ui.setActive(true)
    let oldPageResponse = GamepadInput.shared.takeResponse()
    ui.end()
    check(!nav.ownsInput, "Closing test releases the navigation token")
    check(ui.begin(), "A later test session can open")
    shown.raw.buttons = 0x8000
    oldPageResponse(ControllerDiagnosticSnapshot(devices: [shown], enabled: true, active: true))
    check(ui.snapshot.devices.first?.raw.buttons == 0x1000,
          "An earlier page's queued response cannot update a replacement session")
    ui.setActive(false)
    let afterCloseResponse = GamepadInput.shared.takeResponse()
    ui.end()
    afterCloseResponse(pressedA)
    check(ui.snapshot.devices.first?.raw == GamepadSample(),
          "Hidden page remains neutral after a queued response")
    testWineRunning = 1
    check(!ui.begin() && !nav.ownsInput,
          "Diagnostics never take game input ownership from an active Windows process")
    testWineRunning = 0
    check(ui.begin(), "Idle diagnostics can start again")
    testWineRunning = 1
    ui.setActive(true)
    check(ui.sessionStarted && !nav.ownsInput,
          "A new Windows session closes observation and releases ownership")
    ui.end()
    testWineRunning = 0
}
print("PASS: actual UI observer lifecycle, background/closed/replaced late responses, active-game exclusion")
'''
with tempfile.TemporaryDirectory(prefix='madeira-controller-test-') as directory:
    source = Path(directory) / 'main.swift'
    executable = Path(directory) / 'check'
    source.write_text(stubs + sample + model + navigation + tests + observer_boundary + observer + observer_tests)
    subprocess.run([os.environ.get('SWIFTC', 'swiftc'), '-swift-version', '5',
                    str(source), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
