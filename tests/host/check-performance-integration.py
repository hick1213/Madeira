#!/usr/bin/env python3
"""Run actual LibraryModel overlay-launch and profile-save method bodies.

Only UI controls, config flags, and the persistence endpoint are doubles. The
production overlay codec and both production method definitions compile and run
unchanged. Each run uses a new named UserDefaults suite, never standard defaults.
Static begin/schema checks are supplementary boundaries, not device evidence.
"""

from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "app/Madeira/Library.swift"
OVERLAY = ROOT / "app/Madeira/PerformanceOverlay.swift"
OUT = ROOT / "app/Build/Products/IPAInspection/global-metrics-integration-tests"


def code_mask(source: str) -> str:
    """Mask Swift strings/comments while preserving code offsets and braces."""
    result = list(source)
    index = 0
    while index < len(source):
        start = index
        if source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
        elif source.startswith("/*", index):
            index += 2
            depth = 1
            while index < len(source) and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError("unterminated production block comment")
        elif source[index] == '"':
            delimiter = '"""' if source.startswith('"""', index) else '"'
            index += len(delimiter)
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source.startswith(delimiter, index):
                    index += len(delimiter)
                    break
                else:
                    index += 1
            else:
                raise ValueError("unterminated production string")
        else:
            index += 1
            continue
        for position in range(start, min(index, len(source))):
            if source[position] != "\n":
                result[position] = " "
    return "".join(result)


def function(source: str, signature: str) -> str:
    """Extract a real definition, skipping declarations, strings, and comments."""
    masked = code_mask(source)
    for match in re.finditer(re.escape(signature), masked):
        candidate = match.start()
        punctuation = re.search(r"[{};]", masked[match.end():])
        if punctuation is None or punctuation.group() != "{":
            continue
        opening = match.end() + punctuation.start()
        depth = 1
        for closing in range(opening + 1, len(masked)):
            depth += (masked[closing] == "{") - (masked[closing] == "}")
            if depth == 0:
                return source[candidate:closing + 1]
    raise ValueError(f"production definition missing: {signature}")


HARNESS = r'''
import Foundation
import Darwin

struct LibraryEntry: Codable, Equatable {
    var id = UUID()
    var title: String
    var performance: Bool
    var overlayFields: [String]?
    var controls: [String]?
    var controlLayout: String?
    var touchControls = false
    var fpsMode = 1
    var controlOpacity: Double?
    var controlSize: Double?
    var controllerMode: String?
    var controllerBinds: [String: String]?
    var padMouseVertical: Double?
    var display: String? = "fit"
}
enum DisplayMode: String { case fit, fill }
@MainActor final class TouchControlsModel {
    static let shared = TouchControlsModel()
    var controls = ["jump", "attack"]
    var layoutID: String? = "fixture-layout"
    var visible = true
    var sizeScale = 1.5
}
@MainActor enum ControlPresetsModel { static var enabled = true }
@MainActor enum GamepadInput { static var keyboardMouseAvailable = true }
@MainActor enum MadeiraConfig {
    static var sessionTools = true
    static func flag(_ key: String) -> Bool { key == "MADEIRA_SESSION_TOOLS" && sessionTools }
}
@MainActor final class LibraryModel {
    var entries: [LibraryEntry] = []
    var current: UUID?
    var activeEntry: LibraryEntry?
    var performance = false
    var overlayFields: [String] = []
    var fpsMode = 4
    var opacity = 0.55
    var controllerMode: String? = "keys"
    var controllerBinds = ["A": "SPACE"]
    var padMouseVertical = 2.0
    var displayMode = DisplayMode.fill
    var saveCalls = 0
    // Real persistence is an endpoint boundary; record the values handed to it.
    func save(_ entry: LibraryEntry) {
        saveCalls += 1
        if let index = entries.firstIndex(where: { $0.id == entry.id }) { entries[index] = entry }
    }
    __CONFIGURE__
    __SAVE__
}
enum CheckFailure: Error { case failed(String) }
@main struct Main {
    @MainActor static func main() {
        do { try run() }
        catch { print("FAIL \(error)"); exit(1) }
    }
    @MainActor static func run() throws {
        var checks: [String] = []
        func check(_ condition: Bool, _ name: String) throws {
            guard condition else { throw CheckFailure.failed(name) }
            checks.append(name)
        }
        guard CommandLine.arguments.count == 2 else { throw CheckFailure.failed("owned defaults domain argument") }
        let suiteName = CommandLine.arguments[1]
        guard let defaults = UserDefaults(suiteName: suiteName) else { throw CheckFailure.failed("isolated defaults") }
        defer { defaults.removePersistentDomain(forName: suiteName) }
        // Serialize only this owned domain in stable order. No real preferences
        // file or standard-defaults values are read.
        func defaultsBytes() throws -> Data {
            try JSONSerialization.data(withJSONObject: defaults.persistentDomain(forName: suiteName) ?? [:], options: [.sortedKeys])
        }
        let model = LibraryModel()
        let fixtures = [
            LibraryEntry(title: "Historical overlay on", performance: true, overlayFields: ["GPU"]),
            LibraryEntry(title: "Historical overlay off", performance: false, overlayFields: ["CPU", "Battery"]),
            LibraryEntry(title: "Legacy missing metric fields", performance: true, overlayFields: nil)
        ]
        model.entries = fixtures
        let initialBytes = try defaultsBytes()
        for fixture in fixtures {
            model.current = fixture.id; model.activeEntry = fixture
            model.performance = true; model.overlayFields = ["Thermal"]
            model.configurePerformanceOverlay(defaults: defaults)
            try check(!model.performance, "default global off overrides history: \(fixture.title)")
            try check(model.overlayFields == ["FPS", "Frame time", "RAM"], "default fields override history: \(fixture.title)")
        }
        try check(model.entries == fixtures, "configuring launch preserves all saved game profiles")
        try check(try defaultsBytes() == initialBytes, "configuring defaults does not write preferences")

        defaults.set(true, forKey: GlobalPerformanceOverlay.enabledKey)
        defaults.set("RAM|CPU|RAM", forKey: GlobalPerformanceOverlay.fieldsKey)
        defaults.set(["keep", "untouched"], forKey: "unrelated-fixture")
        let globalBytes = try defaultsBytes()
        for fixture in fixtures {
            model.current = fixture.id; model.activeEntry = fixture
            model.performance = false; model.overlayFields = ["GPU"]
            model.configurePerformanceOverlay(defaults: defaults)
            try check(model.performance, "global on applies to every game: \(fixture.title)")
            try check(model.overlayFields == ["CPU", "RAM"], "same global fields apply to every game: \(fixture.title)")
        }
        try check(model.entries == fixtures, "global-on configuration preserves historical entry values")
        try check(try defaultsBytes() == globalBytes, "global-on configuration leaves preference bytes unchanged")

        model.current = fixtures[0].id; model.activeEntry = fixtures[0]
        model.performance = false; model.overlayFields = ["Battery"]
        model.saveCurrentProfile()
        let saved = model.entries[0]
        try check(model.saveCalls == 1, "profile save reaches the persistence endpoint once")
        try check(saved.performance, "session switch does not overwrite game's historical overlay state")
        try check(saved.overlayFields == ["GPU"], "session fields do not overwrite game's historical metrics")
        try check(try defaultsBytes() == globalBytes, "session save leaves all global preference bytes unchanged")
        try check(saved.controls == ["jump", "attack"], "touch control edits still persist")
        try check(saved.controlLayout == "fixture-layout", "touch layout still persists")
        try check(saved.touchControls, "touch visibility still persists")
        try check(saved.fpsMode == 4, "FPS limit still persists separately from overlay visibility")
        try check(saved.controlOpacity == 0.55, "touch opacity still persists")
        try check(saved.controlSize == 1.5, "touch size still persists")
        try check(saved.display == "fill", "aspect and scaling still persist")
        try check(saved.controllerMode == "keys", "controller mode still persists")
        try check(saved.controllerBinds == ["A": "SPACE"], "controller binds still persist")
        try check(saved.padMouseVertical == 2, "controller mouse speed still persists")
        try check(Array(model.entries.dropFirst()) == Array(fixtures.dropFirst()), "saving one game leaves other profiles unchanged")

        model.current = fixtures[1].id; model.activeEntry = fixtures[1]
        model.performance = true; model.overlayFields = ["Thermal"]
        ControlPresetsModel.enabled = false
        GamepadInput.keyboardMouseAvailable = false
        MadeiraConfig.sessionTools = false
        model.saveCurrentProfile()
        let restricted = model.entries[1]
        try check(model.saveCalls == 2, "other game's profile still saves")
        try check(!restricted.performance, "temporary on preserves historical off")
        try check(restricted.overlayFields == ["CPU", "Battery"], "temporary fields preserve other game's historical fields")
        try check(restricted.controlLayout == nil, "disabled preset feature preserves stored layout")
        try check(restricted.controllerMode == nil && restricted.controllerBinds == nil && restricted.padMouseVertical == nil,
                  "unavailable keyboard-mouse feature preserves stored controller fields")
        try check(restricted.display == "fit", "hidden session display picker preserves stored scaling")
        try check(restricted.touchControls && restricted.fpsMode == 4, "unrelated active profile values still save under feature restrictions")
        try check(try defaultsBytes() == globalBytes, "second profile save also preserves global preference bytes")

        model.current = nil
        model.saveCurrentProfile()
        try check(model.saveCalls == 2, "no active game does not write any profile")
        model.current = UUID()
        model.saveCurrentProfile()
        try check(model.saveCalls == 2, "missing active entry does not write a profile")
        defaults.set(false, forKey: GlobalPerformanceOverlay.enabledKey)
        let offBytes = try defaultsBytes()
        model.configurePerformanceOverlay(defaults: defaults)
        try check(!model.performance && model.overlayFields == ["CPU", "RAM"], "next launch rereads updated global switch without losing global fields")
        try check(try defaultsBytes() == offBytes, "rereading next-launch selection does not write preferences")
        try check(model.entries[0] == saved && model.entries[1] == restricted && model.entries[2] == fixtures[2],
                  "subsequent launch configuration leaves every saved profile unchanged")
        let data = try JSONSerialization.data(withJSONObject: ["checks": checks, "count": checks.count], options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
'''


DEFAULTS_CLEANUP = r'''
import Foundation
import Darwin
@main struct Cleanup {
    static func main() {
        let prefix = "Madeira.PerformanceIntegration.Tests."
        guard CommandLine.arguments.count == 2 else { exit(2) }
        let domain = CommandLine.arguments[1]
        guard domain.hasPrefix(prefix), UUID(uuidString: String(domain.dropFirst(prefix.count))) != nil,
              let defaults = UserDefaults(suiteName: domain) else { exit(2) }
        let before = defaults.persistentDomain(forName: domain) ?? [:]
        defaults.removePersistentDomain(forName: domain)
        defaults.synchronize()
        let after = defaults.persistentDomain(forName: domain) ?? [:]
        do {
            let data = try JSONSerialization.data(withJSONObject: [
                "domain": domain, "beforeKeys": before.keys.sorted(),
                "afterKeys": after.keys.sorted(), "afterEmpty": after.isEmpty
            ], options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch { exit(3) }
        exit(after.isEmpty ? 0 : 4)
    }
}
'''


def run_native(configure: str, save: str, label: str, optimized: bool, runtime_timeout: float = 20) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    domain = "Madeira.PerformanceIntegration.Tests." + str(uuid.uuid4())
    (OUT / f"{label}-fixture-scope.json").write_text(json.dumps({"domain": domain}, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="native-", dir=OUT) as temporary:
        directory = Path(temporary)
        main = directory / "main.swift"
        binary = directory / "check"
        cleanup_source = directory / "cleanup.swift"
        cleanup_binary = directory / "cleanup"
        cleanup_source.write_text(DEFAULTS_CLEANUP)
        cleanup_compile = subprocess.run(
            ["xcrun", "swiftc", "-swift-version", "6", "-parse-as-library", str(cleanup_source), "-o", str(cleanup_binary)],
            capture_output=True, text=True, timeout=90,
        )
        (OUT / f"{label}-cleanup-compile.log").write_text(cleanup_compile.stdout + cleanup_compile.stderr)
        if cleanup_compile.returncode:
            raise RuntimeError(f"{label} cleanup helper did not compile; test domain was not opened")
        main.write_text(HARNESS.replace("__CONFIGURE__", configure).replace("__SAVE__", save))
        command = ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
                   "-warnings-as-errors", "-parse-as-library"]
        if optimized:
            command.append("-O")
        command.extend([str(OVERLAY), str(main), "-o", str(binary)])
        outcome = {}
        stage = "production-compile"
        try:
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=90)
            (OUT / f"{label}-compile.log").write_text(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                raise RuntimeError(f"{label} failed to compile; inspect retained log")
            stage = "production-runtime"
            result = subprocess.run([str(binary), domain], capture_output=True, text=True, timeout=runtime_timeout)
            (OUT / f"{label}-runtime.log").write_text(result.stdout + result.stderr)
            outcome.update({"label": label, "optimized": optimized, "returncode": result.returncode,
                            "configure_sha256": hashlib.sha256(configure.encode()).hexdigest(),
                            "save_sha256": hashlib.sha256(save.encode()).hexdigest(),
                            "report": json.loads(result.stdout) if result.returncode == 0 else None})
            return outcome
        finally:
            error_type = sys.exc_info()[0]
            cleanup = subprocess.run([str(cleanup_binary), domain], capture_output=True, text=True, timeout=20)
            (OUT / f"{label}-cleanup.log").write_text(cleanup.stdout + cleanup.stderr)
            evidence = {"domain": domain, "stage": stage, "returncode": cleanup.returncode,
                        "execution_error": error_type.__name__ if error_type else None,
                        "native": json.loads(cleanup.stdout) if cleanup.returncode == 0 else None}
            (OUT / f"{label}-cleanup.json").write_text(json.dumps(evidence, indent=2) + "\n")
            if cleanup.returncode or not evidence["native"]["afterEmpty"]:
                raise RuntimeError(f"{label} did not clean its exact owned defaults domain")
            outcome["cleanup"] = evidence


def supplementary_boundaries(source: str) -> dict:
    fixture = '// func sample() { not_a_definition }\nfunc sample();\nfunc sample() { /* } */ return "{" }'
    extracted = function(fixture, "func sample()")
    if extracted != 'func sample() { /* } */ return "{" }':
        raise RuntimeError("extractor failed to ignore a declaration/comment/literal")
    begin = code_mask(function(source, "func begin(_ entry: LibraryEntry"))
    if len(re.findall(r"\bconfigurePerformanceOverlay\(\s*\)", begin)) != 1:
        raise RuntimeError("real begin must call the verified global configuration once")
    entry = code_mask(function(source, "struct LibraryEntry"))
    if not re.search(r"\bvar performance\s*=\s*false\b", entry):
        raise RuntimeError("historical per-entry overlay boolean schema changed")
    if not re.search(r"\bvar overlayFields\s*:\s*\[String\]\?", entry):
        raise RuntimeError("historical optional overlay field schema changed")
    return {"extraction_fixture": "passed", "begin_calls_configuration_once": True,
            "historical_schema_retained": True, "device_launch_tested": False,
            "description": "Supplementary static boundaries only; native methods supply behavior evidence."}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    source = LIBRARY.read_text()
    configure = function(source, "func configurePerformanceOverlay(defaults:")
    save = function(source, "func saveCurrentProfile()")
    boundaries = supplementary_boundaries(source)
    old_overwrite = "entry.performance = performance; entry.overlayFields = overlayFields\n            entry.controlOpacity = opacity"
    target = "entry.controlOpacity = opacity"
    if save.count(target) != 1:
        raise RuntimeError("old-profile-overwrite mutant target is not unique")
    old_configure = configure.replace("performance = selection.enabled", "performance = activeEntry?.performance ?? selection.enabled")
    old_configure = old_configure.replace("overlayFields = selection.fields", "overlayFields = activeEntry?.overlayFields ?? selection.fields")
    if old_configure == configure:
        raise RuntimeError("per-game-launch mutant did not change actual production method")
    prefix = "python-optimized-" if sys.flags.optimize else ""
    # Observe the old overwrite fail at runtime before the production GREEN runs.
    mutants = [
        run_native(configure, save.replace(target, old_overwrite, 1), prefix + "red-old-profile-overwrite", True),
        run_native(old_configure, save, prefix + "red-per-game-launch", True),
    ]
    expected_failures = ["session switch does not overwrite game", "default global off overrides history"]
    for mutant, expected_failure in zip(mutants, expected_failures, strict=True):
        runtime = (OUT / f"{mutant['label']}-runtime.log").read_text()
        if mutant["returncode"] != 1 or expected_failure not in runtime:
            raise RuntimeError(f"compiled mutant did not reach its intended runtime assertion: {mutant['label']}")
    results = [run_native(configure, save, prefix + "debug", False),
               run_native(configure, save, prefix + "optimized", True)]
    if any(result["returncode"] for result in results):
        raise RuntimeError("production integration behavior failed; inspect retained log")
    report = {"production": results, "detected_runtime_mutants": mutants,
              "supplementary_boundaries": boundaries,
              "library_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "overlay_sha256": hashlib.sha256(OVERLAY.read_bytes()).hexdigest(),
              "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    destination = OUT / ("result-python-optimized.json" if prefix else "result.json")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {results[0]['report']['count']} actual-method assertions Debug/-O; 2 compiled runtime mutations rejected; static boundaries reported separately")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
