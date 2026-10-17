#!/usr/bin/env python3
"""Exercise the production runtime detector against isolated synthetic prefixes."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

HARNESS = r'''
import Foundation

@main struct Main {
 @MainActor static func main() async throws {
  func check(_ value: Bool, _ message: String) throws {
   if !value { throw NSError(domain: message, code: 1) }
  }
  let fm = FileManager.default
  let root = fm.temporaryDirectory.appendingPathComponent("runtime-preflight-" + UUID().uuidString)
  defer { try? fm.removeItem(at: root) }
  let game = root.appendingPathComponent("game"), drive = root.appendingPathComponent("prefix/drive_c")
  let prefix = root.appendingPathComponent("prefix"), exe = game.appendingPathComponent("Game.exe")
  try fm.createDirectory(at: game, withIntermediateDirectories: true)
  try fm.createDirectory(at: drive, withIntermediateDirectories: true)
  func pe(_ machine: UInt16 = 0x8664, clr: Bool = false, flags: UInt32 = 1) -> Data {
   var b = [UInt8](repeating: 0, count: 1024)
   func put(_ at: Int, _ value: UInt32, _ count: Int = 4) { for i in 0..<count { b[at+i] = UInt8((value >> (i*8)) & 255) } }
   b[0] = 0x4d; b[1] = 0x5a; put(0x3c, 0x80); b[0x80] = 0x50; b[0x81] = 0x45
   put(0x84, UInt32(machine), 2); put(0x86, 1, 2); put(0x94, machine == 0x14c ? 224 : 240, 2)
   let optional = 0x98, pe32 = machine == 0x14c
   put(optional, pe32 ? 0x10b : 0x20b, 2); put(optional + (pe32 ? 92 : 108), 16)
   let section = optional + (pe32 ? 224 : 240)
   put(section+8, 512); put(section+12, 0x2000); put(section+16, 512); put(section+20, 512)
   if clr { put(optional + (pe32 ? 96 : 112) + 14*8, 0x2000); put(optional + (pe32 ? 96 : 112) + 14*8+4, 72); put(512, 72); put(528, flags) }
   return Data(b)
  }
  func write(_ url: URL, _ data: Data) throws { try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true); try data.write(to: url) }
  func text(_ url: URL, _ value: String) throws { try write(url, Data(value.utf8)) }
  let config = game.appendingPathComponent("Game.runtimeconfig.json"), xml = game.appendingPathComponent("Game.exe.config")
  func declare(_ name: String = "Microsoft.WindowsDesktop.App", _ version: String = "6.0.0", _ extra: String = "") throws {
   try text(config, "{\"runtimeOptions\":{\"framework\":{\"name\":\"\(name)\",\"version\":\"\(version)\"}\(extra)}}")
  }
  func scan() throws -> RuntimePreflight.Report { try RuntimePreflight.scan(executable: exe, gameRoot: game, drive: drive, prefix: prefix) }
  func install(_ family: String, _ version: String, _ arch: UInt16 = 0x8664, _ dotnet: String = "Program Files/dotnet") throws {
   let base = drive.appendingPathComponent(dotnet)
   try write(base.appendingPathComponent("dotnet.exe"), pe(arch))
   try write(base.appendingPathComponent("host/fxr/\(version)/hostfxr.dll"), pe(arch))
   let shared = base.appendingPathComponent("shared/\(family)/\(version)")
   try write(shared.appendingPathComponent(family == "Microsoft.NETCore.App" ? "coreclr.dll" : family == "Microsoft.WindowsDesktop.App" ? "PresentationFramework.dll" : "Microsoft.AspNetCore.dll"), pe(arch))
   try text(shared.appendingPathComponent("\(family).deps.json"), "{}")
   if family == "Microsoft.NETCore.App" {
    try write(shared.appendingPathComponent("hostpolicy.dll"), pe(arch))
    try write(shared.appendingPathComponent("System.Private.CoreLib.dll"), pe(0x14c, clr: true))
   }
  }
  try check(RuntimePreflight.Version("6.0.1-preview") == nil, "prerelease unsupported version")
  try check(RuntimePreflight.Version("6.0.1.2") == nil, "extra version component")
  try write(exe, pe())
  try check(try scan().findings.isEmpty, "native without declaration should have no declared dependency")
  try declare()
  var report = try scan()
  try check(report.missingRequirements.count == 1 && report.requiresAttention, "declared Desktop must be missing")
  try declare("Microsoft.NETCore.App", "6.0.0")
  let coreRoot = drive.appendingPathComponent("Program Files/dotnet")
  let coreVersion = coreRoot.appendingPathComponent("shared/Microsoft.NETCore.App/6.0.33")
  try fm.createDirectory(at: coreVersion, withIntermediateDirectories: true)
  try check(try scan().missingRequirements.count == 1, "bare runtime version folder is incomplete")
  try install("Microsoft.NETCore.App", "6.0.33")
  try check(try scan().findings.contains { $0.state == .satisfied }, "complete Core runtime with AnyCPU CoreLib")
  let essential = [
   coreRoot.appendingPathComponent("dotnet.exe"),
   coreRoot.appendingPathComponent("host/fxr/6.0.33/hostfxr.dll"),
   coreVersion.appendingPathComponent("Microsoft.NETCore.App.deps.json"),
   coreVersion.appendingPathComponent("coreclr.dll"),
   coreVersion.appendingPathComponent("hostpolicy.dll"),
   coreVersion.appendingPathComponent("System.Private.CoreLib.dll")
  ]
  for file in essential {
   let original = try Data(contentsOf: file)
   try fm.removeItem(at: file)
   try check(try scan().missingRequirements.count == 1, "partial Core runtime missing " + file.lastPathComponent)
   try write(file, original)
  }
  for file in [essential[0], essential[1], essential[3], essential[4]] {
   let original = try Data(contentsOf: file)
   try write(file, pe(0x14c))
   try check(try scan().missingRequirements.count == 1, "native runtime bitness mismatch " + file.lastPathComponent)
   try write(file, original)
  }
  let corelib = essential[5], originalCorelib = try Data(contentsOf: essential[5])
  try write(corelib, pe())
  try check(try scan().missingRequirements.count == 1, "CoreLib must be a managed PE assembly")
  try write(corelib, originalCorelib)
  try declare()
  try check(try scan().missingRequirements.count == 1, "Core alone cannot satisfy Desktop")
  try install("Microsoft.WindowsDesktop.App", "6.0.33")
  // Managed Desktop assemblies are commonly AnyCPU even for an x64 runtime.
  try write(drive.appendingPathComponent("Program Files/dotnet/shared/Microsoft.WindowsDesktop.App/6.0.33/PresentationFramework.dll"), pe(0x14c, clr: true))
  try check(try scan().findings.contains { $0.state == .satisfied }, "real Desktop files and Core patch satisfy")
  let desktopFolder = drive.appendingPathComponent("Program Files/dotnet/shared/Microsoft.WindowsDesktop.App")
  let desktopBackup = root.appendingPathComponent("desktop-backup")
  try fm.moveItem(at: desktopFolder, to: desktopBackup)
  try install("Microsoft.WindowsDesktop.App", "6.0.33", 0x8664, "Program Files/dotnet/x64")
  try check(try scan().missingRequirements.count == 1, "Desktop and Core in different installation roots cannot combine")
  try fm.moveItem(at: desktopBackup, to: desktopFolder)
  try fm.removeItem(at: drive.appendingPathComponent("Program Files/dotnet/x64"))
  try declare("Microsoft.AspNetCore.App", "6.0.0")
  try check(try scan().missingRequirements.count == 1, "Core and Desktop cannot satisfy ASP.NET")
  try install("Microsoft.AspNetCore.App", "6.0.33", 0x8664, "Program Files/dotnet/x64")
  try check(try scan().missingRequirements.count == 1, "ASP.NET and Core in different installation roots cannot combine")
  try fm.removeItem(at: drive.appendingPathComponent("Program Files/dotnet/x64"))
  try install("Microsoft.AspNetCore.App", "6.0.33")
  try check(try scan().findings.contains { $0.state == .satisfied }, "ASP.NET with native Core runtime")
  try declare("Microsoft.WindowsDesktop.App", "6.0.34")
  try check(try scan().missingRequirements.count == 1, "older patch cannot satisfy")
  try declare("Microsoft.NETCore.App", "5.0.0", ",\"rollForward\":\"Major\"")
  try check(try scan().findings.contains { $0.state == .satisfied }, "Major roll forward")
  try declare("Microsoft.NETCore.App", "5.0.0")
  try check(try scan().missingRequirements.count == 1, "default Minor cannot change major")
  try declare("Microsoft.NETCore.App", "6.0.0", ",\"rollForward\":\"Disable\"")
  try check(try scan().missingRequirements.count == 1, "Disable requires exact")
  try declare("Microsoft.NETCore.App", "6.0.0", ",\"applyPatches\":false")
  try check(try scan().missingRequirements.count == 1, "applyPatches false cannot use later patch in same minor")
  try declare("Microsoft.NETCore.App", "6.0.0", ",\"rollForward\":\"latestpatch\"")
  try check(try scan().findings.contains { $0.state == .satisfied }, "rollForward is case insensitive")
  try declare("Microsoft.NETCore.App", "6.0.0", ",\"rollForward\":\"Minor\",\"applyPatches\":false")
  try check(try scan().findings.contains { $0.state == .unknown }, "mixed modern and legacy policy invalid")
  try text(config, "{\"runtimeOptions\":{\"frameworks\":[{\"name\":\"Microsoft.NETCore.App\",\"version\":\"6.0.0\",\"rollForward\":\"Minor\"},{\"name\":\"Microsoft.WindowsDesktop.App\",\"version\":\"6.0.0\",\"applyPatches\":false}]}}")
  try check(try scan().findings.contains { $0.state == .unknown }, "cross-framework policy mix invalid")
  try declare("Microsoft.NETCore.App", "6.0.0", ",\"rollForward\":\"UnknownPolicy\"")
  try check(try scan().findings.contains { $0.state == .unknown }, "unsupported policy must be visible")
  try text(config, "{\"runtimeOptions\":{\"includedFrameworks\":[{\"name\":\"Microsoft.NETCore.App\",\"version\":\"6.0.0\"}]}}")
  try check(try scan().missingRequirements.isEmpty, "self contained declares no global dependency")
  try text(config, "{\"runtimeOptions\":{\"frameworks\":[{\"name\":\"Microsoft.NETCore.App\",\"version\":\"6.0.0\"},{\"name\":\"Microsoft.WindowsDesktop.App\",\"version\":\"6.0.0\"}]}}")
  try check(try scan().findings.filter { $0.state == .satisfied }.count == 2, "multiple frameworks")
  try declare("Microsoft.NETCore.App", "6.0.0")
  let nativeCore = drive.appendingPathComponent("Program Files/dotnet/shared/Microsoft.NETCore.App/6.0.33/coreclr.dll")
  try fm.removeItem(at: nativeCore)
  try write(nativeCore, pe(0x14c))
  try check(try scan().missingRequirements.count == 1, "runtime bitness must match even in x64 root")
  try write(nativeCore, pe())
  try write(exe, pe(0x14c, clr: true))
  try check(try scan().findings.contains { $0.state == .unknown }, "AnyCPU architecture must be uncertain")
  try write(exe, pe(0x14c))
  try declare("Microsoft.NETCore.App")
  try check(try scan().missingRequirements.count == 1, "x64 runtime cannot satisfy x86")
  try install("Microsoft.NETCore.App", "6.0.33", 0x14c, "Program Files (x86)/dotnet")
  try check(try scan().findings.contains { $0.state == .satisfied }, "x86 runtime root")
  try write(exe, pe())
  try fm.removeItem(at: config)
  try text(xml, "<configuration><startup><supportedRuntime version=\"v4.0\" sku=\".NETFramework,Version=v4.8\"/></startup></configuration>")
  try check(try scan().missingRequirements.count == 1, "explicit framework sku missing")
  try text(prefix.appendingPathComponent("system.reg"), "[Software\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v4\\\\Full] 1\n\"Release\"=dword:00080eb1\n")
  try write(drive.appendingPathComponent("windows/Microsoft.NET/Framework64/v4.0.30319/clr.dll"), pe())
  try check(try scan().findings.contains { $0.state == .satisfied }, "Framework release and native CLR")
  try text(xml, "<configuration><startup><supportedRuntime version=\"v4.0\" sku=\".NETFramework,Version=v4.0\"/></startup></configuration>")
  try text(prefix.appendingPathComponent("system.reg"), "[Software\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v4\\\\Full] 1\n\"Install\"=dword:00000001\n\"Version\"=\"4.0.30319.1\"\n")
  try check(try scan().findings.contains { $0.state == .unknown || $0.state == .unsupported }, "Framework 4.0 legacy metadata cannot become false missing")
  try text(xml, "<configuration><startup><supportedRuntime version=\"v4.0\"/></startup></configuration>")
  try check(try scan().findings.contains { $0.state == .unknown }, "v4.0 must not imply v4.8")
  try fm.removeItem(at: xml)
  try text(config, "")
  try check(try scan().findings.contains { $0.state == .unknown }, "empty metadata must be visible")
  try text(config, String(repeating: " ", count: 131073))
  try check(try scan().findings.contains { $0.state == .unknown }, "metadata bounded")
  try fm.removeItem(at: config)
  try text(config, "{\"runtimeOptions\":{\"framework\":{\"name\":\"Unknown.Runtime\",\"version\":\"6.0.0\"}}}")
  try check(try scan().findings.contains { $0.state == .unknown }, "unknown framework cannot appear clear")
  try text(config, "{\"runtimeOptions\":{\"framework\":{\"name\":\"Microsoft.NETCore.App\",\"version\":\"6.0.0\"},\"applyPatches\":1}}")
  try check(try scan().findings.contains { $0.state == .unknown }, "nonboolean applyPatches rejected")
  try fm.removeItem(at: config)
  let outside = root.appendingPathComponent("outside.json")
  try text(outside, "{\"runtimeOptions\":{\"framework\":{\"name\":\"Microsoft.NETCore.App\",\"version\":\"6.0.0\"}}}")
  try fm.createSymbolicLink(at: config, withDestinationURL: outside)
  try check(try scan().findings.contains { $0.state == .unknown }, "escaping companion symlink rejected")
  try fm.removeItem(at: config)
  let task = Task { @MainActor () throws -> RuntimePreflight.Report in
   withUnsafeCurrentTask { $0?.cancel() }
   return try scan()
  }
  do { _ = try await task.value; throw NSError(domain: "cancellation ignored", code: 1) } catch is CancellationError {}
  print("runtime preflight fixtures passed")
 }
}
'''

class RuntimePreflightTests(unittest.TestCase):
    def test_production_detector(self):
        with tempfile.TemporaryDirectory(prefix='madeira-runtime-test-') as directory:
            directory = Path(directory)
            harness = directory / 'main.swift'
            harness.write_text(HARNESS)
            binary = directory / 'runtime-preflight'
            subprocess.run(['swiftc', '-j1', '-swift-version', '6', '-parse-as-library',
                            str(ROOT / 'app/Madeira/RuntimePreflight.swift'), str(harness), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

if __name__ == '__main__':
    unittest.main()
