// SPDX-License-Identifier: GPL-3.0-or-later
import SwiftUI
import SafariServices

/// Unfinished jobs stay visible independently of installed games and grouping.
/// Both providers remove their queue entry only after successful installation.
struct LibraryDownloadsSection: View {
    let open: (LibraryEntry) -> Void
    @ObservedObject private var steam = SteamOwnedLibrary.shared
    @ObservedObject private var epic = EpicLibraryModel.shared
    @ObservedObject private var steamGames = SteamGamesModel.shared
    @State private var steamSelection: SteamSelection?
    @State private var epicSelection: EpicGame?
    private struct SteamSelection: Identifiable { let id: Int }

    private var steamIDs: [Int] { SteamOwnedLibrary.enabled ? steam.downloads.keys.sorted() : [] }
    private var epicJobs: [EpicDownload] {
        EpicGamesSection.enabled ? epic.visibleDownloads.sorted {
            $0.game.title.localizedStandardCompare($1.game.title) == .orderedAscending
        } : []
    }

    var body: some View {
        let ids = steamIDs, jobs = epicJobs
        Group {
            if !ids.isEmpty || !jobs.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    LibrarySectionHeader(title: "正在下载", count: ids.count + jobs.count) { EmptyView() }
                    ForEach(ids, id: \.self) { id in
                        if let job = steam.downloads[id] {
                            Button { steamSelection = SteamSelection(id: id) } label: {
                                VStack(alignment: .leading, spacing: 8) {
                                    downloadTitle(steam.game(id)?.name ?? steamGames.games.first(where: { $0.id == id })?.name ?? "Steam \(id)", provider: "Steam")
                                    SteamDownloadStatus(download: job)
                                }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                                    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
                            }.buttonStyle(.plain)
                        }
                    }
                    ForEach(jobs) { job in
                        Button { epicSelection = job.game } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                downloadTitle(job.game.title, provider: "Epic Games")
                                EpicDownloadStatus(download: job)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                                .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
                        }.buttonStyle(.plain)
                    }
                }
            }
        }
            .sheet(item: $steamSelection) { selection in
                SteamGameSheet(appID: selection.id) { entry in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { open(entry) }
                }
            }
            .sheet(item: $epicSelection) { game in
                EpicGameSheet(game: game) { entry in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { open(entry) }
                }
            }
    }

    private func downloadTitle(_ title: String, provider: String) -> some View {
        HStack {
            Image(systemName: "arrow.down.circle")
            Text(title).font(.headline).lineLimit(2)
            Spacer(minLength: 8)
            Text(provider).font(.caption).foregroundStyle(.secondary)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
        }.foregroundStyle(.primary)
    }
}

struct EpicSettingsSection: View {
    let open: (SettingsSheet) -> Void
    @ObservedObject private var account = EpicAccount.shared
    @ObservedObject private var library = EpicLibraryModel.shared
    var body: some View {
        Section {
            if account.signedIn {
                LabeledContent("Signed in as", value: account.displayName)
                Button("Refresh Epic library") { library.refresh(force: true) }.disabled(library.refreshing)
                Button("Sign out of Epic Games", role: .destructive) { account.signOut() }
            } else {
                Button { open(.epicSignIn) } label: { Label("Sign in to Epic Games", systemImage: "person.crop.circle") }
            }
        } header: { Text("Epic Games") } footer: {
            Text("Download your Epic Windows games and run them in Madeira. Epic Cloud saves are not synced yet.")
        }
    }
}

private struct EpicLoginBrowser: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController { SFSafariViewController(url: url) }
    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

struct EpicSignInView: View {
    @ObservedObject private var account = EpicAccount.shared
    @Environment(\.dismiss) private var dismiss
    @State private var browser = false
    @State private var browserURL = EpicAccount.loginURL
    @State private var code = ""
    @State private var failure: String?
    @State private var loginTask: Task<Void, Never>?
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Button("Open Epic login") { browserURL = EpicAccount.loginURL; browser = true }
                    Text("Sign in on Epic's website. Copy authorizationCode from the page that appears, then return here. You can paste the whole JSON response.")
                        .foregroundStyle(.secondary)
                }
                Section {
                    Button("Open Epic account") { browserURL = EpicAccount.accountURL; browser = true }
                } footer: {
                    Text("If Epic asks for privacy policy acceptance, review and complete the prompts on its website yourself. Then return here and open Epic login again for a new authorization code.")
                }
                Section {
                    SecureField("Paste authorization code", text: $code)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button {
                        let input = code; code = ""; failure = nil
                        loginTask = Task {
                            do {
                                try await account.login(code: input)
                                guard !Task.isCancelled else { return }
                                EpicLibraryModel.shared.refresh(); dismiss()
                            } catch { if !Task.isCancelled { failure = EpicLibraryModel.message(error) } }
                        }
                    } label: {
                        if account.busy { ProgressView() } else { Text("Sign in") }
                    }.disabled(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || account.busy)
                } header: { Text("One-time authorization code") } footer: { Text("Your password stays on Epic's website. The refresh session is stored in this device's Keychain.") }
                if let failure { Section { Text(failure).foregroundStyle(.red) } }
                if let error = account.error { Section { Text(error).foregroundStyle(.secondary) } }
            }
            .navigationTitle("Epic Games")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .sheet(isPresented: $browser) { EpicLoginBrowser(url: browserURL).ignoresSafeArea() }
            .onDisappear { if !browser { code = ""; loginTask?.cancel() } }
        }
    }
}

/// Match Steam's artwork sizing: the decoded image fills the allocated box,
/// never contributing its original pixel dimensions to the card's layout.
struct EpicGameArtwork: View {
    let game: EpicGame
    @Environment(\.displayScale) private var displayScale
    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color(uiColor: .secondarySystemFill)
                Image(systemName: "gamecontroller.fill").font(.largeTitle).foregroundStyle(.secondary)
                CachedArtwork(url: game.artworkURL,
                              maximumPixelSize: ArtworkCache.thumbnailPixelSize(width: geometry.size.width,
                                  height: geometry.size.height, scale: displayScale)) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().scaledToFill()
                            .frame(width: geometry.size.width, height: geometry.size.height).clipped()
                    default:
                        // A concrete placeholder mounts CachedArtwork's loading task.
                        Color.clear
                    }
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height).clipped()
        }
        .accessibilityHidden(true)
    }
}

struct EpicGameCell: View {
    let game: EpicGame
    var list = false
    var dense = false
    @ObservedObject private var model = EpicLibraryModel.shared
    var body: some View {
        let download = model.download(game.id)
        let installed = model.installed(game.id) != nil
        Group {
            if list && dense {
                HStack(spacing: 10) {
                    artwork(download, installed: installed, font: .caption).frame(width: 28, height: 42)
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(game.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        if let download { EpicDownloadStatus(download: download) }
                    }
                    Spacer(minLength: 6)
                    badge(download, installed: installed).fixedSize()
                }.padding(.horizontal, 8).padding(.vertical, 5)
                    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 10))
            } else if list {
                HStack(spacing: 14) {
                    artwork(download, installed: installed, font: .title3).frame(width: 48, height: 72)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    VStack(alignment: .leading, spacing: 8) {
                        Text(game.title).font(.headline).lineLimit(2)
                        if let download { EpicDownloadStatus(download: download) }
                        else { badge(nil, installed: installed) }
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
                }.padding(10)
                    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    artwork(download, installed: installed, font: .largeTitle).aspectRatio(2.0 / 3.0, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    Text(game.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                    badge(download, installed: installed)
                }.padding(4)
            }
        }
        .foregroundStyle(.primary).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private func artwork(_ download: EpicDownload?, installed: Bool, font: Font) -> some View {
        EpicGameArtwork(game: game).overlay {
            if let download {
                ZStack {
                    Color.black.opacity(0.45)
                    switch download.state {
                    case "downloading": ProgressView().tint(.white)
                    case "queued": Image(systemName: "clock").font(.title2).foregroundStyle(.white)
                    case "failed": Image(systemName: "exclamationmark.triangle.fill").font(.title2).foregroundStyle(.yellow)
                    default: Image(systemName: "pause.circle.fill").font(.title).foregroundStyle(.white)
                    }
                }
            } else if !installed {
                ZStack {
                    Color.black.opacity(0.4)
                    Image(systemName: "icloud.and.arrow.down").font(font.weight(.medium))
                        .foregroundStyle(.white.opacity(0.5))
                }
            }
        }
    }

    private func badge(_ download: EpicDownload?, installed: Bool) -> some View {
        Text(download?.state.capitalized ?? (installed ? "Installed" : "Not downloaded"))
            .font(.caption2.weight(.medium)).lineLimit(1)
            .padding(.horizontal, 5).padding(.vertical, 4)
            .background(.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            .foregroundStyle(.secondary)
    }
}

struct EpicDownloadStatus: View {
    let download: EpicDownload
    @ObservedObject private var model = EpicLibraryModel.shared
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if download.total > 0 {
                ProgressView(value: Double(min(max(0, download.received), download.total)), total: Double(download.total))
            }
            Text(download.state.capitalized).font(.caption).foregroundStyle(.secondary)
            if download.state == "downloading" {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    Text(model.downloadSpeed(download).map { rate in
                        "下载速度：" + ByteCountFormatter.string(
                            fromByteCount: Int64(min(rate, Double(Int64.max / 2))), countStyle: .file) + "/s"
                    } ?? "下载速度：等待数据…")
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
            }
            if download.total > 0 {
                Text(ByteCountFormatter.string(fromByteCount: download.received, countStyle: .file) + " / " +
                     ByteCountFormatter.string(fromByteCount: download.total, countStyle: .file))
                    .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            if let error = download.error { Text(error).font(.caption).foregroundStyle(.red) }
        }
    }
}

struct EpicGameSheet: View {
    let game: EpicGame
    let open: (LibraryEntry) -> Void
    @ObservedObject private var model = EpicLibraryModel.shared
    @Environment(\.dismiss) private var dismiss
    @State private var confirmCancellation = false
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 20) {
                        EpicGameArtwork(game: game).frame(width: 120, height: 180)
                            .clipShape(RoundedRectangle(cornerRadius: 14))
                        VStack(alignment: .leading, spacing: 12) {
                            Text(game.title).font(.title2.bold())
                            primaryAction
                        }
                    }.padding(.vertical, 24)
                        .listRowBackground(
                            EpicGameArtwork(game: game).blur(radius: 4)
                                .overlay(Color(uiColor: .secondarySystemGroupedBackground).opacity(0.55))
                                .clipped()
                        )
                }
                if let requirement = game.launchRequirement {
                    Section { Text("This game requires \(requirement). That launch flow is not supported yet.") }
                }
                if let job = model.download(game.id) {
                    Section("Download") {
                        EpicDownloadStatus(download: job)
                        if model.installed(game.id) == nil {
                            Button(job.cancellationID == nil ? "Cancel download and clear files" : "Retry cleanup", role: .destructive) {
                                confirmCancellation = true
                            }.disabled(job.state == "cancelling")
                        }
                    }
                    if let nodes = job.cdnResults, !nodes.isEmpty {
                        Section {
                            let ranked = nodes.sorted { ($0.bytesPerSecond ?? 0) > ($1.bytesPerSecond ?? 0) }
                            ForEach(Array(ranked.enumerated()), id: \.offset) { index, node in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(node.host).font(.caption).textSelection(.enabled)
                                    if node.failed {
                                        Text("Unavailable for this download test").font(.caption).foregroundStyle(.orange)
                                    } else if let rate = node.bytesPerSecond, rate.isFinite, rate > 0 {
                                        Text(node.scheme.uppercased() + " · " + ByteCountFormatter.string(
                                            fromByteCount: Int64(min(rate, Double(Int64.max / 2))), countStyle: .file) + "/s")
                                            .font(.caption).foregroundStyle(.secondary)
                                        if index == 0 { Text("Fastest measured").font(.caption).foregroundStyle(.tint) }
                                    } else {
                                        Text("Not measured yet").font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        } header: {
                            Text("CDN nodes")
                        } footer: {
                            Text("Official nodes are tested one at a time using verified game data. Downloads prefer the fastest measured node and try alternatives if it fails.")
                        }
                    }
                }
                Section { Text("Epic Cloud saves are not synced yet. Game compatibility depends on Madeira's Windows runtime.").font(.footnote).foregroundStyle(.secondary) }
            }
            .navigationTitle("Epic Games").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .confirmationDialog("Cancel download for \(game.title)?", isPresented: $confirmCancellation, titleVisibility: .visible) {
                Button("Cancel download and delete files", role: .destructive) { model.cancel(game.id) }
                Button("Keep download", role: .cancel) {}
            } message: {
                Text("Delete this download's unfinished game files and cached data. A new download will start from the beginning.")
            }
        }
    }

    @ViewBuilder private var primaryAction: some View {
        if game.launchRequirement == nil {
            if let installed = model.installed(game.id) {
                Button { dismiss(); open(LibraryModel.shared.epicEntry(installed)) } label: {
                    steamActionLabel("Open", symbol: "play.fill")
                }.buttonStyle(.borderedProminent)
            } else if let job = model.download(game.id) {
                if job.state == "cancelling" {
                    ProgressView("Clearing download…")
                } else if job.cancellationID != nil {
                    Text("Cleanup needs attention").font(.caption).foregroundStyle(.secondary)
                } else if job.state == "queued" || job.state == "downloading" {
                    Button { model.pause(game.id) } label: { steamActionLabel("Pause", symbol: "pause.fill") }
                        .buttonStyle(.bordered)
                } else {
                    Button { model.resume(game.id) } label: {
                        steamActionLabel(job.state == "failed" ? "Try again" : "Resume", symbol: "arrow.down.circle.fill")
                    }.buttonStyle(.borderedProminent)
                }
            } else {
                Button { model.enqueue(game) } label: { steamActionLabel("Install", symbol: "arrow.down.circle.fill") }
                    .buttonStyle(.borderedProminent)
            }
        }
    }
}

struct EpicGamesSection: View {
    let search: String
    var layout = "cards"
    var sort = "played"
    var width: CGFloat = 390
    let open: (LibraryEntry) -> Void
    @ObservedObject private var model = EpicLibraryModel.shared
    @ObservedObject private var account = EpicAccount.shared
    @ObservedObject private var library = LibraryModel.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var signIn = false
    @State private var selection: EpicGame?
    static var enabled: Bool { MadeiraConfig.flag("MADEIRA_EPIC_LIBRARY") }
    var items: [EpicGame] {
        var seen = Set<String>()
        let lastPlayed = library.entries.reduce(into: [String: Date]()) { dates, entry in
            if let id = entry.epicAppName, dates[id] == nil { dates[id] = entry.lastPlayed ?? .distantPast }
        }
        return (model.visibleInstallations.map(\.game) + model.visibleDownloads.map(\.game) + model.games)
            .filter { seen.insert($0.id).inserted && (search.isEmpty || $0.title.localizedCaseInsensitiveContains(search)) }
            .sorted { a, b in
                if sort == "played" {
                    let first = lastPlayed[a.id] ?? .distantPast, second = lastPlayed[b.id] ?? .distantPast
                    if first != second { return first > second }
                }
                return a.title.localizedStandardCompare(b.title) == .orderedAscending
            }
    }
    var body: some View {
        if Self.enabled {
            let visibleItems = items
            VStack(alignment: .leading, spacing: 14) {
                LibrarySectionHeader(title: "Epic Games", count: visibleItems.count) {
                    if model.refreshing { ProgressView() }
                    else if account.signedIn { Button { model.refresh(force: true) } label: { Image(systemName: "arrow.clockwise") } }
                }
                if !account.signedIn { Button("Sign in to Epic Games") { signIn = true } }
                if visibleItems.isEmpty && account.signedIn && !model.refreshing { Text("No Windows games in this Epic library.").foregroundStyle(.secondary) }
                LibraryCells(items: visibleItems, layout: layout, width: width) { game, list, dense in
                    Button {
                        if let installed = model.installed(game.id) { open(library.epicEntry(installed)) }
                        else { selection = game }
                    } label: { EpicGameCell(game: game, list: list, dense: dense) }.libraryCardButtonStyle(grid: !list)
                }
            }
            .onAppear { model.refresh() }
            .onChange(of: scenePhase) { _, phase in if phase == .active { model.refresh() } }
            .sheet(isPresented: $signIn) { EpicSignInView() }
            .sheet(item: $selection) { game in
                EpicGameSheet(game: game) { entry in DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { open(entry) } }
            }
            .alert("Epic Games", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
                Button("OK", role: .cancel) { model.error = nil }
            } message: { Text(model.error ?? "") }
        }
    }
}
