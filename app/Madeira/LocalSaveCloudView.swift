// SPDX-License-Identifier: GPL-3.0-or-later
import SwiftUI
import UIKit
import UniformTypeIdentifiers

private struct SaveFolderPicker: UIViewControllerRepresentable {
    let initial: URL?
    let selected: (URL) -> Void
    func makeCoordinator() -> Coordinator { Coordinator(selected: selected) }
    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder], asCopy: false)
        picker.directoryURL = initial
        picker.allowsMultipleSelection = false
        picker.delegate = context.coordinator
        return picker
    }
    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}
    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let selected: (URL) -> Void
        init(selected: @escaping (URL) -> Void) { self.selected = selected }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let url = urls.first { selected(url) }
        }
    }
}

struct LocalSaveCloudView: View {
    @ObservedObject private var cloud = LocalSaveCloudModel.shared
    @ObservedObject private var library = LibraryModel.shared
    @State private var selectedGame: UUID?
    @State private var windowsPath = ""
    @State private var choosingCloudFolder = false
    @State private var choosingSaveFolder = false
    private var games: [LibraryEntry] {
        library.entries.filter { $0.steamAppID == nil && $0.epicAppName == nil && $0.desktop != true }
    }
    private var game: LibraryEntry? { games.first { $0.id == selectedGame } }
    private var pendingPresented: Binding<Bool> {
        Binding(get: { cloud.pending != nil }, set: { if !$0, cloud.pending != nil { cloud.cancel() } })
    }
    var body: some View {
        Form {
            Section {
                Button { choosingCloudFolder = true } label: {
                    Label(cloud.folderName ?? "Choose iCloud Drive folder…", systemImage: "folder.badge.icloud")
                }.disabled(cloud.busy)
                if cloud.hasFolder {
                    Button("Refresh cloud backups") { cloud.refresh() }.disabled(cloud.busy)
                }
            } header: { Text("Cloud folder") } footer: {
                Text("Choose a dedicated folder in iCloud Drive using Files. Madeira keeps separate versions in a Madeira Local Saves subfolder. Other Files providers follow their own upload behavior.")
            }
            Section {
                Picker("Local game", selection: $selectedGame) {
                    Text("Choose a game").tag(UUID?.none)
                    ForEach(games) { entry in Text(entry.title).tag(Optional(entry.id)) }
                }.disabled(cloud.busy)
                if let game {
                    if game.externalLibraryID != nil {
                        Text("For an external game, choose its save folder in Madeira's Windows user profile (Documents, Saved Games or AppData). Saves on the external drive are not synchronized.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Button("Choose this game's save folder…") { choosingSaveFolder = true }.disabled(cloud.busy)
                    TextField("C:\\users\\…\\Saved Games\\Game", text: $windowsPath)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().disabled(cloud.busy)
                    Button("Use this save folder") {
                        do { try cloud.configure(game, windowsPath: windowsPath) }
                        catch { cloud.message = error.localizedDescription }
                    }.disabled(cloud.busy || windowsPath.isEmpty)
                    if let config = cloud.configuration(game.id) {
                        Text("C:\\" + config.directory.dropFirst("wine/drive_c/".count).replacingOccurrences(of: "/", with: "\\"))
                            .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        Button { cloud.prepareUpload(game) } label: { Label("Upload saves", systemImage: "icloud.and.arrow.up") }
                            .disabled(cloud.busy || !cloud.hasFolder)
                    }
                }
                if games.isEmpty { Text("Add a local game to the library first.").foregroundStyle(.secondary) }
            } header: { Text("Local game saves") } footer: {
                Text("Select the game's actual save directory, not a whole Windows user or AppData folder. Internal games may use saves inside their installation folder; external games use Madeira's Windows user profile saves. Steam and Epic entries keep their existing save handling. Close the game before syncing. Each backup supports up to 1,024 files, 16 MiB per file and 64 MiB in total.")
            }
            if cloud.hasFolder {
                Section {
                    if cloud.records.isEmpty { Text("Refresh to see available versions.").foregroundStyle(.secondary) }
                    ForEach(cloud.records) { record in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(record.title).font(.headline)
                            Text(record.created, format: .dateTime.year().month().day().hour().minute())
                                .font(.caption).foregroundStyle(.secondary)
                            Text("\(record.files) files · \(ByteCountFormatter.string(fromByteCount: Int64(record.bytes), countStyle: .file))")
                                .font(.caption).foregroundStyle(.secondary)
                            Button { if let game { cloud.prepareDownload(record, into: game) } } label: {
                                Label("Download into selected game…", systemImage: "icloud.and.arrow.down")
                            }.disabled(cloud.busy || game.flatMap { cloud.configuration($0.id) } == nil)
                        }
                    }
                } header: { Text("Available cloud versions") } footer: {
                    Text("On another device, configure that game's local save folder, then choose its cloud version here. Confirm the game and destination before restoring. Existing local files are backed up before replacement; files absent from the backup are preserved.")
                }
            }
            if cloud.busy || cloud.message != nil {
                Section {
                    if cloud.busy { HStack { ProgressView(); Text(cloud.pending == nil ? "Synchronizing saves…" : "Waiting for your choice…") } }
                    if let message = cloud.message { Text(message).font(.footnote).textSelection(.enabled) }
                    if cloud.busy { Button("Cancel", role: .cancel) { cloud.cancel() } }
                }
            }
        }
        .navigationTitle("Local game saves")
        .onChange(of: selectedGame) { _, value in
            if let value, let config = cloud.configuration(value) {
                windowsPath = "C:\\" + config.directory.dropFirst("wine/drive_c/".count).replacingOccurrences(of: "/", with: "\\")
            } else { windowsPath = "" }
        }
        .sheet(isPresented: $choosingCloudFolder) {
            SaveFolderPicker(initial: nil) { url in
                choosingCloudFolder = false
                do { try cloud.setFolder(url); cloud.refresh() } catch { cloud.message = error.localizedDescription }
            }
        }
        .sheet(isPresented: $choosingSaveFolder) {
            SaveFolderPicker(initial: LibraryModel.documents.appendingPathComponent("wine/drive_c", isDirectory: true)) { url in
                choosingSaveFolder = false
                guard let game else { return }
                do {
                    // Only the system /var alias is normalized; links are not resolved.
                    func parts(_ path: String) -> [String] {
                        var p = path.split(separator: "/").map(String.init)
                        if p.first == "var" { p.insert("private", at: 0) }
                        return p
                    }
                    let base = parts(LibraryModel.documents.appendingPathComponent("wine/drive_c").path(percentEncoded: false))
                    let selected = parts(url.path(percentEncoded: false))
                    guard url.isFileURL, selected.starts(with: base), selected.count > base.count else {
                        throw LibraryError.message("Choose a save folder in On My iPad/iPhone → Madeira → wine → drive_c, or enter its Windows path.")
                    }
                    let path = "C:\\" + selected.dropFirst(base.count).joined(separator: "\\")
                    try cloud.configure(game, windowsPath: path); windowsPath = path
                } catch { cloud.message = error.localizedDescription }
            }
        }
        .confirmationDialog(confirmationTitle, isPresented: pendingPresented, titleVisibility: .visible) {
            Button(confirmationButton, role: .destructive) { cloud.confirm() }
            Button("Cancel", role: .cancel) { cloud.cancel() }
        } message: { Text(confirmationMessage) }
        .onDisappear { cloud.cancel() }
    }
    private var confirmationTitle: String {
        switch cloud.pending {
        case .upload(let archive, _, _): return "Upload saves for \(archive.title)?"
        case .download(let archive, _, _, _, _): return "Restore cloud saves for \(archive.title)?"
        case nil: return "Synchronize saves?"
        }
    }
    private var confirmationButton: String {
        switch cloud.pending { case .upload: return "Upload new cloud version"; case .download: return "Back up and restore"; case nil: return "Continue" }
    }
    private var confirmationMessage: String {
        switch cloud.pending {
        case .upload(let archive, _, _):
            return "\(archive.files.count) files. This becomes the current cloud version for this game. Previous cloud versions are kept. The file provider handles the final upload."
        case .download(let archive, let config, _, _, let overwritten):
            let title = library.entries.first { $0.id == config.game }?.title ?? "the selected game"
            let path = "C:\\" + config.directory.dropFirst("wine/drive_c/".count).replacingOccurrences(of: "/", with: "\\")
            return "Cloud game: \(archive.title). Local game: \(title). Destination: \(path). \(archive.files.count) files; \(overwritten) existing files will be replaced. Original local saves are backed up first."
        case nil: return ""
        }
    }
}
