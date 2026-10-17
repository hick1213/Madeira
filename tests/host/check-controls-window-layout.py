#!/usr/bin/env python3
"""Run production overlay sizing and menu placement with UIKit I/O doubles.

This covers a scene resize without a UIDevice notification. It does not replace
the device screenshot check for UIKit/SwiftUI rendering and safe-area delivery.
"""

from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
content = (ROOT / "app/Madeira/ContentView.swift").read_text()
library = (ROOT / "app/Madeira/Library.swift").read_text()


def block(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


host = block(content, "enum TouchControlsHost {")
surface = block(content, "    override func layoutSubviews() {")
position = block(library, "    private func position(_ translation: CGSize) -> CGPoint {")

stubs = r"""
import Foundation
import CoreGraphics
struct UIColor { static let clear = UIColor() }
extension Double { static let normal = 0.0 }
class UIView {
    var backgroundColor: UIColor?
    var bounds = CGRect.zero
    weak var window: UIWindow?
    var superclassLayouts = 0
    func layoutSubviews() { superclassLayouts += 1 }
    func convert(_ bounds: CGRect, to: UIWindow) -> CGRect { bounds }
}
class UIViewController { let view = UIView() }
class UIHostingController<Content>: UIViewController {
    init(rootView: Content) { super.init() }
}
class UIScene {
    enum State { case foregroundActive, background }
    var activationState = State.foregroundActive
}
final class UIWindowScene: UIScene {
    struct Coordinates { var bounds: CGRect }
    var coordinateSpace = Coordinates(bounds: CGRect(x: 0, y: 0, width: 430, height: 932))
}
class UIWindow: UIView {
    static var created: [UIWindow] = []
    weak var windowScene: UIWindowScene?
    var frameWrites = 0
    var frame = CGRect.zero {
        didSet { frameWrites += 1; bounds = CGRect(origin: .zero, size: frame.size) }
    }
    var windowLevel = 0.0
    var isHidden = true
    var rootViewController: UIViewController?
    init(windowScene: UIWindowScene) {
        self.windowScene = windowScene
        super.init()
        UIWindow.created.append(self)
    }
}
final class ControlsWindow: UIWindow {}
final class UIApplication {
    static let shared = UIApplication()
    var connectedScenes: [UIScene] = []
}
final class UIDevice {
    static let current = UIDevice()
    var orientationNotificationStarts = 0
    func beginGeneratingDeviceOrientationNotifications() { orientationNotificationStarts += 1 }
}
final class TouchControlsModel {
    static let shared = TouchControlsModel()
    var controls: [Int] = []
}
struct TouchControlsOverlay {}
func winios_set_compositor_frame(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ height: CGFloat) {}
"""

checks = r"""
func expect(_ condition: Bool, _ message: String) {
    if !condition { print("FAIL: " + message); exit(1) }
    print("PASS: " + message)
}
let scene = UIWindowScene()
UIApplication.shared.connectedScenes = [scene]
TouchControlsHost.attach()
let controls = UIWindow.created[0]
expect(controls.frame.size == CGSize(width: 430, height: 932), "initial portrait overlay matches scene")
let gameWindow = UIWindow(windowScene: scene)
let surface = TestSurface()
surface.window = gameWindow
scene.coordinateSpace.bounds = CGRect(x: 0, y: 0, width: 932, height: 430)
surface.bounds = scene.coordinateSpace.bounds
// A geometry request updates the interface while the phone stays physically
// upright: no orientation notification and no additional attach() call here.
surface.layoutSubviews()
expect(controls.frame == scene.coordinateSpace.bounds, "layout follows programmatic landscape without device notification")
expect(surface.superclassLayouts == 1, "surface keeps its existing UIKit layout callback")
expect(UIDevice.current.orientationNotificationStarts == 1, "layout does not install more orientation observers")
let landscape = MenuLayout(viewport: controls.bounds.size, insets: Insets(top: 0, leading: 59, bottom: 21, trailing: 59))
let center = landscape.position(.zero)
expect(abs(center.x - 841) < 0.001 && abs(center.y - 51.6) < 0.001, "default menu lies at landscape upper trailing safe edge")
expect(center.x > 800, "menu cannot retain its old portrait x around 396")
expect(landscape.position(CGSize(width: 1000, height: -1000)) == CGPoint(x: 841, y: 32), "menu drag clamps to updated safe area")
let custom = MenuLayout(viewport: controls.bounds.size, insets: Insets(top: 0, leading: 59, bottom: 21, trailing: 59), nx: 0.4, ny: 0.5)
expect(custom.position(.zero) == CGPoint(x: 372.8, y: 215), "user menu coordinates remain intact")
let writes = controls.frameWrites
surface.layoutSubviews()
expect(controls.frameWrites == writes, "unchanged layout does not repeatedly resize overlay")
let otherScene = UIWindowScene()
gameWindow.windowScene = otherScene
surface.layoutSubviews()
expect(controls.frameWrites == writes, "other scene cannot resize this overlay")
surface.window = nil
surface.layoutSubviews()
expect(controls.frameWrites == writes, "detached surface leaves overlay untouched")
surface.window = gameWindow
gameWindow.windowScene = scene
scene.coordinateSpace.bounds = CGRect(x: 0, y: 0, width: 430, height: 932)
surface.bounds = scene.coordinateSpace.bounds
surface.layoutSubviews()
let portrait = MenuLayout(viewport: controls.bounds.size, insets: Insets(top: 59, leading: 0, bottom: 34, trailing: 0))
expect(controls.frame == scene.coordinateSpace.bounds && abs(portrait.position(.zero).x - 395.6) < 0.001, "exit portrait layout and trailing anchor restore")
scene.coordinateSpace.bounds = CGRect(x: 0, y: 0, width: 1194, height: 834)
surface.layoutSubviews()
expect(controls.frame.size == CGSize(width: 1194, height: 834), "ordinary tablet scene resizing remains supported")
"""

with tempfile.TemporaryDirectory(prefix="madeira-controls-window-layout-") as temp:
    source = Path(temp) / "main.swift"
    executable = Path(temp) / "check"
    source.write_text(
        stubs
        + host
        + "\nfinal class TestSurface: UIView {\n"
        + "func applyDisplayMode(reason: String) {}\n"
        + surface
        + "\n}\nstruct Insets { var top, leading, bottom, trailing: CGFloat }\n"
        + "struct MenuLayout { var viewport: CGSize; var insets: Insets; "
        + "var measured = CGSize(width: 48, height: 48); var nx = 0.92; var ny = 0.12\n"
        + position.replace("private func", "func", 1)
        + "\n}\n"
        + checks
    )
    subprocess.run([os.environ.get("SWIFTC", "swiftc"), str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
