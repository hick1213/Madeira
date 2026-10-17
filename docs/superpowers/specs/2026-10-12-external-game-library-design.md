# External USB game library

User request: add an external USB-drive game library while preserving existing Steam functionality. Implement within the supplied AGENTS autonomy rule; routine local design and reversible source changes do not require renewed approval.

## User behavior

Add an External game library entry point beside the existing internal Add game browser. The iOS Files folder picker grants access to a user-selected game folder on a USB drive. The user browses that folder and explicitly selects an EXE/BAT/CMD; the resulting game joins the existing library with an external-drive badge. Game data remains on the selected drive. No recursive whole-disk discovery, copying, Steam account/download changes, or bulk file writes.

Registered roots are listed with their display name and a browse/relink action. Unavailable drives retain games, UUIDs, covers and launch settings. Opening or playing one reports reconnect/reselect rather than deleting it. Re-selecting a root preserves its ID and all game IDs. Removing a registered root is not part of this change; existing Remove game removes only its library entry as before.

This direct-launch interpretation is the implementer's call based on the user's request for a USB library; the user has not specified a copying workflow. Current Steam/Epic/internal entries have no external root field and keep their current paths and provider behavior.

## Implementation boundaries

- New optional LibraryEntry.externalLibraryID preserves version1 decoding. External relativePath is relative to its authorized root; computed Windows/launch paths prepend madeira-external/<rootUUID>/ inside C:.
- Foundation-only ExternalGameLibrary core validates persisted bounded roots/bookmarks and every component of a relative path. Roots are selected through the existing minimal-bookmark folder-picker pattern. Access scopes are leased for browsing/metadata and retained across launch preflight, JIT and actual Wine execution.
- Wine, wineserver and child processes run as pthreads in Madeira. No security grant transfer to JIT extension is required. Do not claim scope works on a USB drive until physical testing.
- Map only registered roots through app-owned symlinks under wine/drive_c/madeira-external/<rootUUID>. Keep generic executable(String) containment unchanged; introduce executable(for:entry) and root-aware inspect. Never overwrite existing directories/files/unrelated symlinks when preparing the mount. Mount target must be the currently resolved, granted root; no global symlink exception.
- Prefer leases acquired before preflight and transferred into a retained successful-session scope. Release every denied/preflight failure/cancelled preparation. Conservative successful scope lifetime through app termination is acceptable until source/runtime proves all child Wine threads have drained; do not release because a UI page closes.
- Directory browser uses bounded immediate enumeration, rejects symlink escapes and provides explicit unavailable/error states. No security-scoped path stored as raw persistence fallback.
- Cover/settings/Steam artwork matching remain existing mechanisms. Metadata scan uses authorized root boundary and scoped lease and pauses during game mode. Local save snapshots keep their O_NOFOLLOW rules; external installation-directory save snapshots are not enabled by this change. Windows-profile saves may remain inside Madeira.
- Batch/desktop/services/working-directory launches keep existing C: bridge semantics. Existing global HUD, iCloud saves and game mode changes stay intact. No Wine/FEX native changes required.

## Verification

Host tests exercise actual Foundation path validation/root persistence/mount behavior: malformed/oversized config, old LibraryEntry decoding, separators/traversal/absolute/NUL paths, sibling-prefix and symlink escape, external/internal Windows path identity, relink IDs, mount collision preservation, failed-preflight scope release and successful scope retention. Use real temporary directories and symlinks rather than mocked containment. Scope provider may be injected only for lifecycle accounting, clearly separate from physical authorization proof.

Build the real iOS target and inspect the signed package. Preserve the current build16 IPA as an independent verified artifact; the USB change produces a distinct build/version artifact. Do not replace a running device app or claim installation/gameplay without actual evidence.

Required iPad tests: Files picker and permission; reopen after app restart; PE plus sibling DLL/assets reads; relative CWD; x86/x64 game start; shortcut and optional desktop/batch/services mode; disconnect/reconnect/relink; read-only drive; launcher children retaining access. These remain unverified if the drive is absent or no user interaction is available.
