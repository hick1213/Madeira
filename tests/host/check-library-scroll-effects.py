#!/usr/bin/env python3
"""Check actual library scroll policy and SwiftUI animation scheduling on macOS.

No device, account, network, preferences, or production UI interaction is used.
This verifies scheduling behavior, not iPad frame rate or perceived smoothness.
"""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/Library.swift"
STEAM = ROOT / "app/Madeira/SteamGames.swift"

HARNESS = r'''
import SwiftUI
import Foundation
import Darwin
__PRODUCTION__
func phaseIsIdle(_ phase: ScrollPhase) -> Bool { __PHASE__ }
func schedule(_ scrollFrozenTime: Double?, _ reduceMotion: Bool) -> AnimationTimelineSchedule { __SCHEDULE__ }
struct ClockContext { let date: Date }
func renderTime(_ scrollFrozenTime: Double?, _ context: ClockContext) -> Double { __TIME__ }
func showsBlur(_ notDownloaded: Bool, _ scrollFrozenTime: Double?) -> Bool { __BLUR__ }
func disappeared(_ visual: inout LibraryScrollVisualState) { __DISAPPEAR__ }
@main struct Check {
    static func main() {
        var failures = 0
        var assertions = 0
        func check(_ condition: Bool, _ label: String) {
            assertions += 1
            print("\(condition ? "PASS" : "FAIL"): \(label)")
            if !condition { failures += 1 }
        }
        func emits(_ state: LibraryScrollVisualState, reduced: Bool = false) -> Bool {
            var entries = schedule(state.frozenTime, reduced).entries(from: Date(), mode: .normal)
            return entries.next() != nil
        }
        var state = LibraryScrollVisualState()
        check(state.frozenTime == nil && emits(state), "idle starts with an active real SwiftUI clock")
        check(!emits(state, reduced: true), "Reduce Motion stops the clock while idle")
        check(showsBlur(true, nil) && !showsBlur(false, nil), "idle blur applies only to undownloaded covers")
        let busy: [ScrollPhase] = [.tracking, .interacting, .decelerating, .animating]
        for (index, phase) in busy.enumerated() {
            var isolated = LibraryScrollVisualState()
            let began = 100.0 + Double(index)
            isolated.phaseChanged(isIdle: phaseIsIdle(phase), at: began)
            check(isolated.frozenTime == began, "\(phase) freezes from idle")
            check(!emits(isolated), "\(phase) emits no animation schedule entries")
            check(!emits(isolated, reduced: true), "\(phase) and Reduce Motion stay stopped")
            check(renderTime(isolated.frozenTime, ClockContext(date: Date(timeIntervalSinceReferenceDate: began + 60))) == began,
                  "\(phase) keeps the actual draw time fixed as wall time advances")
            check(!showsBlur(true, isolated.frozenTime), "\(phase) omits progressive cover blur")
            isolated.phaseChanged(isIdle: phaseIsIdle(.idle), at: began + 120)
            check(isolated.frozenTime == nil && emits(isolated), "\(phase) resumes on idle")
        }
        state.phaseChanged(isIdle: phaseIsIdle(.tracking), at: 200)
        for (index, phase) in busy.enumerated() {
            state.phaseChanged(isIdle: phaseIsIdle(phase), at: 300 + Double(index))
            check(state.frozenTime == 200, "busy transition to \(phase) retains the first frozen timestamp")
        }
        state.phaseChanged(isIdle: phaseIsIdle(.idle), at: 400)
        state.phaseChanged(isIdle: phaseIsIdle(.idle), at: 500)
        check(state.frozenTime == nil && emits(state), "repeated idle remains live")
        let context = ClockContext(date: Date(timeIntervalSinceReferenceDate: 600))
        check(renderTime(state.frozenTime, context) == 600, "idle draws at the actual timeline date")
        state.phaseChanged(isIdle: phaseIsIdle(.interacting), at: 700)
        check(state.frozenTime == 700, "a new scroll captures a fresh timestamp")
        check(!emits(state), "a new scroll stops the clock again")
        disappeared(&state)
        check(state.frozenTime == nil && emits(state), "actual disappearance cleanup releases a frozen scroll")
        print("Scroll effects assertions: \(assertions); failures: \(failures)")
        exit(failures == 0 ? 0 : 1)
    }
}
'''


def one(pattern: str, source: str) -> str:
    matches = re.findall(pattern, source)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one production expression: {pattern}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional task evidence directory")
    args = parser.parse_args()
    extractor = runpy.run_path(str(ROOT / "tests/host/check-performance-integration.py"),
                              run_name="scroll_effects_extractor")
    extract = extractor["function"]
    source = SOURCE.read_text()
    steam = STEAM.read_text()
    state = extract(source, "struct LibraryScrollVisualState")
    key_start = source.index("private struct LibraryScrollTimeKey")
    key = extract(source[key_start:], "private struct LibraryScrollTimeKey")
    environment = extract(source[key_start:], "extension EnvironmentValues")
    activity = extract(source, "private struct LibraryScrollActivity")
    glow = extract(source, "struct AmbientGlow: View")
    artwork = extract(steam, "struct SteamGameArtwork: View")
    callback = extract(activity, ".onScrollPhaseChange")
    phase = one(r"phaseChanged\(isIdle:\s*([^,\n]+),", callback)
    disappear = extract(activity, ".onDisappear")
    disappear_body = disappear[disappear.index("{") + 1:-1]
    scheduling = one(r"TimelineView\((\.animation\([^\n]+\))\)\s*\{ context in", glow)
    draw_time = one(r"let t = ([^\n]+)", glow)
    blur = one(r"if ([^{\n]+) \{ SteamArtworkBlurSpot\(", artwork)
    page = extract(source, "private var library: some View")
    if ".modifier(LibraryScrollActivity())" not in page:
        raise ValueError("actual library ScrollView does not attach the scroll activity modifier")
    if ".onScrollPhaseChange" not in activity or ".environment(\\.libraryScrollFrozenTime, visual.frozenTime)" not in activity:
        raise ValueError("scroll phase changes do not publish the production state through the environment")
    binding = '@Environment(\\.libraryScrollFrozenTime) private var scrollFrozenTime'
    if binding not in glow or binding not in artwork:
        raise ValueError("ambient glow and Steam artwork must consume the same frozen time")

    variants = {"production": (state, phase, scheduling, draw_time, blur)}
    mutation = state.replace("else if frozenTime == nil", "else")
    if mutation == state:
        raise ValueError("busy-timestamp mutation no longer matches production; update the mutation")
    variants["reset-timestamp-on-every-busy-phase"] = (mutation, phase, scheduling, draw_time, blur)
    variants["treat-deceleration-as-idle"] = (state, f"({phase}) || phase == .decelerating", scheduling, draw_time, blur)
    variants["keep-animation-clock-running-during-scroll"] = (state, phase, scheduling.replace("reduceMotion || scrollFrozenTime != nil", "reduceMotion"), draw_time, blur)
    variants["ignore-reduce-motion"] = (state, phase, scheduling.replace("reduceMotion || scrollFrozenTime != nil", "scrollFrozenTime != nil"), draw_time, blur)
    variants["advance-draw-time-while-clock-paused"] = (state, phase, scheduling, "context.date.timeIntervalSinceReferenceDate", blur)
    variants["keep-cover-blur-during-scroll"] = (state, phase, scheduling, draw_time, "notDownloaded")
    if len(set(variants.values())) != len(variants):
        raise ValueError("a mutation is unchanged or duplicates another variant")

    sdk = subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                         capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    report = {"source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "steam_source_sha256": hashlib.sha256(steam.encode()).hexdigest(),
              "host_sdk": sdk, "device_frame_rate_measured": False,
              "actual_scroll_callback_interacted": False, "runs": {}}
    with tempfile.TemporaryDirectory(prefix="madeira-scroll-effects-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        for name, (policy, idle_expression, clock, time, blur_expression) in variants.items():
            harness = HARNESS.replace("__PRODUCTION__", "\n".join([policy, key, environment, activity]))
            for placeholder, value in [("__PHASE__", idle_expression), ("__SCHEDULE__", clock),
                                       ("__TIME__", time), ("__BLUR__", blur_expression),
                                       ("__DISAPPEAR__", disappear_body)]:
                harness = harness.replace(placeholder, value)
            swift = directory / (name + ".swift")
            binary = directory / name
            swift.write_text(harness)
            command = ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
                       "-warnings-as-errors", "-parse-as-library", "-target",
                       f"{platform.machine()}-apple-macosx15.0", "-sdk", sdk, "-O", str(swift), "-o", str(binary)]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if compiled.returncode:
                raise RuntimeError(f"{name} compile failed:\n{compiled.stdout}{compiled.stderr}")
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            failures = re.findall(r"(?m)^FAIL: (.+)$", result.stdout)
            report["runs"][name] = {"exit_code": result.returncode, "failed_assertions": failures,
                                    "compile_command": command, "compile_timeout_seconds": 90,
                                    "harness_sha256": hashlib.sha256(harness.encode()).hexdigest(),
                                    "execute_timeout_seconds": 20}
            if args.output:
                args.output.mkdir(parents=True, exist_ok=True)
                (args.output / (name + ".txt")).write_text(result.stdout + result.stderr)
            if name == "production":
                print(result.stdout, end="")
                if result.returncode:
                    raise RuntimeError("actual production scroll behavior failed: " + result.stderr)
            else:
                if result.returncode != 1 or not failures:
                    raise RuntimeError(f"mutation {name} did not fail its behavioral assertions")
                print(f"PASS: mutation {name} rejected ({len(failures)} failed assertions)")
    report["temporary_files_removed"] = not directory.exists()
    if args.output:
        (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("check-library-scroll-effects: PASS (host policy and SwiftUI clock; device smoothness unmeasured)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
