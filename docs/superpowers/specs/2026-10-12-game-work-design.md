# Game session background work

The user requested automatic game mode when entering any game, with unnecessary Madeira work paused and restored afterwards. This extends existing Steam session behavior; it does not change the game's networking or require private system APIs. GCSupportsGameMode is already enabled in Info.plist. The OS decides when its own Game Mode is active.

Use one main-actor coordinator for launch preparation, Library sessions, legacy Wine sessions and manual save operations. Preparation must survive asynchronous JIT/Dock startup. Ending a detach/preparation sequence is not the end of a running game: release only when preparation is finished, Library.current is nil and both native Wine and wineserver have stopped. Failed startup releases its own preparation. An idle coordinator stops its timer.

Keep Steam's connection gate, pause/resume ownership, Dock account handoff and Cloud quit-upload exception intact. Epic downloads keep their queued/waiting/paused distinction. Cancel or defer Epic catalogue refresh, install-record scans, metadata scans and hidden library polling. Resume only work paused by this session, not manually paused or failed downloads. In-flight stale refresh results cannot publish after entering game mode or after account replacement. Keep rendering, audio, input/hotplug, JIT setup and native runtime, privacy filtering, Dock reporting and exit detection operational.

Manual save operations acquire an exclusive coordinator token while no session or preparation runs. Launch attempts during this operation report that saves are being synchronized; releasing a different token does nothing. This provides the shared guard required by the separately requested iCloud feature.

Validation: executable native lifecycle tests for library and legacy sessions, fast failure, running server after detach, repeated transitions, manually paused queues, stale catalogue tasks and save/launch exclusion. Existing Steam Cloud and Epic account/download model tests remain relevant regressions. A full signed iOS build and real iPad launch must follow; host doubles cannot establish physical device behavior.
