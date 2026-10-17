#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute native asset selection and production numeric report parsing offline."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def function(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class SDLIntegrationTests(unittest.TestCase):
    def test_native_bundle_override(self):
        bridge = (ROOT / 'app/Madeira/WineProcessBridge.m').read_text()
        helper = function(bridge, 'static int madeira_set_sdl_component_path(')
        caller = bridge.index('madeira_set_sdl_component_path([[NSBundle mainBundle] bundlePath]);')
        self.assertGreater(caller, bridge.index('const char *gameCfg ='))
        self.assertLess(caller, bridge.index('// Pick which exe to run'))
        harness = r'''
#import <Foundation/Foundation.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <assert.h>
HELPER
int main(int argc, char **argv) {
 @autoreleasepool {
  assert(argc == 2);
  NSFileManager *fm = NSFileManager.defaultManager;
  NSString *root = [NSString stringWithUTF8String:argv[1]];
  NSString *bundle = [root stringByAppendingPathComponent:@"Madeira.app"];
  NSString *asset = [bundle stringByAppendingPathComponent:@"arm64ec-windows/madeira-steam-components/SDL3.dll"];
  assert([fm createDirectoryAtPath:asset.stringByDeletingLastPathComponent withIntermediateDirectories:YES attributes:nil error:NULL]);
  assert([[NSMutableData dataWithLength:128] writeToFile:asset atomically:YES]);
  setenv("MADEIRA_SDL3_COMPONENT_PATH", "C:\\mutable\\SDL3.dll", 1);
  assert(madeira_set_sdl_component_path(bundle) == 1);
  const char *selected = getenv("MADEIRA_SDL3_COMPONENT_PATH");
  assert(selected && !strcmp(selected, asset.stringByResolvingSymlinksInPath.UTF8String));
  assert([fm removeItemAtPath:asset error:NULL]);
  assert(madeira_set_sdl_component_path(bundle) == 0 && !getenv("MADEIRA_SDL3_COMPONENT_PATH"));
  NSString *outside = [root stringByAppendingPathComponent:@"external.dll"];
  assert([[NSMutableData dataWithLength:128] writeToFile:outside atomically:YES]);
  assert([fm createSymbolicLinkAtPath:asset withDestinationPath:outside error:NULL]);
  setenv("MADEIRA_SDL3_COMPONENT_PATH", "stale", 1);
  assert(madeira_set_sdl_component_path(bundle) == 0 && !getenv("MADEIRA_SDL3_COMPONENT_PATH"));
  assert([fm removeItemAtPath:asset error:NULL]);
  assert([fm createDirectoryAtPath:asset withIntermediateDirectories:NO attributes:nil error:NULL]);
  setenv("MADEIRA_SDL3_COMPONENT_PATH", "stale", 1);
  assert(madeira_set_sdl_component_path(bundle) == 0 && !getenv("MADEIRA_SDL3_COMPONENT_PATH"));
  assert([fm removeItemAtPath:asset error:NULL]);
  assert([[NSData data] writeToFile:asset atomically:YES]);
  setenv("MADEIRA_SDL3_COMPONENT_PATH", "stale", 1);
  assert(madeira_set_sdl_component_path(bundle) == 0 && !getenv("MADEIRA_SDL3_COMPONENT_PATH"));
  assert(truncate(asset.fileSystemRepresentation, 16 * 1024 * 1024 + 1) == 0);
  setenv("MADEIRA_SDL3_COMPONENT_PATH", "stale", 1);
  assert(madeira_set_sdl_component_path(bundle) == 0 && !getenv("MADEIRA_SDL3_COMPONENT_PATH"));
  puts("PASS native-owned bundle asset overwrites profile value and clears refused assets");
 }
 return 0;
}
'''.replace('HELPER', helper)
        with tempfile.TemporaryDirectory(prefix='madeira-sdl-bridge-') as td:
            td = Path(td)
            source = td / 'main.m'
            source.write_text(harness)
            binary = td / 'test'
            build = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror',
                                    '-framework', 'Foundation', str(source), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary), str(td)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_numeric_sdl_report(self):
        source = (ROOT / 'app/Madeira/MadeiraDock.swift').read_text()
        parser = source[source.index('    struct Report {'):
                        source.index('    @MainActor private static var lastReport =')]
        harness = r'''
let parsed = MadeiraDock.parseReport(Data("[steam-host] ml1830 sdl-component-stage=8\n[steam-host] ml1830 sdl-component-error=193\n[steam-host] ml1830 sdl-component-path=secret\n".utf8))
assert(parsed.fields["sdl-component-stage"] == "8")
assert(parsed.fields["sdl-component-error"] == "193")
assert(parsed.fields.count == 2)
let nonnumeric = MadeiraDock.parseReport(Data("[steam-host] ml1830 sdl-component-error=secret\n".utf8))
assert(nonnumeric.fields.isEmpty)
print("PASS numeric SDL stages survive live parsing without paths or payloads")
'''
        with tempfile.TemporaryDirectory(prefix='madeira-sdl-report-') as td:
            td = Path(td)
            swift = td / 'main.swift'
            swift.write_text('import Foundation\nenum MadeiraDock {\n' + parser + '\n}\n' + harness)
            binary = td / 'test'
            build = subprocess.run(['xcrun', 'swiftc', '-j', '1', str(swift), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
