// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation
import Combine

@MainActor final class RuntimePreflightController: ObservableObject {
    struct Context: Sendable {
        let id: UUID
        let title: String
        let identity: String
        let executable: URL
        let gameRoot: URL
        let drive: URL
        let prefix: URL
        var inspectionOnly: Bool = false
        var repairAutomatically: Bool = false
    }
    enum Phase { case checking, needsRepair, preparing, installing, verifying, finished, failed }
    struct Operations {
        var scan: (Context) async throws -> RuntimePreflight.Report
        var prepare: ([RuntimePreflight.Requirement], Context, @escaping @Sendable (String) -> Void) async throws -> RuntimeRepair.Plan
        var validate: (RuntimeRepair.Plan, Context) async throws -> Void
        var result: (RuntimeRepair.Plan, Context) async throws -> RuntimeRepair.Result
        var cleanup: (RuntimeRepair.Plan, Context) async throws -> Void
        var progress: ((RuntimeRepair.Plan, Context) async throws -> RuntimeRepair.LiveProgress)? = nil
    }
    @Published var isPresented = false
    @Published private(set) var phase: Phase = .checking
    @Published private(set) var report: RuntimePreflight.Report?
    @Published private(set) var message = "" {
        didSet {
            if message != oldValue && !message.isEmpty {
                diagnostic("[runtime-preflight] phase=\(phase) \(message)")
            }
        }
    }
    @Published private(set) var requiresRestart = false
    @Published private(set) var installationProgress = RuntimeRepair.LiveProgress()
    @Published private(set) var installationStartedAt: Date?
    private(set) var context: Context?
    private let operations: Operations
    private let diagnostic: (String) -> Void
    private var operation: Task<Void, Never>?
    private var progressOperation: Task<Void, Never>?
    private var progressEpoch = UUID()
    private var epoch = UUID()
    private var cancelling = false
    private var sessionConsumed = false
    private var plan: RuntimeRepair.Plan?
    private var valid: () -> Bool = { false }
    private var identityValid: () -> Bool = { false }
    private var proceedAction: (() -> Void)?
    private var cancelAction: (() -> Void)?
    private var installAction: ((RuntimeRepair.Plan) -> Void)?

    init(operations: Operations? = nil, diagnostic: @escaping (String) -> Void = { _ in }) {
        self.diagnostic = diagnostic
        self.operations = operations ?? Operations(scan: { context in
            try await Self.background { try RuntimePreflight.scan(executable: context.executable, gameRoot: context.gameRoot, drive: context.drive, prefix: context.prefix) }
        }, prepare: { requirements, context, progress in
            try await RuntimeRepair.prepare(requirements: requirements, gameRoot: context.gameRoot, drive: context.drive, identity: context.identity, progress: progress)
        }, validate: { plan, context in
            try await Self.background { try RuntimeRepair.validate(plan: plan, identity: context.identity, drive: context.drive) }
        }, result: { plan, context in
            try await Self.background { try RuntimeRepair.result(for: plan, identity: context.identity, drive: context.drive) }
        }, cleanup: { plan, context in
            try await Self.background { try RuntimeRepair.cleanupUnlaunched(plan: plan, identity: context.identity, drive: context.drive) }
        }, progress: { plan, context in
            try await Self.background { try RuntimeRepair.liveProgress(plan: plan, identity: context.identity, drive: context.drive) }
        })
    }
    private static func background<T: Sendable>(_ action: @escaping @Sendable () throws -> T) async throws -> T {
        let worker = Task.detached(priority: .userInitiated) { try action() }
        return try await withTaskCancellationHandler(operation: { try await worker.value }, onCancel: { worker.cancel() })
    }
    var repairableRequirements: [RuntimePreflight.Requirement] {
        (report?.missingRequirements ?? []).filter { $0.family != .framework && ($0.architecture == .x86 || $0.architecture == .x64) }
    }
    var canRepair: Bool { canContinue && !repairableRequirements.isEmpty }
    var installerCount: Int { plan?.packages.count ?? 0 }
    var canContinue: Bool {
        !requiresRestart && !cancelling && operation == nil && progressOperation == nil && context != nil && valid() &&
            (phase == .needsRepair || phase == .failed || phase == .finished)
    }
    func begin(context: Context, valid: @escaping () -> Bool, identityValid: @escaping () -> Bool,
               proceed: @escaping () -> Void, cancel: @escaping () -> Void,
               install: @escaping (RuntimeRepair.Plan) -> Void) {
        guard operation == nil, progressOperation == nil, !cancelling, cancelAction == nil else { cancel(); return }
        epoch = UUID(); self.context = context; self.valid = valid; self.identityValid = identityValid
        proceedAction = proceed; cancelAction = cancel; installAction = install; plan = nil; report = nil
        installationProgress = .init(); installationStartedAt = nil
        isPresented = true
        if sessionConsumed {
            requiresRestart = true; phase = .failed; message = "本次运行已使用 Windows 会话，请重启 Madeira 后再启动游戏。"; release(); return
        }
        scan(initial: true)
    }
    private func current(_ token: UUID) -> Bool {
        epoch == token && !Task.isCancelled && (sessionConsumed ? identityValid() : valid())
    }
    private func run(_ body: @escaping (UUID) async -> Void) {
        let token = epoch
        operation = Task { await body(token); if self.epoch == token { self.operation = nil } }
    }
    private func release() {
        let callback = cancelAction
        cancelAction = nil; proceedAction = nil; installAction = nil
        callback?()
    }
    func cancel() {
        // Dismissing the sheet must never terminate or release a running installer.
        if phase == .installing { isPresented = false; return }
        guard !cancelling else { return }
        cancelling = true; epoch = UUID()
        let token = epoch, worker = operation
        worker?.cancel()
        Task {
            await worker?.value
            await self.stopProgress()
            guard self.epoch == token else { return }
            if !self.sessionConsumed, let plan = self.plan, let context = self.context {
                self.plan = nil
                do { try await self.operations.cleanup(plan, context) }
                catch { self.message = "未启动的修复文件无法安全清理：\(error.localizedDescription)" }
            }
            guard self.epoch == token else { return }
            self.operation = nil; self.cancelling = false; self.isPresented = false; self.release()
        }
    }
    func launchAnyway() {
        guard canContinue, context?.inspectionOnly == false else { return }
        guard valid() else { cancel(); return }
        if let plan, let context {
            self.plan = nil; phase = .preparing; message = "正在清理未启动的修复文件…"
            run { token in
                do {
                    try await self.operations.cleanup(plan, context)
                    guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
                    self.continueLaunch()
                } catch {
                    guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
                    self.phase = .failed; self.message = "未启动的修复文件无法安全清理：\(error.localizedDescription)"
                }
            }
        } else { continueLaunch() }
    }
    private func continueLaunch() {
        let callback = proceedAction
        proceedAction = nil; cancelAction = nil; installAction = nil; context = nil
        isPresented = false; callback?()
    }
    func recheck() { guard canContinue else { return }; scan(initial: false) }
    private func scan(initial: Bool) {
        guard let context else { return }
        phase = .checking; message = "正在检查此游戏与当前 Wine 环境的运行库…"
        run { token in
            do {
                let report = try await self.operations.scan(context)
                guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
                self.report = report
                if report.requiresAttention {
                    self.phase = .needsRepair; self.message = "请查看检测结果；仅明确且支持的缺失依赖可自动修复。"
                    let requirements = self.repairableRequirements
                    if initial && context.inspectionOnly && context.repairAutomatically && !requirements.isEmpty {
                        await self.prepareRepair(requirements, context: context, token: token)
                    }
                } else {
                    self.phase = .finished; self.message = "未发现可识别的缺失依赖。"
                    if initial && !context.inspectionOnly {
                        let callback = self.proceedAction
                        self.proceedAction = nil; self.cancelAction = nil; self.installAction = nil; self.context = nil
                        self.isPresented = false; callback?()
                    }
                }
            } catch {
                guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
                self.phase = .failed; self.message = "运行环境检查失败：\(error.localizedDescription)"
            }
        }
    }
    func repair() {
        guard canRepair, let context else { return }
        let requirements = repairableRequirements
        run { token in await self.prepareRepair(requirements, context: context, token: token) }
    }
    private func prepareRepair(_ requirements: [RuntimePreflight.Requirement], context: Context, token: UUID) async {
        guard current(token) else { if epoch == token { cancel() }; return }
        phase = .preparing; message = "正在核对 Microsoft 官方安装包并准备修复…"
        var unlaunched: RuntimeRepair.Plan?
        do {
            if let previous = self.plan {
                self.plan = nil
                try await self.operations.cleanup(previous, context)
            }
            guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
            let plan = try await self.operations.prepare(requirements, context) { [weak self] text in
                Task { @MainActor in guard let self, self.current(token), self.phase == .preparing else { return }; self.message = text }
            }
            unlaunched = plan
            guard self.current(token) else { throw CancellationError() }
            try await self.operations.validate(plan, context)
            guard self.current(token) else { throw CancellationError() }
            self.plan = plan; unlaunched = nil; self.phase = .installing
            self.message = "正在启动运行库安装会话；安装完成后将重新检查。"
            self.installAction?(plan)
        } catch {
            var detail = error.localizedDescription
            if let unlaunched {
                do { try await self.operations.cleanup(unlaunched, context) }
                catch { detail += "；未启动的修复文件无法安全清理：\(error.localizedDescription)" }
            }
            guard self.current(token) else { if self.epoch == token { self.cancel() }; return }
            self.phase = .failed; self.message = "修复准备失败：\(detail)"
        }
    }
    func installerStarted() {
        guard phase == .installing else { return }
        sessionConsumed = true; requiresRestart = true
        isPresented = true
        if installationStartedAt == nil {
            installationStartedAt = Date()
            publishProgress(installationProgress)
        }
        startProgress()
    }
    private func packageTitle(at index: Int, plan: RuntimeRepair.Plan) -> String {
        guard plan.packages.indices.contains(index) else { return "运行库" }
        let package = plan.packages[index], requested = package.requirement
        return RuntimePreflight.Requirement(family: requested.family, version: package.version,
            architecture: requested.architecture, rollForward: requested.rollForward, applyPatches: requested.applyPatches).title
    }
    private func publishProgress(_ progress: RuntimeRepair.LiveProgress) {
        guard let plan else { return }
        if installationProgress != progress { installationProgress = progress }
        let count = plan.packages.count
        let completed = "已有 \(progress.completed)/\(count) 个安装包结束。"
        var text: String
        switch progress.stage {
        case .waiting:
            text = "安装会话正在运行，等待 \(packageTitle(at: min(progress.completed, max(0, count - 1)), plan: plan)) 的进度。\(completed)"
        case .installing(let index):
            text = "正在处理第 \(index + 1)/\(count) 个安装包：\(packageTitle(at: index, plan: plan))。\(completed)"
        case .finishing:
            text = "安装回执已写入，正在等待 Windows 会话结束。\(completed)结束后将核对回执并重新检查依赖。"
        }
        if let failure = progress.firstFailure {
            text += " 第 \(failure.index + 1) 个安装包报告退出码 \(failure.exitCode)。"
        }
        if message != text { message = text }
    }
    private func startProgress() {
        guard progressOperation == nil, let reader = operations.progress, let plan, let context,
              identityValid(), !cancelling else { return }
        let token = epoch, progressToken = UUID()
        progressEpoch = progressToken
        progressOperation = Task {
            defer { if self.progressEpoch == progressToken { self.progressOperation = nil } }
            while !Task.isCancelled {
                guard self.epoch == token, self.progressEpoch == progressToken,
                      self.phase == .installing, self.identityValid() else { return }
                do {
                    let progress = try await reader(plan, context)
                    guard !Task.isCancelled, self.epoch == token, self.progressEpoch == progressToken,
                          self.phase == .installing, self.identityValid() else { return }
                    self.publishProgress(progress)
                } catch {
                    guard !Task.isCancelled, self.epoch == token, self.progressEpoch == progressToken,
                          self.phase == .installing, self.identityValid() else { return }
                    let unavailable = "安装进度暂时无法读取；安装会话仍在运行。"
                    if self.message != unavailable { self.message = unavailable }
                }
                do { try await Task.sleep(for: .seconds(1)) }
                catch { return }
            }
        }
    }
    private func stopProgress() async {
        let token = UUID(), worker = progressOperation
        progressEpoch = token
        worker?.cancel()
        // The bound file reader may ignore cancellation until its final read has
        // drained. Its source lease stays owned throughout this await.
        await worker?.value
        if progressEpoch == token { progressOperation = nil }
    }
    /// The launch owner calls this only after both Wine and wineserver stop.
    func installationEnded() async {
        guard phase == .installing, let plan, let context else { return }
        sessionConsumed = true; requiresRestart = true; phase = .verifying
        message = "正在核对安装回执并重新检查依赖…"
        run { token in
            defer { if self.epoch == token { self.release() } }
            await self.stopProgress()
            do {
                guard self.epoch == token else { return }
                guard self.current(token) else { self.phase = .failed; self.message = "账户或游戏入口已变化，无法确认本次修复。请重启 Madeira。"; return }
                let result = try await self.operations.result(plan, context)
                guard self.epoch == token else { return }
                guard self.current(token) else { self.phase = .failed; self.message = "账户或游戏入口已变化，修复结果未确认。请重启 Madeira。"; return }
                switch result {
                case .completed:
                    let fresh = try await self.operations.scan(context)
                    guard self.epoch == token else { return }
                    guard self.current(token) else { self.phase = .failed; self.message = "账户或游戏入口已变化，修复结果未确认。请重启 Madeira。"; return }
                    self.report = fresh
                    guard !fresh.requiresAttention else {
                        self.phase = .failed; self.message = "安装程序已完成，但重新检查仍发现缺失或无法确认的依赖。请重启 Madeira 后重新检查。"; return
                    }
                    self.phase = .finished; self.message = "安装回执与重新检查均已通过。请重启 Madeira 后再启动游戏。"
                case .failed(let index, let code):
                    self.phase = .failed; self.message = "第 \(index + 1) 个安装包失败（退出码 \(code)）。请重启 Madeira 后重试。"
                case .interrupted:
                    self.phase = .failed; self.message = "安装回执不完整，无法确认修复。请重启 Madeira 后重试。"
                }
            } catch {
                guard self.epoch == token else { return }
                self.phase = .failed; self.message = "安装结果无法验证：\(error.localizedDescription)。请重启 Madeira 后重试。"
            }
        }
        await operation?.value
    }
    func installationFailedToStart(_ message: String, sessionConsumed: Bool) {
        guard phase == .installing else { return }
        self.sessionConsumed = self.sessionConsumed || sessionConsumed
        requiresRestart = self.sessionConsumed; phase = .failed
        self.message = message + (requiresRestart ? " 请重启 Madeira 后重试。" : "")
        if progressOperation != nil {
            let token = epoch
            progressEpoch = UUID(); progressOperation?.cancel()
            Task {
                await self.stopProgress()
                guard self.epoch == token, !self.cancelling else { return }
                if self.requiresRestart { self.release() }
            }
        } else if requiresRestart { release() }
    }
}
