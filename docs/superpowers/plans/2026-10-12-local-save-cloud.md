# Local save cloud synchronization implementation plan

> **For agentic workers:** Implement only assigned files, adapt to shared changes and preserve all existing work. Do not commit, publish or access real cloud credentials/files without the user's Files selection.

**Goal:** Give local/imported games manual save upload/download through a user-selected iCloud Drive folder, confirming before replacement.

**Architecture:** A bounded native snapshot module validates and atomically restores one configured directory. A coordinated Files/bookmark model presents Settings controls and owns the GameSessionWork save token. Existing ZIP import and Steam Cloud remain separate.

**Tech Stack:** Swift Foundation/CryptoKit/Darwin, SwiftUI/UIDocumentPicker, NSFileCoordinator, existing main-actor session coordinator.

**Spec:** docs/superpowers/specs/2026-10-12-local-save-cloud-design.md

## Constraints and review focus

- Only local game directories explicitly configured by the user; no guessed per-game save path or broad AppData snapshot.
- No new iCloud provisioning/container registration; scoped minimal bookmarks as documented by Apple.
- No partial restore, unconfirmed overwrite, archive trust bypass, or false server-upload success.
- Verify malformed archives and live path/link/file changes at actual native filesystem boundaries.
- Prefix operation and Play must exclude each other including JIT/Dock preparation and page dismissal.
- External reports/builds; APFS-only swap fixtures use exact internal temporary directories and finally cleanup.

## Tasks

- [ ] Add executable failing native tests for current missing safe snapshot/restore capability; compile real production core with explicit fixture paths.
- [ ] Implement LocalSaveSnapshot.swift: capture, bounded encode/decode/validation, fingerprint and confirmed atomic merged restore with pre-replacement backup.
- [ ] Implement LocalSaveCloud.swift and LocalSaveCloudView.swift: minimal bookmark, coordinated bounded read/write, immutable versions, local-game path configuration, conflict confirmation and truthful provider status. Add only the Settings navigation and Xcode source references required.
- [ ] Run native core and affected regression checks, independent code/security/Python reviews; fix actual findings.
- [ ] Full signed iOS build, external package roundtrip and installation on the current iPad when idle; validate user-selected folder and upload/download confirmation through actual device interaction. Report unavailable second-device verification explicitly.
