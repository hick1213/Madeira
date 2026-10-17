// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 125hz
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
//
import Foundation
import SwiftUI

/// Steam's directory or bundled HTTPS alternatives supply the hosts; this page never accepts an
/// arbitrary address or starts a second Steam sign-in connection.
@MainActor struct SteamDownloadSettingsView: View {
    var appID: UInt32?
    @State private var selectedHost = SteamContentSourceStore.shared.selectedHost
    @State private var servers: [SteamContentServer] = []
    @State private var history: [String: SteamContentHostRecord] = [:]
    @State private var loading = false
    @State private var loaded = false
    @State private var displayedCellID: UInt32?
    @State private var displayedAppID: UInt32?
    @State private var error: String?
    @State private var refreshID = UUID()

    var body: some View {
        Form {
            Section {
                Button { select(nil) } label: {
                    selectionRow("Automatic", selected: selectedHost == nil)
                }.foregroundStyle(.primary)
                if let selectedHost {
                    LabeledContent("Selected CDN", value: selectedHost)
                        .font(.caption).textSelection(.enabled)
                    if loaded, error == nil, !servers.contains(where: { $0.host == selectedHost && $0.downloadEligible }) {
                        Text("The selected node is unavailable for this download. Madeira will use automatic selection until it is available again.")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            } header: {
                Text("Download source")
            } footer: {
                Text("Automatic tests every eligible Steam node with verified download data and chooses the fastest successful node. A manual choice is used first, with automatic fallback if it fails. Changes apply to new downloads and when a paused download resumes.")
            }
            Section {
                if loading {
                    HStack(spacing: 10) { ProgressView(); Text("Loading Steam nodes…") }
                }
                if let error {
                    Text(error).foregroundStyle(.orange)
                }
                if loaded, !loading, servers.isEmpty, error == nil {
                    Text("Steam returned no CDN nodes for this download. Refresh to try again.")
                        .foregroundStyle(.secondary)
                }
                if !servers.isEmpty {
                    if let savedAt = servers.first?.cachedAt {
                        Text("Showing the Steam node list saved \(savedAt.formatted(date: .abbreviated, time: .shortened)).")
                            .font(.caption).foregroundStyle(.secondary)
                        if let failure = servers.first?.refreshFailure {
                            Text("\(failure.message) The previous list is retained; availability and speed will be checked during download.")
                                .font(.caption).foregroundStyle(.orange)
                        }
                    } else if servers.contains(where: \.isFallback) {
                        Text("Steam's directory is unavailable. Showing bundled Steam alternatives; availability and speed will be checked during download.")
                            .font(.caption).foregroundStyle(.orange)
                        if let failure = servers.first?.refreshFailure {
                            Text(failure.message).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    ForEach(servers) { server in
                        Button { select(server.host) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                selectionRow(server.host, selected: selectedHost == server.host)
                                Text("\(server.scheme.uppercased()) · \(server.type)")
                                    .font(.caption).foregroundStyle(.secondary)
                                if server.directorySource == .mainlandSupplement {
                                    Text("Mainland supplemental recommendation from Steam")
                                        .font(.caption).foregroundStyle(.secondary)
                                } else if server.directorySource == .bundledMainland {
                                    Text("Bundled mainland Steam recommendation")
                                        .font(.caption).foregroundStyle(.secondary)
                                } else if server.directorySource == .bundledHTTPS {
                                    Text("Bundled Steam HTTPS alternative")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                if server.cachedAt != nil, let failure = server.refreshFailure {
                                    Text("\(failure.message) Using saved recommendations.")
                                        .font(.caption).foregroundStyle(.orange)
                                }
                                if let reason = server.cannotDownloadReason {
                                    Text(reason).font(.caption).foregroundStyle(.orange)
                                }
                                Text(speedText(for: server)).font(.caption).foregroundStyle(.secondary)
                            }.padding(.vertical, 2)
                        }.foregroundStyle(.primary).disabled(!server.downloadEligible)
                    }
                }
            } header: {
                Text("Available CDN nodes")
            } footer: {
                Text("All nodes returned by Steam are shown, including session recommendations and a mainland supplement requested with a public IP geolocation override. Recommendation region does not confirm a node’s physical location. HTTP partners and unavailable entries retain their reasons. The last complete list is kept for refresh failures. When both directory requests fail, bundled mainland and HTTPS alternatives supplement the saved lists. Successful empty lists and app restrictions remain authoritative. Speeds are from verified transfers on this device; a node without a measurement has not been tested yet.")
            }
        }
        .navigationTitle("Steam download source")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Refresh", systemImage: "arrow.clockwise") { refreshID = UUID() }
                    .disabled(loading)
            }
        }
        .task(id: refreshID) { await reload() }
    }

    private func selectionRow(_ title: String, selected: Bool) -> some View {
        HStack {
            Text(title).multilineTextAlignment(.leading)
            Spacer()
            if selected { Image(systemName: "checkmark").foregroundStyle(.tint) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func select(_ host: String?) {
        guard SteamContentSourceStore.shared.select(host) else {
            error = "This Steam node cannot be selected. Refresh the list and try again."
            return
        }
        selectedHost = SteamContentSourceStore.shared.selectedHost
    }

    private func speedText(for server: SteamContentServer) -> String {
        guard let record = history[server.url], record.bytesPerSecond.isFinite, record.bytesPerSecond > 0 else {
            return "Not measured yet"
        }
        let speed = ByteCountFormatter.string(fromByteCount: Int64(min(record.bytesPerSecond, Double(Int64.max / 2))),
                                              countStyle: .file)
        return "Last measured: \(speed)/s"
    }

    private func reload() async {
        loading = true; error = nil
        selectedHost = SteamContentSourceStore.shared.selectedHost
        let cellID = SteamOwnedLibrary.shared.contentDownloadCellID
        if displayedCellID != cellID || displayedAppID != appID {
            servers = []; history = [:]; loaded = false
        }
        displayedCellID = cellID
        displayedAppID = appID
        if let cached = SteamContentSourceDirectory.cachedServers(cellID: cellID, appID: appID) {
            apply(cached, cellID: cellID)
        }
        do {
            let current = try await SteamOwnedLibrary.shared.contentDownloadSources(appID: appID)
            guard !Task.isCancelled else { return }
            apply(current, cellID: cellID)
            loading = false
        } catch {
            guard !Task.isCancelled else { return }
            self.error = "Could not load Steam nodes: \(error.localizedDescription)"
            loading = false
        }
    }

    private func apply(_ current: [SteamContentServer], cellID: UInt32) {
        servers = current
        history = [:]
        for server in current {
            history[server.url] = SteamContentSourceStore.shared.history(for: server.host, cellID: cellID, scheme: server.scheme)
        }
        loaded = true
    }
}
