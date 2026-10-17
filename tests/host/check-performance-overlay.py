#!/usr/bin/env python3
"""Exercise the production global-overlay codec and monotonic frame sampler."""

from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/Madeira/PerformanceOverlay.swift"
ARTIFACTS = ROOT / "app/Build/Products/IPAInspection/global-metrics-tests"

# Expectations are hand-calculated. The production codec never supplies a want
# value, and no test reads or writes the user's standard defaults.
MAIN = r'''
import Foundation
import Darwin

enum CheckFailure: Error { case failed(String) }

@main struct Main {
    static func main() {
        do { try run() }
        catch { print("FAIL \(error)"); exit(1) }
    }

    static func run() throws {
        var checks: [String] = []
        func check(_ condition: Bool, _ name: String) throws {
            guard condition else { throw CheckFailure.failed(name) }
            checks.append(name)
        }
        func near(_ actual: Double, _ expected: Double, _ name: String) throws {
            try check(actual.isFinite && abs(actual - expected) < 0.000_001, name)
        }
        guard CommandLine.arguments.count == 2 else { throw CheckFailure.failed("owned defaults domain argument") }
        let suiteName = CommandLine.arguments[1]
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            throw CheckFailure.failed("create isolated defaults")
        }
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let initial = GlobalPerformanceOverlay.read(defaults: defaults)
        try check(!initial.enabled, "new installation leaves the overlay off")
        try check(initial.fields == ["FPS", "Frame time", "RAM"], "new installation supplies usable default metrics")
        defaults.set(true, forKey: GlobalPerformanceOverlay.enabledKey)
        defaults.set("GPU|FPS|GPU|Thermal", forKey: GlobalPerformanceOverlay.fieldsKey)
        let enabled = GlobalPerformanceOverlay.read(defaults: defaults)
        try check(enabled.enabled, "global switch persists for later launches")
        try check(enabled.fields == ["FPS", "GPU", "Thermal"], "persisted metrics are canonical and unique")
        defaults.set(false, forKey: GlobalPerformanceOverlay.enabledKey)
        try check(!GlobalPerformanceOverlay.read(defaults: defaults).enabled, "turning global switch off persists")
        defaults.set(["GPU"], forKey: GlobalPerformanceOverlay.fieldsKey)
        try check(GlobalPerformanceOverlay.read(defaults: defaults).fields == ["FPS", "Frame time", "RAM"], "wrong defaults storage type falls back safely")
        defaults.set("CPU", forKey: GlobalPerformanceOverlay.fieldsKey)
        try check(GlobalPerformanceOverlay.read(defaults: defaults).fields == ["CPU"], "single persisted metric remains selected")

        let fixtures: [(String, [String])] = [
            ("", ["FPS", "Frame time", "RAM"]),
            ("FPS|RAM", ["FPS", "RAM"]),
            ("RAM|Frame time|FPS|RAM", ["FPS", "Frame time", "RAM"]),
            ("Other|GPU|CPU", ["CPU", "GPU"]),
            ("Other", ["FPS", "Frame time", "RAM"]),
            ("FPS||GPU", ["FPS", "Frame time", "RAM"]),
            ("|GPU", ["FPS", "Frame time", "RAM"]),
            ("GPU|", ["FPS", "Frame time", "RAM"]),
            ("FPS\n|GPU", ["FPS", "Frame time", "RAM"]),
            ("FPS\0|GPU", ["FPS", "Frame time", "RAM"]),
            (String(repeating: "x", count: 257), ["FPS", "Frame time", "RAM"]),
            (String(repeating: "Frame time|", count: 31) + "GPU", ["FPS", "Frame time", "RAM"]),
            (String(repeating: "FPS|", count: 65) + "GPU", ["FPS", "Frame time", "RAM"]),
            ("Thermal|Battery|RAM|GPU|CPU|Frame time|FPS", ["FPS", "Frame time", "CPU", "GPU", "RAM", "Battery", "Thermal"])
        ]
        for (index, fixture) in fixtures.enumerated() {
            try check(GlobalPerformanceOverlay.decode(fixture.0) == fixture.1, "decode fixture \(index)")
        }
        try check(GlobalPerformanceOverlay.normalize(["GPU", "Unknown", "FPS", "GPU"]) == ["FPS", "GPU"], "array normalization retains supported fields once")
        try check(GlobalPerformanceOverlay.normalize([]) == ["FPS", "Frame time", "RAM"], "empty array stays displayable")
        try check(GlobalPerformanceOverlay.normalize(Array(repeating: "GPU", count: 65)) == ["FPS", "Frame time", "RAM"], "oversized array remains bounded")
        try check(GlobalPerformanceOverlay.normalize(["FPS", "bad\0name"]) == ["FPS", "Frame time", "RAM"], "control characters make array malformed")
        try check(GlobalPerformanceOverlay.normalize(["FPS", String(repeating: "x", count: 65)]) == ["FPS", "Frame time", "RAM"], "oversized field makes array malformed")
        try check(GlobalPerformanceOverlay.encode(["RAM", "FPS", "RAM"]) == "FPS|RAM", "encoding gives stable AppStorage string")
        try check(GlobalPerformanceOverlay.encode([]) == "FPS|Frame time|RAM", "encoding empty fields retains default metrics")
        try check(GlobalPerformanceOverlay.settingField("GPU", enabled: true, in: ["RAM", "FPS"]) == ["FPS", "GPU", "RAM"], "enabling a metric uses canonical order")
        try check(GlobalPerformanceOverlay.settingField("FPS", enabled: false, in: ["FPS", "GPU"]) == ["GPU"], "disabling one of two metrics works")
        try check(GlobalPerformanceOverlay.settingField("GPU", enabled: false, in: ["GPU"]) == ["GPU"], "last metric cannot be disabled")
        try check(GlobalPerformanceOverlay.settingField("Unknown", enabled: true, in: ["GPU"]) == ["GPU"], "unknown field cannot enter selection")
        try check(GlobalPerformanceOverlay.settingField("GPU", enabled: true, in: ["GPU"]) == ["GPU"], "repeated enable does not duplicate")

        var sampler = PerformanceFrameSampler()
        try check(sampler.sample(count: 100, time: 10) == nil, "first sample only establishes baseline")
        guard let one = sampler.sample(count: 160, time: 11) else { throw CheckFailure.failed("one second sample produced") }
        try near(one.fps, 60, "one second frame rate")
        try near(one.elapsed, 1, "one second elapsed interval")
        try check(one.frames == 60, "one second submitted frame count")
        guard let two = sampler.sample(count: 250, time: 13) else { throw CheckFailure.failed("two second sample produced") }
        try near(two.fps, 45, "delayed timer divides by real two second interval")
        try near(two.elapsed, 2, "delayed timer keeps actual interval")
        try check(two.frames == 90, "delayed timer counts actual submissions")
        guard let idle = sampler.sample(count: 250, time: 14) else { throw CheckFailure.failed("idle sample produced") }
        try near(idle.fps, 0, "zero new frames reports zero instead of stale FPS")
        try check(idle.frames == 0, "idle frame delta is zero")
        try check(sampler.sample(count: 300, time: 13.5) == nil, "clock reversal does not publish a rate")
        guard let reversed = sampler.sample(count: 310, time: 14.5) else { throw CheckFailure.failed("rate after clock reversal") }
        try near(reversed.fps, 10, "clock reversal rebaselines frame count")
        try check(sampler.sample(count: 320, time: 14.5) == nil, "same timestamp does not divide by zero")
        guard let equalTime = sampler.sample(count: 340, time: 15.5) else { throw CheckFailure.failed("rate after same timestamp") }
        try near(equalTime.fps, 20, "same timestamp rebaselines count")
        try check(sampler.sample(count: 1, time: 16.5) == nil, "counter reset does not wrap UInt64")
        guard let restarted = sampler.sample(count: 31, time: 17.5) else { throw CheckFailure.failed("rate after counter reset") }
        try near(restarted.fps, 30, "counter reset uses new baseline")
        for invalid in [Double.nan, Double.infinity, -Double.infinity, 0, -1] {
            try check(sampler.sample(count: 90, time: invalid) == nil, "invalid timestamp \(invalid) rejected")
            try check(sampler.sample(count: 100, time: 100) == nil, "valid timestamp after invalid establishes baseline")
            guard let recovered = sampler.sample(count: 120, time: 101) else { throw CheckFailure.failed("rate after invalid timestamp") }
            try near(recovered.fps, 20, "rate recovers after invalid timestamp")
        }
        sampler.reset(count: 10_000, time: 500)
        guard let resumed = sampler.sample(count: 10_030, time: 501) else { throw CheckFailure.failed("foreground rate produced") }
        try near(resumed.fps, 30, "foreground reset excludes background elapsed time")
        sampler.reset(count: 1, time: .nan)
        try check(sampler.sample(count: 2, time: 600) == nil, "invalid explicit reset clears baseline")
        try check(sampler.sample(count: UInt64.max, time: 600) == nil, "large counter at unchanged time rebaselines without arithmetic overflow")
        try check(sampler.sample(count: 0, time: 601) == nil, "counter wrapping to zero resets baseline")
        guard let tiny = sampler.sample(count: 1, time: 601.25) else { throw CheckFailure.failed("quarter second sample produced") }
        try near(tiny.fps, 4, "subsecond real interval remains accurate")
        var extreme = PerformanceFrameSampler()
        extreme.reset(count: 0, time: Double.leastNonzeroMagnitude)
        try check(extreme.sample(count: UInt64.max, time: Double.leastNonzeroMagnitude * 2) == nil, "overflowing derived FPS is never published")
        try check(extreme.sample(count: UInt64.max, time: 1) != nil, "finite sampling resumes after derived overflow")

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
        let prefix = "Madeira.PerformanceOverlay.Tests."
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


def run_native(source: Path, label: str, optimized: bool, runtime_timeout: float = 20) -> dict:
    """Compile real source, run its behavior, and retain bounded evidence."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    domain = "Madeira.PerformanceOverlay.Tests." + str(uuid.uuid4())
    (ARTIFACTS / f"{label}-fixture-scope.json").write_text(json.dumps({"domain": domain}, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="compile-", dir=ARTIFACTS) as temporary:
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
        (ARTIFACTS / f"{label}-cleanup-compile.log").write_text(cleanup_compile.stdout + cleanup_compile.stderr)
        if cleanup_compile.returncode:
            raise RuntimeError(f"{label} cleanup helper did not compile; test domain was not opened")
        main.write_text(MAIN)
        compile_command = [
            "xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
            "-warnings-as-errors", "-parse-as-library",
        ]
        if optimized:
            compile_command.append("-O")
        compile_command.extend([str(source), str(main), "-o", str(binary)])
        outcome = {}
        stage = "production-compile"
        try:
            compiled = subprocess.run(compile_command, capture_output=True, text=True, timeout=90)
            (ARTIFACTS / f"{label}-compile.log").write_text(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                raise RuntimeError(f"{label} did not compile; see {label}-compile.log")
            stage = "production-runtime"
            result = subprocess.run([str(binary), domain], capture_output=True, text=True, timeout=runtime_timeout)
            (ARTIFACTS / f"{label}-runtime.log").write_text(result.stdout + result.stderr)
            outcome.update({
                "label": label,
                "optimized": optimized,
                "returncode": result.returncode,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "report": json.loads(result.stdout) if result.returncode == 0 else None,
            })
            return outcome
        finally:
            error_type = sys.exc_info()[0]
            cleanup = subprocess.run([str(cleanup_binary), domain], capture_output=True, text=True, timeout=20)
            (ARTIFACTS / f"{label}-cleanup.log").write_text(cleanup.stdout + cleanup.stderr)
            evidence = {"domain": domain, "stage": stage, "returncode": cleanup.returncode,
                        "execution_error": error_type.__name__ if error_type else None,
                        "native": json.loads(cleanup.stdout) if cleanup.returncode == 0 else None}
            (ARTIFACTS / f"{label}-cleanup.json").write_text(json.dumps(evidence, indent=2) + "\n")
            if cleanup.returncode or not evidence["native"]["afterEmpty"]:
                raise RuntimeError(f"{label} did not clean its exact owned defaults domain")
            outcome["cleanup"] = evidence


def typecheck_ios(label: str) -> dict:
    """Compile the UIKit/SwiftUI production branch against the actual SDK."""
    sdk = subprocess.run(
        ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
        capture_output=True, text=True, timeout=20, check=True,
    ).stdout.strip()
    checked = subprocess.run(
        ["xcrun", "swiftc", "-swift-version", "6", "-strict-concurrency=complete",
         "-warnings-as-errors", "-typecheck", "-target", "arm64-apple-ios17.0",
         "-sdk", sdk, str(SOURCE)],
        capture_output=True, text=True, timeout=90,
    )
    (ARTIFACTS / f"{label}-ios-typecheck.log").write_text(checked.stdout + checked.stderr)
    if checked.returncode:
        raise RuntimeError("iOS SwiftUI branch failed to typecheck; inspect retained log")
    return {"returncode": checked.returncode, "sdk": sdk, "target": "arm64-apple-ios17.0"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-red", action="store_true", help="record a missing-feature runtime failure")
    parser.add_argument("--no-mutations", action="store_true", help="run only the unmodified production source")
    arguments = parser.parse_args()
    if arguments.expect_red:
        result = run_native(SOURCE, "first-red", optimized=False)
        (ARTIFACTS / "first-red-result.json").write_text(json.dumps(result, indent=2) + "\n")
        failed_for_missing_fields = "new installation supplies usable default metrics" in (ARTIFACTS / "first-red-runtime.log").read_text()
        if result["returncode"] == 0 or not failed_for_missing_fields:
            raise RuntimeError("RED must fail at the absent production default-field behavior")
        print("Expected production runtime RED: missing global default metrics")
        return 0

    prefix = "python-optimized-" if sys.flags.optimize else ""
    results = [run_native(SOURCE, prefix + "debug", optimized=False),
               run_native(SOURCE, prefix + "optimized", optimized=True)]
    if any(result["returncode"] for result in results):
        raise RuntimeError("production runtime checks failed; inspect retained logs")
    mutations = [
        ("ignore-global-switch", "enabled: defaults.bool(forKey: enabledKey)", "enabled: false"),
        ("allow-last-field-off", "if !enabled && current.count == 1", "if !enabled && current.count == 0"),
        ("assume-one-second-timer", "let fps = Double(frames) / elapsed", "let fps = Double(frames)"),
        ("ignore-counter-reset", "count >= previous.count", "true"),
        ("accept-oversized-codec", "stored.utf8.count <= maxEncodedBytes", "true"),
    ]
    mutation_results = []
    if not arguments.no_mutations:
        original = SOURCE.read_text()
        with tempfile.TemporaryDirectory(prefix="mutants-", dir=ARTIFACTS) as temporary:
            for name, old, new in mutations:
                if original.count(old) != 1:
                    raise RuntimeError(f"mutation {name} has no unique production target")
                mutant = Path(temporary) / f"{name}.swift"
                mutant.write_text(original.replace(old, new, 1))
                result = run_native(mutant, prefix + name, optimized=True)
                if result["returncode"] == 0:
                    raise RuntimeError(f"runtime suite did not detect mutation {name}")
                mutation_results.append(result)
    report = {"production": results, "detected_mutations": mutation_results,
              "ios_typecheck": typecheck_ios(prefix + "production"),
              "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (ARTIFACTS / ("result-python-optimized.json" if prefix else "result.json")).write_text(
        json.dumps(report, indent=2) + "\n")
    print(f"PASS: {results[0]['report']['count']} production assertions in Swift 6 Debug and -O; {len(mutation_results)} compiled runtime mutations detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
