// SPDX-License-Identifier: GPL-3.0-or-later
import SwiftUI

struct RuntimePreflightSheet: View {
    @ObservedObject var model: RuntimePreflightController
    private var busy: Bool { model.phase == .checking || model.phase == .preparing || model.phase == .verifying }
    private func status(_ state: RuntimePreflight.State) -> String {
        switch state {
        case .satisfied: return "已满足"
        case .missing: return "缺失"
        case .unknown: return "无法确认"
        case .unsupported: return "暂不支持"
        }
    }
    private func color(_ state: RuntimePreflight.State) -> Color {
        switch state {
        case .satisfied: return .green
        case .missing: return .orange
        case .unknown, .unsupported: return .secondary
        }
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text(model.context?.title ?? "运行环境").font(.title2.bold())
                    HStack(alignment: .top, spacing: 12) {
                        if busy || model.phase == .installing { ProgressView() }
                        Text(model.message).textSelection(.enabled)
                    }
                    if model.phase == .installing || model.phase == .verifying {
                        if let began = model.installationStartedAt {
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                Text("已耗时 \(max(0, Int(context.date.timeIntervalSince(began)))) 秒")
                                    .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                            }
                        }
                        if model.installerCount > 0 {
                            Text("已结束的安装包：\(min(model.installerCount, max(0, model.installationProgress.completed)))/\(model.installerCount) 项")
                                .font(.subheadline.monospacedDigit())
                        }
                        Text("安装器以静默模式运行，不提供连续百分比。这里显示实际步骤和耗时；完成后会重新检查运行环境。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    if let report = model.report, model.phase != .installing, model.phase != .verifying {
                        ForEach(report.findings) { finding in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack(alignment: .top) {
                                    Text(finding.requirement?.title ?? "未明确的运行库要求").font(.headline)
                                    Spacer()
                                    Text(status(finding.state)).font(.subheadline.bold()).foregroundStyle(color(finding.state))
                                }
                                Text(finding.message).font(.footnote).foregroundStyle(.secondary).textSelection(.enabled)
                                if finding.state == .missing && finding.requirement?.family == .framework {
                                    Text("此版本暂不提供 .NET Framework 自动安装；可在确认兼容性后自行安装。").font(.footnote).foregroundStyle(.secondary)
                                }
                            }
                            Divider()
                        }
                        if report.findings.contains(where: { $0.state == .unknown || $0.state == .unsupported }) {
                            Text("无法确认或暂不支持的项目不会自动安装；检测无法保证游戏的所有运行要求都已满足。").font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    if model.requiresRestart && model.phase != .installing && model.phase != .verifying {
                        Label("重启 Madeira 后再启动游戏", systemImage: "arrow.clockwise")
                            .font(.headline).foregroundStyle(.orange)
                    }
                    if model.canRepair {
                        Button(model.repairableRequirements.count == model.report?.missingRequirements.count ? "一键修复" : "修复可支持项") { model.repair() }
                            .buttonStyle(.borderedProminent)
                        Text("仅下载并验证 Microsoft 官方运行库安装包。安装会使用本次 Windows 会话，完成后需要重启 Madeira。").font(.footnote).foregroundStyle(.secondary)
                    }
                    if model.canContinue {
                        HStack {
                            Button("重新检查") { model.recheck() }.buttonStyle(.bordered)
                            if model.context?.inspectionOnly == true {
                                Button(model.report?.requiresAttention == false ? "完成" : "稍后处理") { model.cancel() }.buttonStyle(.bordered)
                            } else {
                                Button(model.report?.requiresAttention == false ? "启动游戏" : "仍然启动") { model.launchAnyway() }.buttonStyle(.bordered)
                            }
                        }
                    }
                }
                .padding(24)
            }
            .navigationTitle("运行环境检查")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(model.phase == .installing ? "查看 Windows 画面" : model.requiresRestart ? "关闭" : "取消") { model.cancel() }
                }
            }
            .interactiveDismissDisabled(busy)
        }
    }
}
