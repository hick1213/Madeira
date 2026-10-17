# External game library implementation plan

Spec: docs/superpowers/specs/2026-10-12-external-game-library-design.md.
Scope: user-requested USB game library, preserving Steam and existing App work. Routine reversible implementation is authorized by supplied AGENTS. No commit/publication/force-stop/automatic app replacement.

1. Add bounded Foundation roots persistence, bookmark resolution/scoped leases, registered mount ownership and safe canonical path validation. Write meaningful real-filesystem tests first for traversal, symlink escape, mount collision, relink identity and lease cleanup.
2. Add optional externalLibraryID to existing entries and root-aware executable/inspect APIs. Internal executable(String), Steam/Epic identities, provider ordering and downloads retain their behavior. Verify old-entry decoding and stable external Windows shortcuts.
3. Add folder picker, registered-root browser/relink UI, existing-style executable browsing and external-drive badge/unavailable messages. No automatic recursive scan or file copying.
4. Acquire and hold scope before launch/JIT/PE preflight; transfer successful lease into conservative session lifetime and release all failed/cancelled launch paths. Scoped metadata reads respect game mode. Refuse relink during running/prepared/retained sessions, validate selected root against existing relative executable paths, never overwrite ambiguous mount contents.
5. Independent code/security review and fix verified issues. Build actual iOS target, inspect signed separate build17 package without replacing verified16 artifacts. Physically verify folder access and Wine sibling DLL/assets/CWD on current iPad only when USB connected, required user interaction available, and game safely idle. Host tests are not USB proof.

Worker ownership: app/Madeira/{ExternalGameLibrary.swift,ExternalGameLibraryView.swift,Library.swift,ContentView.swift}, app/Madeira.xcodeproj/project.pbxproj file registrations only, and owned app/Scripts/test-external-game-library.*. Root owns docs/build/package/coordination. Do not edit native Wine/FEX, Steam/Epic provider sources, LocalSaveCloud/Snapshot, GameSessionWork or any original game/artifact. Preserve all preceding dirty changes in shared files.
