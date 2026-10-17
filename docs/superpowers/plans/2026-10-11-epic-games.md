# Epic Games Implementation Plan

> **For agentic workers:** Execute bounded, independently owned tasks, with focused production-code tests and independent final review. User approved the design and explicitly requested direct execution; no repeated implementation approval or commit is needed.

**Goal:** Add native Epic account login, owned Windows games, verified downloads/resume and launch without changing Steam behavior.

**Architecture:** Independent Swift Epic account/API, manifest/download/install and library/view modules use Epic services. Existing LibraryEntry and JIT/Wine paths receive optional Epic identity and ephemeral authenticated arguments. Steam authentication, depots, records, Dock and Cloud retain their existing protocols and state.

**Tech Stack:** Swift/Foundation/SwiftUI, Security Keychain, CryptoKit, system zlib, existing Wine bridge.

**Spec:** `docs/superpowers/specs/2026-10-11-epic-games-design.md`.

## Global Constraints

- Preserve existing uncommitted work, games, saves, Steam accounts and signing; do not edit FEX.
- Pure IPA unpack/check directories: external `app/Build/Products/IPAInspection/`; APFS-required steps use exact task-owned internal directories, archive needed evidence externally and clean after use.
- Epic tokens in device-only Keychain; one-use credentials never in persistent profiles, launch batch files or logs.
- No purchase, publishing or automatic store claim; Epic cloud saves are a follow-up.
- Real login/download/launch on He Ke's iPad required; report incomplete runtime evidence honestly.

## Review Focus

- Account changes during API/download work must cancel or reject stale completion.
- Corrupt/oversized BuildPatch data, path traversal and symlinks must fail before installation is marked complete.
- CDN redirects must not receive Epic bearer credentials; manifest/chunk/file hashes verified.
- Cancellation/checkpoint/restart must not lose valid finished files or falsely mark partial install ready.
- Secrets in wrapped command lines/logs and parameter quoting must be covered by production-code tests.

### Task 1: Epic account, API and game metadata

Ownership: `app/Madeira/EpicAccount.swift`, `EpicAPI.swift`, `EpicCatalog.swift`; corresponding `tests/host/check-epic-account.py`/`check-epic-api.py`. No existing Steam edits.

Interfaces: `EpicGame` Codable/Identifiable/Hashable/Sendable with appName, namespace, catalogItemID, title, artworkURL (optional), buildVersion (optional); id=appName. `EpicManifestLocation` Sendable with url and chunkBaseURLs. `EpicAccount` @MainActor shared ObservableObject with signedIn, displayName, accountID, busy, error, login(code:) async throws, signOut(), restored session. `EpicAPI` @MainActor shared with games() async throws -> [EpicGame], manifest(game:) async throws -> EpicManifestLocation, exchangeCode() async throws -> String.

- [ ] Write/run RED tests for actual response decoding, auth expiry, request routing/credential boundaries, cancellation and pagination.
- [ ] Implement browser auth-code login, device-only Keychain, refresh single-flight/generation, Windows assets and catalog metadata/manifest, one-use exchange.
- [ ] Run focused tests and report public-client/protocol provenance accurately.

### Task 2: BuildPatch manifest, install and download

Ownership: `app/Madeira/EpicManifest.swift`, `EpicInstall.swift`, `EpicDownloader.swift`; `tests/host/check-epic-download.py`. No Library/ContentView/API edits.

Interfaces: `EpicManifest.parse(Data) throws` exposes appName, launchExecutable, launchCommand, fileManifestList, totalBytes. `EpicDownloader.install(manifestData: Data, baseURLs: [URL], destination: URL, progress: @escaping @Sendable (Int64, Int64) -> Void) async throws -> EpicInstallResult`. Result contains executable, arguments, totalBytes (paths relative to install root). Installer checkpoint stays under destination and is validated against the manifest. Downloader must support cancellation/verified-file resume and bounded memory.

- [ ] Write/run RED tests for real JSON/binary manifest formats, chunk decompression/hashes, final file hashes, traversal/symlink paths, corruption, resume and cancellation.
- [ ] Implement native manifest/chunk parsing, CDN-only network session, per-file assembly and atomic validated checkpoint.
- [ ] Run focused tests; no bearer credentials given to downloader.

### Task 3: Epic library, UI and safe launch integration

Ownership: root agent `EpicLibrary.swift`, `EpicViews.swift`, optional Epic identity/upsert/grouping/settings in `Library.swift`, Epic launch in `ContentView.swift`, selective bridge redaction and ephemeral arguments, Xcode wiring and tests. Preserve all existing Steam branches.

- [ ] Write/run RED tests for old profile decode, Epic identity registration, queue/account lifecycle and secret-free launch/log handling.
- [ ] Add Epic section, details, login browser/code entry, independent queue/status/install records and background lifetime.
- [ ] Register validated install in LibraryModel with existing user settings retained; authenticated args are in-memory one-use values only.
- [ ] Add exact bridge/redaction needed for Epic; do not put authorization in Codable LibraryEntry or generated .bat files.
- [ ] Run Steam library/sign-in/download/cloud regressions and normal-game launch checks.

### Task 4: Review, build, package, device validation

- [ ] Independent code, Python and security review; fix findings and build failures.
- [ ] Full iOS build with existing team/JIT/helper entitlements, resources and higher recognizable build number.
- [ ] Stage/package and ordinary extraction/check on external disk; required APFS work minimal and cleaned after outputs copied.
- [ ] Install on He Ke's iPad; user performs Epic login on device and chooses a small owned game.
- [ ] Verify real library/download/resume/launch and unchanged Steam entry/runtime; document any user/account/device blocker.

No commits or publication in this task. Any unavoidable missing user login is a runtime-verification blocker, not a reason to stop implementing and building independent work.
