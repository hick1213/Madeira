#!/usr/bin/env python3
"""Execute the phone session orientation policy with UIKit I/O test doubles.

This checks transitions and scene selection, not actual device rotation.
"""

from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
app = (ROOT / "app/Madeira/MadeiraApp.swift").read_text()
content = (ROOT / "app/Madeira/ContentView.swift").read_text()
start = app.index("@MainActor enum PhoneSessionOrientation {")
end = app.index("\nfinal class MadeiraAppDelegate", start)
policy = app[start:end]

STUBS = r"""
import Foundation
struct UIInterfaceOrientationMask: OptionSet, Equatable {
    let rawValue: Int
    static let portrait = Self(rawValue: 1)
    static let landscapeLeft = Self(rawValue: 2)
    static let landscapeRight = Self(rawValue: 4)
    static let landscape: Self = [.landscapeLeft, .landscapeRight]
    static let allButUpsideDown: Self = [.portrait, .landscape]
    static let all = Self(rawValue: 15)
}
enum UIInterfaceOrientation {
    case unknown, portrait, landscapeLeft, landscapeRight
    var isLandscape: Bool { self == .landscapeLeft || self == .landscapeRight }
}
enum Idiom { case phone, pad }
@MainActor final class UIDevice {
    static let current = UIDevice()
    var userInterfaceIdiom = Idiom.phone
}
class UIScene {
    enum State { case foregroundActive, background }
    var activationState = State.foregroundActive
}
class UIViewController {
    var updates = 0
    func setNeedsUpdateOfSupportedInterfaceOrientations() { updates += 1 }
}
class UIWindow { var rootViewController: UIViewController? = UIViewController() }
class UIWindowScene: UIScene {
    struct GeometryPreferences {
        let mask: UIInterfaceOrientationMask
        static func iOS(interfaceOrientations: UIInterfaceOrientationMask) -> Self {
            Self(mask: interfaceOrientations)
        }
    }
    var interfaceOrientation = UIInterfaceOrientation.portrait
    var windows = [UIWindow()]
    var requests: [UIInterfaceOrientationMask] = []
    var failure: Error?
    func requestGeometryUpdate(_ geometry: GeometryPreferences, errorHandler: (Error) -> Void) {
        requests.append(geometry.mask)
        if let failure { errorHandler(failure) }
    }
}
@MainActor final class UIApplication {
    static let shared = UIApplication()
    var connectedScenes: [UIScene] = []
}
@MainActor final class LogStore {
    static let shared = LogStore()
    var lines: [String] = []
    func log(_ line: String) { lines.append(line) }
}
"""

CHECKS = r"""
@main struct Test {
    @MainActor static func main() {
        func expect(_ value: Bool, _ message: String) {
            if !value { fatalError(message) }
            print("PASS: " + message)
        }
        expect(PhoneSessionOrientation.supported(phone: true, session: true) == .landscape,
               "phone session supports landscape only")
        expect(PhoneSessionOrientation.supported(phone: true, session: false) == .allButUpsideDown,
               "phone library restores normal support")
        expect(PhoneSessionOrientation.supported(phone: false, session: true) == .allButUpsideDown &&
               PhoneSessionOrientation.supported(phone: false, session: false) == .allButUpsideDown,
               "tablet support preserves the existing plist orientations")
        let inactive = UIWindowScene()
        inactive.activationState = .background
        let active = UIWindowScene()
        UIApplication.shared.connectedScenes = [inactive, UIScene(), active]
        UIDevice.current.userInterfaceIdiom = .pad
        PhoneSessionOrientation.update(session: true)
        expect(active.requests.isEmpty && active.windows[0].rootViewController!.updates == 0,
               "tablet never requests rotation or controller updates")
        UIDevice.current.userInterfaceIdiom = .phone
        PhoneSessionOrientation.update(session: false)
        expect(active.requests.isEmpty, "idle phone does not force portrait")
        PhoneSessionOrientation.update(session: true)
        expect(active.requests == [.landscape] && inactive.requests.isEmpty,
               "phone starts in the foreground scene only")
        active.interfaceOrientation = .landscapeLeft
        PhoneSessionOrientation.update(session: true)
        expect(active.requests.count == 1, "foreground resume does not overwrite saved portrait")
        PhoneSessionOrientation.update(session: false)
        expect(active.requests == [.landscape, .portrait], "exit restores pre-session portrait")
        PhoneSessionOrientation.update(session: false)
        expect(active.requests.count == 2, "idle updates do not repeat restoration")
        active.interfaceOrientation = .landscapeRight
        PhoneSessionOrientation.update(session: true)
        PhoneSessionOrientation.update(session: false)
        expect(active.requests.last == .landscapeRight, "exit preserves previous landscape side")
        UIApplication.shared.connectedScenes = [inactive]
        PhoneSessionOrientation.update(session: true)
        expect(inactive.requests.isEmpty, "background scene is never rotated")
        UIApplication.shared.connectedScenes = [active]
        active.interfaceOrientation = .portrait
        active.failure = NSError(domain: "test", code: 1)
        PhoneSessionOrientation.update(session: true)
        expect(LogStore.shared.lines.last?.contains("request failed") == true,
               "system geometry rejection is recorded")
        PhoneSessionOrientation.update(session: false)
    }
}
"""

with tempfile.TemporaryDirectory(prefix="madeira-phone-orientation-") as temp:
    source = Path(temp) / "main.swift"
    executable = Path(temp) / "check"
    source.write_text(STUBS + policy + CHECKS)
    subprocess.run(["swiftc", "-parse-as-library", str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)

if "@UIApplicationDelegateAdaptor(MadeiraAppDelegate.self)" not in app:
    raise SystemExit("FAIL: application orientation delegate is not wired")
if ".onChange(of: library.current)" not in content:
    raise SystemExit("FAIL: session lifecycle is not wired")
if content.count("PhoneSessionOrientation.update(session:") != 3:
    raise SystemExit("FAIL: appearance, session changes and foreground resume must update policy")
if not re.search(
    r"\.onChange\(of: library\.current\) \{ _, current in\s*"
    r"PhoneSessionOrientation\.update\(session: current != nil\)", content
):
    raise SystemExit("FAIL: session transition must use the current session value")
if not re.search(
    r"\.onAppear \{\s*PhoneSessionOrientation\.update\(session: library\.current != nil\)", content
):
    raise SystemExit("FAIL: initial appearance must use the real session state")
foreground = content[content.index("for: UIApplication.didBecomeActiveNotification"):]
foreground = foreground[:foreground.index(".onAppear {")]
if "PhoneSessionOrientation.update(session: library.current != nil)" not in foreground:
    raise SystemExit("FAIL: foreground resume must use the real session state")
delegate = app[app.index("final class MadeiraAppDelegate"):app.index("@main")]
if not re.search(
    r"PhoneSessionOrientation\.supported\(phone: UIDevice\.current\.userInterfaceIdiom == \.phone,\s*"
    r"session: LibraryModel\.shared\.current != nil\)", delegate
):
    raise SystemExit("FAIL: delegate must pass the real idiom and session state")
print("PASS: application delegate and session lifecycle wiring")
