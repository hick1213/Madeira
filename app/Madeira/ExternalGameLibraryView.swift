// SPDX-License-Identifier: GPL-3.0-or-later
import SwiftUI
import UIKit
import UniformTypeIdentifiers

private struct ExternalGameFolderPicker: UIViewControllerRepresentable {
    let selected: (URL) -> Void
    func makeCoordinator() -> Coordinator { Coordinator(selected) }
    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder], asCopy: false)
        picker.allowsMultipleSelection = false; picker.delegate = context.coordinator
        return picker
    }
    func updateUIViewController(_ picker: UIDocumentPickerViewController, context: Context) {}
    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let selected: (URL) -> Void
        init(_ selected: @escaping (URL) -> Void) { self.selected = selected }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let folder = urls.first { selected(folder) }
        }
    }
}

struct ExternalGameLibraryView: View {
    let select: (LibraryEntry) -> Void
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var work = GameSessionWork.shared
    @ObservedObject private var library = LibraryModel.shared
    @State private var roots = ExternalGameLibrary.shared.roots
    @State private var error = ExternalGameLibrary.shared.loadError
    @State private var choosingFolder = false
    @State private var relinking: UUID?
    var body: some View {
        List {
            Section {
                Button {
                    relinking = nil; choosingFolder = true
                } label: { Label("Choose game folder on external drive…", systemImage: "externaldrive.badge.plus") }
                .disabled(work.pausesBackgroundWork)
            } footer: {
                Text("Connect the drive, select its game folder in Files, then browse to an .exe, .bat or .cmd. Game files stay on the drive. Steam keeps its existing library and downloads.")
            }
            if !roots.isEmpty {
                Section("Registered folders") {
                    ForEach(roots) { root in
                        VStack(alignment: .leading, spacing: 8) {
                            NavigationLink {
                                ExternalExecutableBrowser(rootID: root.id, name: root.name, relative: "", select: select)
                            } label: { Label(root.name, systemImage: "externaldrive") }
                            .disabled(work.pausesBackgroundWork)
                            Button("Reconnect or reselect folder…") { relinking = root.id; choosingFolder = true }
                                .font(.subheadline).disabled(work.pausesBackgroundWork)
                        }.padding(.vertical, 4)
                    }
                }
            }
            if let error { Section { Text(error).foregroundStyle(.red) } }
        }
        .navigationTitle("External game library")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        .sheet(isPresented: $choosingFolder) {
            ExternalGameFolderPicker { url in
                choosingFolder = false
                do {
                    let paths = library.entries.filter { $0.externalLibraryID == relinking && relinking != nil }.map(\.relativePath)
                    try ExternalGameLibrary.shared.register(url, relinking: relinking, requiredPaths: paths,
                        busy: work.pausesBackgroundWork || library.current != nil || wine_process_is_running() != 0 || wineserver_is_running() != 0)
                    roots = ExternalGameLibrary.shared.roots; error = nil
                } catch { self.error = error.localizedDescription }
            }
        }
    }
}

private struct ExternalExecutableBrowser: View {
    let rootID: UUID
    let name: String
    let relative: String
    let select: (LibraryEntry) -> Void
    @ObservedObject private var work = GameSessionWork.shared
    @State private var items: [ExternalGameLibrary.BrowseItem] = []
    @State private var truncated = false
    @State private var error: String?
    var body: some View {
        List {
            if let error { Text(error).foregroundStyle(.red) }
            ForEach(items) { item in
                if item.directory {
                    NavigationLink {
                        ExternalExecutableBrowser(rootID: rootID, name: item.name, relative: item.relative, select: select)
                    } label: { Label(item.name, systemImage: "folder") }.disabled(work.pausesBackgroundWork)
                } else {
                    Button {
                        do {
                            guard !work.pausesBackgroundWork else { throw ExternalGameLibrary.Failure.busy }
                            let lease = try ExternalGameLibrary.shared.acquire(rootID); defer { lease.release() }
                            let file = try ExternalGameLibrary.resolve(item.relative, inside: lease.root, directory: false)
                            select(try LibraryModel.inspect(file, externalLibraryID: rootID, lease: lease))
                        } catch { self.error = error.localizedDescription }
                    } label: { Label(item.name, systemImage: item.name.lowercased().hasSuffix(".exe") ? "app.dashed" : "terminal") }
                    .disabled(work.pausesBackgroundWork)
                }
            }
            if truncated { Text("This folder has more than 512 items. Select a smaller game folder in Files to browse all its contents.").foregroundStyle(.secondary) }
            if items.isEmpty && error == nil { Text("No game programs or folders here.").foregroundStyle(.secondary) }
        }
        .navigationTitle(name)
        .task(id: work.pausesBackgroundWork) {
            guard !work.pausesBackgroundWork else { return }
            do {
                let lease = try ExternalGameLibrary.shared.acquire(rootID); defer { lease.release() }
                let listing = try ExternalGameLibrary.browse(relative, lease: lease)
                items = listing.items; truncated = listing.truncated; error = nil
            } catch { self.error = error.localizedDescription }
        }
    }
}
