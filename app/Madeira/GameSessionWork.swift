// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Combine

/// Background work yields from launch preparation until both Wine and its
/// server have stopped. A detach completes preparation, not the game session.
@MainActor final class GameSessionWork: ObservableObject {
    static let shared = GameSessionWork(
        librarySession: { LibraryModel.shared.current != nil },
        nativeSession: { wine_process_is_running() != 0 || wineserver_is_running() != 0 },
        changed: { paused in
            LibraryModel.shared.pauseOptionalWork(paused)
            SteamOwnedLibrary.shared.sessionChanged(active: paused)
            EpicLibraryModel.shared.sessionChanged(active: paused)
        }, backgroundWork: {
            (downloads: SteamOwnedLibrary.shared.hasInFlightDownload || EpicLibraryModel.shared.hasInFlightDownload,
             cloudSaves: SteamOwnedLibrary.shared.hasActiveCloudSaveOperation)
        })

    enum Failure: LocalizedError {
        case gameRunning, saveAlreadyRunning, savesSyncing, sessionAlreadyRunning, backgroundBusy, saveOperationEnded, launchPreparationEnded
        var errorDescription: String? {
            switch self {
            case .gameRunning: return "Close the game and wait for it to stop before syncing saves."
            case .saveAlreadyRunning: return "A save operation is already in progress."
            case .savesSyncing: return "Game saves are being synchronized. Wait for the operation to finish before starting a game."
            case .sessionAlreadyRunning: return "A session is already starting or running."
            case .backgroundBusy: return "Downloads or saves are still stopping. Wait a moment and try again."
            case .saveOperationEnded: return "The save operation is no longer active. Please try again."
            case .launchPreparationEnded: return "The launch is no longer active. Please try again."
            }
        }
    }

    @Published private(set) var pausesBackgroundWork = false
    private(set) var workEpoch: UInt64 = 0
    private var preparation: UUID?
    private var saveOperation: UUID?
    private var timer: Timer?
    var isMonitoring: Bool { timer != nil }
    var isSynchronizingSaves: Bool { saveOperation != nil }
    private let librarySession: () -> Bool
    private let nativeSession: () -> Bool
    private let changed: (Bool) -> Void
    private let backgroundWork: () -> (downloads: Bool, cloudSaves: Bool)

    /// Only runtime-state/notification boundaries vary in host tests. The
    /// production coordinator and actual Foundation timer remain unchanged.
    init(librarySession: @escaping () -> Bool, nativeSession: @escaping () -> Bool,
         changed: @escaping (Bool) -> Void,
         backgroundWork: @escaping () -> (downloads: Bool, cloudSaves: Bool) = { (false, false) }) {
        self.librarySession = librarySession; self.nativeSession = nativeSession; self.changed = changed
        self.backgroundWork = backgroundWork
    }

    func beginLaunchPreparation() throws -> UUID {
        reconcile()
        guard saveOperation == nil else { throw Failure.savesSyncing }
        guard preparation == nil, !librarySession(), !nativeSession() else { throw Failure.sessionAlreadyRunning }
        let token = UUID(); preparation = token; reconcile(); return token
    }
    func ownsPreparation(_ token: UUID) -> Bool { preparation == token }
    var backgroundWorkIsDrained: Bool {
        let background = backgroundWork()
        return !background.downloads && !background.cloudSaves
    }
    /// Cancellation requests do not end a transfer's file write immediately.
    /// Keep the launch paused until both providers have left those operations.
    func waitForLaunchPreparation(_ token: UUID) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(30))
        while true {
            try Task.checkCancellation()
            guard preparation == token else { throw Failure.launchPreparationEnded }
            guard !nativeSession() else { throw Failure.sessionAlreadyRunning }
            if backgroundWorkIsDrained { return }
            guard ContinuousClock.now < deadline else { throw Failure.backgroundBusy }
            try await Task.sleep(for: .milliseconds(100))
        }
    }
    func completePreparation(_ token: UUID) {
        guard preparation == token else { return }
        preparation = nil; reconcile()
    }

    func beginSaveOperation() throws -> UUID {
        reconcile()
        guard saveOperation == nil else { throw Failure.saveAlreadyRunning }
        guard preparation == nil, !librarySession(), !nativeSession() else { throw Failure.gameRunning }
        guard !backgroundWork().cloudSaves else { throw Failure.backgroundBusy }
        let token = UUID(); saveOperation = token; reconcile(); return token
    }
    /// Cancellation has been requested, but an old transfer may still be
    /// unwinding its file write. Save callers await the actual task boundary.
    func waitForSaveOperation(_ token: UUID) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(30))
        while true {
            try Task.checkCancellation()
            guard saveOperation == token else { throw Failure.saveOperationEnded }
            let background = backgroundWork()
            guard !background.cloudSaves else { throw Failure.backgroundBusy }
            if !background.downloads { return }
            guard ContinuousClock.now < deadline else { throw Failure.backgroundBusy }
            try await Task.sleep(for: .milliseconds(100))
        }
    }
    func endSaveOperation(_ token: UUID) {
        guard saveOperation == token else { return }
        saveOperation = nil; reconcile()
    }

    func reconcile() {
        let session = preparation != nil || librarySession() || nativeSession()
        let paused = session || saveOperation != nil
        if paused != pausesBackgroundWork {
            pausesBackgroundWork = paused; workEpoch &+= 1
            changed(paused)
        }
        if session, timer == nil {
            let value = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.reconcile() }
            }
            timer = value; RunLoop.main.add(value, forMode: .common)
        } else if !session {
            timer?.invalidate(); timer = nil
        }
    }
}
