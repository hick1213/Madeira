# Epic Games

Last updated: 2026-10-14.

Madeira's first Epic integration adds a separate account, owned Windows game
library, downloads and game launch. It uses native Swift and the existing Wine,
FEX, graphics and JIT runtime. It does not install the desktop Epic Games
Launcher on iOS.

App44 is installed on Fang Dong's M5 iPad, with system bundleVersion44 verified. Native Epic sign-in and the owned-library UI were observed in App37. The authenticated-response CDN rule introduced in App38, scoped cancellation/cleanup in App39 and full-node probing/download shelf in App41 remain included. App44 adds the live received-content speed caption in both the download shelf and game details, separate from CDN measurements. Speed is transient, updates at most once per second and expires after3s without data; cache/file verification is excluded. Focused tests, independent review, build, signature, package and installation checks pass. **Actual44 speed display, complete real Epic download/resume and authenticated game launch, including FM2024 ownership-token service acceptance, remain unverified.**
These checks do not establish compatibility with every Epic game or with FM26.
Packaging and installation are tracked separately; an older installed or
published IPA may not contain this integration.

## Sign in

1. Open the library's **Settings › Epic Games › Sign in to Epic Games**.
   The library's **Epic Games** section also has a sign-in button.
2. Tap **Open Epic login**. Complete sign-in and any verification on Epic's
   official website in the browser sheet.
3. Copy the `authorizationCode` from the page that follows. Return to Madeira
   and paste it into **Paste authorization code**. Pasting the whole JSON
   response is also supported.
4. Tap **Sign in**. Madeira refreshes the account's game library.

The code is single-use. If it has expired or was already used, reopen Epic login
to obtain a new one. Automatic return from the website is not implemented.
Your Epic password and verification codes are entered on Epic's website;
Madeira does not ask for them.

If Epic returns `corrective_action_required` with
`PRIVACY_POLICY_ACCEPTANCE`, Madeira displays a privacy-policy instruction
instead of treating that JSON as an authorization code. Tap **Open Epic account**
to visit the fixed [official account page](https://www.epicgames.com/account/personal),
review the policy and complete any prompts yourself. Then return to Madeira,
tap **Open Epic login** again and obtain a new authorization code. This also
applies when the same response arrives during the account request.

Madeira does not accept the policy on your behalf. It neither uses nor displays
the response's continuation value and does not open URLs supplied by that
response. The response handling passed host tests and security review; the
actual website policy-confirmation and subsequent login flow remain unverified.

**Settings › Epic Games** shows the signed-in name and **Refresh Epic library**.
**Sign out of Epic Games** clears Madeira's Epic session and pauses its queued
work. It also cancels pending game launches; an ownership-token file already
claimed by the runtime remains available until that session ends. Sign-out does
not delete installed games or saves. Downloading and an authenticated game
launch require sign-in to the corresponding Epic account.

## Download and play

1. In the library's **Epic Games** section, select a game, then tap
   **Download Windows version**.
2. Its download sheet shows progress and **Pause** or **Resume download**.
   Completed files and cached chunks are verified before they are reused.
   An incomplete download is not registered as an installed game.
3. When installation finishes, the card opens **Game details**. Use **Play**
   and the usual Madeira resolution, controller, touch and JIT settings.

Games are installed in their own directories under
`Files › Madeira › wine › drive_c › Program Files › Epic Games`. Directory
names contain hashes of the account and game identifiers. Epic installation,
queue and library-cache records live separately in `Documents/epic/`.
Do not edit those records to mark a partial download as complete.

Epic downloads use a separate queue. While a Madeira game session is running,
queued and active Epic downloads wait; the waiting queue can continue after the
session ends. Build16 extends this to launch preparation, Epic catalog/metadata
refreshes and install scanning through the shared `GameSessionWork` coordinator.
Waiting work resumes only after Wine and its server have actually stopped;
manual pauses remain manual. This scheduling passed host tests but has not yet
been exercised during a real game on the iPad. iOS permits only limited background execution. When its allotted
time ends, the active download pauses. After restarting Madeira, saved jobs
are restored as paused; use **Resume download** to continue.

Continuation is limited to the same manifest. This version does not overwrite
an existing installation when Epic returns a different manifest, and it does
not yet provide game updates, repair, uninstall or an Epic CDN picker.

Manifest downloads automatically try alternate URLs supplied by Epic's
authenticated response after a fetch failure. They stop immediately on
cancellation, and downloaded bytes must match the response's SHA1 before parsing
or installation. A digest mismatch fails the job rather than trying another URL.

## Installation-record startup error

On Fang Dong's M5 iPad, diag13 reported **Could not read Epic installation**.
The diagnostic identified `open-ancestor-0 errno=1`: opening an ancestor outside
the app container was denied by the iOS sandbox. It did not establish that the
Epic game files or Steam data were corrupt.

Build14 opens the app container supplied by the OS directly, pins its directory
descriptor, then walks only the relative directories beneath it using
`O_NOFOLLOW`. The same helper serves Epic's records and installer. Unsafe roots,
linked writable components and linked record files are still rejected; regular
record files must have a single hard link. This preserves the path protections
without requiring access to the filesystem root or its private ancestors.

Build14 was installed and launched on that iPad. Its startup log contained no
Epic errors and `Documents/epic/` existed with no record files yet. That confirms
the startup-directory fix, not a completed Epic login or installation. The
visible Steam checkpoint showed 69 owned games and the existing GuJian 3
download at about 8 GB of 37.35 GB. It does not establish that every Steam game
or Steam Cloud operation has passed regression testing.

## Current compatibility limits

- Windows games with a direct executable or Madeira's desktop launch mode are
  supported by the launch path. Their ordinary manifest launch arguments are
  preserved; an exchange code is obtained for the current launch.
- Ubisoft Connect, EA and other additional-launcher flows are not implemented.
  Games whose catalog declares such a requirement display that limitation.
- Games with the catalog `OwnershipToken` requirement now request Epic's
  ownership token and pass its temporary file path through `-epicovt`.
  This implements the launch protocol; FM2024 service acceptance and gameplay
  have not yet been verified.
- Authenticated `.cmd`/batch launch and the **Start services** launch option are
  not supported for Epic entries.
- Downloads support unencrypted chunk-based BuildPatch content, JSON and binary
  manifests, and zlib-compressed chunks. Encrypted content, file-data manifests
  and manifest feature levels above 24 are rejected.
- Epic Cloud saves, store purchases, automatic free-game claiming and social
  features are not included. **Epic saves are not uploaded to Steam Cloud.**

Adding a store does not add Windows kernel drivers, anti-cheat support or every
game's graphics/runtime requirements. A downloaded game may still fail in
Madeira's Windows runtime. Its diagnostic log is needed to identify that
separate compatibility problem.

## Account isolation and privacy

Epic uses its own device-only Keychain item, separate from Steam. The persisted
credential is the refresh token; the account ID, display name and refresh
expiry accompany it. Access tokens and one-use authorization/exchange codes
are not persisted as game settings, scripts or download records. Launch
authorization goes through a private, temporary argument channel, and Madeira's
launch logging redacts the credential.

Ownership-token bytes are stored only in a randomly named, owner-readable and
owner-writable (`0600`) `.ovt` file under
`Documents/wine/drive_c/.madeira-epic-ownership/`. The file is excluded from backup
before credentials are written; the game receives its Windows path, not its
contents, in launch arguments. Creation and validation reject linked paths and
check file identity. Cancellation or account changes before native launch revoke
the pending launch and remove the file. After the runtime claims it, the file is
retained until launch fails or the shared session coordinator confirms that
preparation, the library session, Wine and its server have ended. Debugger detach
alone does not remove it. Startup cleans only matching stale token files, while
preserving links and unrelated files.

Library caches and download/install records are tied to the Epic account.
Account changes cancel old in-flight work before a different account's queue
is used. Content-server requests use a separate transport from account
requests and do not carry Epic account authorization headers.

Steam sign-in, depot downloads, Madeira Dock and Steam Cloud remain their own
modules. Epic reuses the shared library and game runtime, so device regression
of Steam remains part of release validation.

If necessary, hide Epic's library and settings entry with
`env.MADEIRA_EPIC_LIBRARY = 0` in `Documents/madeira.cfg`. This does not delete
its data or sign the account out.

## Implementation and validation

- `app/Madeira/EpicAccount.swift`, `EpicCatalog.swift` and `EpicAPI.swift`:
  browser-code sign-in, Keychain session, owned library, download and ownership
  token endpoints.
- `app/Madeira/EpicManifest.swift`, `EpicDownloader.swift` and
  `EpicInstall.swift`: manifests, content verification and resumable installation.
- `app/Madeira/EpicDirectory.swift`, `EpicRecords.swift`, `EpicLibrary.swift`,
  `EpicLaunch.swift` and `EpicViews.swift`: account-scoped records, queue lifecycle,
  private launch arguments, temporary ownership-token lifetime and the interface.
  `ContentView.swift` carries the launch permit through native startup;
  `EpicLogFilter.h` supplies native log redaction.
- Focused checks are in `tests/host/check-epic-account.py`,
  `check-epic-download.py`, `check-epic-library.py`, `check-epic-records.py` and
  `check-epic-launch.py`. They exercise production code and hostile/interrupted
  cases; they do not substitute for real Epic service and game tests.

Pure IPA extraction and inspection use the external disk's task directory under
`app/Build/Products/IPAInspection/`. Steps that require APFS use a recorded,
task-owned internal temporary directory. Archive necessary outputs and logs,
then promptly clear that temporary directory after its processes finish. See
[Building from source](BUILDING.md#temporary-storage-for-ipa-checks-local-workflow).

The 2026-10-12 installed checkpoint is 0.1.3/build15 on Fang Dong's M5 iPad.
SDK app information confirmed version15 and launch succeeded. It includes the
build14 container-directory and privacy-policy response changes above plus the
global performance overlay. Its full build, IPA CRC, signature and resource
checks passed. The startup-directory fix has actual build14 device evidence;
overlay interaction and real Epic service/game tests were pending at that
checkpoint.

Build16 adds shared background-work scheduling and a separate, manual
local-game iCloud save feature. Epic entries are excluded from that save feature;
it does not implement Epic Cloud. Code and security reviews, focused host tests
and Steam/Epic regressions passed, and the full build succeeded. Build16 passed
IPA CRC, strict/deep signature and resource validation. At that checkpoint it
remained pending installation and the iPad's last confirmed version was15.
Host checks, app startup and packaging alone do not establish a real Epic
account's owned library, download, resume or authenticated game launch.

## He Ke device validation and artwork update, 2026-10-12

Build25 was installed and launched on He Ke's iPad13,4 (iPadOS27.0.1).
The user completed real Epic sign-in and confirmed the library loaded; the
captured interface displayed392 games. Download, install and game launch were
not established by that login/library result.

Build26 now bounds covers to their allocated frames, reuses the shared decoded
thumbnail cache with display-sized buckets, and matches Steam's card/list and
cover-header download layout. It passed the app build, strict signature and
bundle checks, and was installed with device version26 read back. The system
Face ID prompt/black screen initially blocked the final device visual and scroll
check. Evidence: `issue/multi-fixes-20261012/`.
After the user unlocked it, the build26 library and bounded Steam cards were
visible. That screenshot remained at the Steam section; Epic card/detail visual
confirmation came in the later build27 evidence below.

## Cover regression and download diagnostics, builds27–29

The build26 blank covers were reproduced as a SwiftUI mounting issue: an empty
loading view prevented the artwork task starting. Build27 supplies a real clear
placeholder. He Ke device screenshots now show Epic covers in both cards and
detail headers. Scroll effects pause while scrolling; smoothness remains
unverified because Instruments could not connect for a hitch trace.

Islets still fails in build28 at `manifest-address`, before manifest retrieval.
Build28's separately validated builder-name/public-ID correction did not fix
that observed failure. Build29 preserves validation and adds sanitized parser
reasons; it passed188 native account/API checks, build/signature/bundle checks
and independent reviews. Device installation was retried successfully and
version29 read back; the device was then locked, so the actual rejection reason
was not collected. Real download/install/launch are not marked passed.

FM2024 has the catalog `OwnershipToken` requirement. Builds through 29 rejected
that startup flow as unsupported; the implementation below removes that local
limitation without bypassing Epic's ownership checks.

## Manifest fallback and ownership-token startup, build33

The manifest parser now accepts three additional exact CDN hosts:
`egdownload.fastly-edge.com`, `egs-cloudfront-chunks.epicgamescdn.com` and
`cloudflare.epicgamescdn.com`. It retains up to 16 manifest URLs from the
authenticated response for the fallback described above. HTTPS, URL restrictions,
credential-free content transport, manifest/file/chunk hashes and installation
path protections remain enforced. These fix reproduced parser and fallback
defects; a real Islets download has not yet confirmed their effect on its failure.

Ownership-token startup now sends the signed-in account's validated namespace
and catalog item to Epic's fixed ownership-token endpoint. Its opaque, nonempty
response is limited to 64 KiB and handled by the temporary-file lifecycle above.
The FM2024 unsupported-token UI block has been removed. Epic still decides
whether to issue the token; this is not an ownership or purchase bypass.

If native startup fails, the external game path fails its final validation, or
the Epic launch permit is revoked, the launch preparation stops the idle
wineserver it created and releases its token. The successful launch path and an
unrelated pre-existing server are preserved, so failed preparation can be retried.

Independent code, security and Python reviews completed after the failed-start
cleanup fix. Recorded checks include 213 native account/API checks, 34 library
scenarios with 210 assertions in both Debug and optimized runs, and five launch
tests covering token files, cancellation/claim races and failed-start cleanup.
Build33 passed the full iOS build, signature and bundle checks. It remains a
local app build awaiting installation; those results do not establish real Epic
download, ownership API acceptance, token-file consumption or game compatibility.
Evidence: [CDN report](../issue/multi-fixes-20261013/epic-cdn/REPORT.md),
[ownership report](../issue/multi-fixes-20261013/epic-ownership/REPORT.md) and
[build33 bundle check](../issue/fangdong-resume-20261013/build33-bundle.json).

## Protocol references and license

The integration references the Launcher protocol documented by
[Legendary](https://github.com/legendary-gl/legendary) and the integration
approach of [Heroic](https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher).
The new native Madeira implementation is **GPL-3.0-or-later**. It does not
embed Legendary's Python, Heroic's Electron application or Epic's proprietary
Launcher binaries, and it does not assert a Converter Exception for this code.
Epic's publicly distributed Launcher client identifiers are protocol metadata,
not a user's password, account token or a private Madeira application key.

This third-party service integration can require changes when Epic changes its
Launcher interfaces. Component attribution is recorded in
[Third-party notices](../THIRD-PARTY-NOTICES.md).

## General download origins — App38 candidate

All games use CDN origins returned by Epic's fixed authenticated Launcher asset API; vendors need no hardcoded entry. URL structure, explicit local/IP rejection, HTTPS, per-response origins, credential isolation and content digests remain enforced. DNS names are not resolved/pinned as part of admission, so this is not a complete network-destination firewall. The user controls the physical device; the agent only observes/reads logs. Final implementation and verification: [report](../issue/epic-retry-20261014/general-cdn-rule/REPORT.md). App38 is not installed or device-download-verified yet.

App38 installation update (2026-10-14): user approved installation; install38.json and apps-after38.json confirm installation and bundleVersion38 on M5. Initial interrupted call did not start installation; explicit retry succeeded. Runtime download remains unverified until user taps retry. Agent only observes/reads logs; no app launch or UI control. Compilation/testing has ended and no simulator is running. Evidence: issue/epic-retry-20261014/general-cdn-rule/STATUS.json.

## Epic cancel and clear unfinished files — App39 candidate

User selected full unfinished-file/cache cleanup for this download. The confirmation UI drains the real writer, persists cancellation/root identity, and removes only a verified incomplete account/game root. Completed, unknown or changed files are protected; interrupted cleanup remains retryable and cannot resume downloading. Final host suites pass128+10 storage/boundary checks plus cleanup fixtures, and44 model scenarios/270 assertions. Independent code/security/Python review approves. App39 full iOS build, strict deep signature and Steam/WoW64 bundle checks pass; build maxRSS1,378,451,456 bytes. Installed M5 remains38, with an agreed update window and user-driven device testing pending. No agent device control or actual cleanup occurred. [Report](../issue/epic-retry-20261014/cancel-download/REPORT.md).

## App41 installed: all official CDN nodes and unfinished-download shelf — 2026-10-14

User requested all official returned nodes retained and measured before fastest selection, then a separate 正在下载 shelf. Steam now retains HTTP partners and disabled rows with reasons, caches full official metadata with migration, and measures all eligible nodes via serial validated encrypted chunks. Secure controls/tokens remain HTTPS; failed short probes get normal retries. Epic has serial approved-origin probing with verified fastest cache reuse and visible account-scoped results. The shelf shows unfinished Steam/Epic tasks in all library grouping modes; owned Steam journal folders restore paused rows after app update/restart. Epic cancel-and-clear is included.

Actual host final: Steam directory60 assertions; content254; full production install/resume/update golden879; Epic143+10 plus cleanup recovery; library45 scenarios277; partial-discovery6 incl links and foreign IDs. Final code/security/Python reviews approve. App40 build/signature/package passed but was superseded before installation. App41 full build/signature/Steam-WoW64 package/install PASS, with M5 system bundleVersion41 read back; build maxRSS1,379,958,784 bytes using jobs1/Swift-j1. No simulator/native rebuild, no publication/commit, no agent launch/download/cancel/cleanup clicks. Physical shelf view and real fastest CDN await user-operated app open/resume; installed is not runtime proof. [Report](../issue/epic-retry-20261014/all-cdn-nodes/REPORT.md).

App43 update (2026-10-14): installed/read back on Fang Dong M5 with Steam HTTPS chunk fallback after regionalHTTP403 and stale Testing notice cleanup. It retains the previously approved Epic features. Steam focused82/274 and full921 checks, independent reviews and iOS package/install checks pass. This does not establish real Epic or Steam download completion. See [Steam403 report](../issue/epic-retry-20261014/cdn-403/REPORT.md).

App44 live-speed evidence:156 native download checks plus10 direct boundaries and46 model scenarios/293 assertions pass. Ordinary transport fixtures use unchanged production source and URLProtocol; the small slow-stream fixture models declared delegate events, not live packet cadence. Build maxRSS1,376,993,280 bytes with jobs1/j1, no simulator. [Report](../issue/epic-retry-20261014/epic-speed/REPORT.md).

## App45 candidate: bounded download prefetch (not installed)

On2026-10-14 the user's App44 logs showed Akamai median541509B/s and CloudFront691758B/s, with TencentHTTPS failures lacking old error detail. The candidate preserves official origins/HTTPS and the all-node firstmissing benchmark, then fetches up to4distinct verified chunks per window. Reservation2*encoded+decoded targets32MiB; a single supported larger chunk runs alone. Windows scan at most64parts and check cancellation; parent owns all file/cache/checkpoint writes, groups drain on cancel. Live wire speed is aggregated across concurrent responses and safe typed failure codes identify HTTP/TLS/network errors on next retry.

176 native+10boundary checks and46 model/294assertions, finalreviews, singlejobbuild/signature/package pass. App45 is not installed: usertemporarilycannotunlock/connectiPad. Real throughput improvement is unverified. Steam source hashes remain unchanged. [Evidence](../issue/epic-retry-20261014/game-logs-epic-perf/REPORT.md).
