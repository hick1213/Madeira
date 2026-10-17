import SwiftUI
import UIKit

/// Phone game sessions use the wide display; tablets keep their existing policy.
@MainActor enum PhoneSessionOrientation {
    private static weak var scene: UIWindowScene?
    private static var previous: UIInterfaceOrientationMask?

    static func supported(phone: Bool, session: Bool) -> UIInterfaceOrientationMask {
        phone && session ? .landscape : .allButUpsideDown
    }

    static func update(session: Bool) {
        guard UIDevice.current.userInterfaceIdiom == .phone,
              let active = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene })
                .first(where: { $0.activationState == .foregroundActive }) else { return }
        var requested: UIInterfaceOrientationMask?
        if session {
            if scene !== active || previous == nil {
                scene = active
                switch active.interfaceOrientation {
                case .portrait: previous = .portrait
                case .landscapeLeft: previous = .landscapeLeft
                case .landscapeRight: previous = .landscapeRight
                default: previous = .portrait
                }
            }
            if !active.interfaceOrientation.isLandscape { requested = .landscape }
        } else {
            if scene === active { requested = previous }
            previous = nil
            scene = nil
        }
        for window in active.windows {
            window.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
        }
        guard let requested else { return }
        LogStore.shared.log("[orientation] phone session=\(session) request=\(requested.rawValue)")
        active.requestGeometryUpdate(.iOS(interfaceOrientations: requested)) { error in
            LogStore.shared.log("[orientation] request failed: \(error.localizedDescription)")
        }
    }
}

final class MadeiraAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        PhoneSessionOrientation.supported(phone: UIDevice.current.userInterfaceIdiom == .phone,
                                          session: LibraryModel.shared.current != nil)
    }
}

@main
struct MadeiraApp: App {
    @UIApplicationDelegateAdaptor(MadeiraAppDelegate.self) private var appDelegate
    init() {
        // ml1172: read the screen on the main thread; library entries, whose
        // default Resolution comes from it, are also made on other threads.
        _ = ResolutionChoices.screen
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .modifier(ClaimGamepadEvents())
                .onAppear {
                    GamepadInput.shared.start()
                    HardwareInput.shared.start()
                    JITNetworkShortcut.shared.restoreLeftover()   // also starts its network path monitor
                }
                // madeira://jit-network/... (the Madeira JIT shortcut returning, JITNetwork.swift),
                // else madeira://play?exe=... (Home Screen shortcuts, SavesAndShortcuts.swift).
                .onOpenURL { url in if !JITNetworkShortcut.shared.handle(url) { ShortcutRouter.shared.handle(url) } }
        }
    }
}
