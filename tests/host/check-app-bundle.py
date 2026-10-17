#!/usr/bin/env python3
"""Synthetic packaging regressions only; fixtures never leave temporary storage."""
import importlib.util
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/check-app-bundle.py'
spec = importlib.util.spec_from_file_location('bundle_check', SCRIPT)
checker = importlib.util.module_from_spec(spec)
# Import intentionally fails before implementation (the red test).
spec.loader.exec_module(checker)


def pe(machine):
    optional_size = 224 if machine == 0x14c else 240
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<HH', data, 68, machine, 1)
    struct.pack_into('<H', data, 84, optional_size)
    struct.pack_into('<H', data, 88, 0x10b if machine == 0x14c else 0x20b)
    struct.pack_into('<II', data, 88 + 56, 8192, 512)
    section = 88 + optional_size
    data[section:section + 8] = b'.text\0\0\0'
    struct.pack_into('<4I', data, section + 8, 512, 4096, 512, 512)
    data[512] = 0xc3
    return data


def macho(platform=2, filetype=6, dependency=None, weak=False,
          rpaths=('@executable_path',)):
    commands = struct.pack('<6I', 0x32, 24, platform, 0, 0, 0)
    count = 1
    if dependency:
        name = dependency.encode() + b'\0'
        size = (24 + len(name) + 7) & ~7
        commands += struct.pack('<6I', 0x80000018 if weak else 0xc, size, 24, 0, 0, 0) + name.ljust(size - 24, b'\0')
        count += 1
        for rpath in rpaths:
            value = rpath.encode() + b'\0'
            size = (12 + len(value) + 7) & ~7
            commands += struct.pack('<3I', 0x8000001c, size, 12) + value.ljust(size - 12, b'\0')
            count += 1
    return struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, filetype,
                       count, len(commands), 0, 0) + commands


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Madeira.app'
        def put(name, data=b'resource'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.put = put
        for name in ['prefix-template.tar.gz', 'cacert.pem', 'madeira-jit.js',
                     'Madeira JIT.shortcut', 'cjk-fonts/NotoSansCJKsc-Regular.otf',
                     'cjk-fonts/LICENSE.txt', 'nls/c_1252.nls', 'nls/c_20127.nls',
                     'nls/c_28591.nls', 'nls/c_437.nls', 'nls/l_intl.nls',
                     'licenses/LICENSE-MADEIRA-GPL-3.0.txt',
                     'licenses/LICENSE-MADEIRA-EXCEPTION.txt',
                     'licenses/THIRD-PARTY-NOTICES.txt', 'legal/THIRD-PARTY-NOTICES.md',
                     'legal/LICENSES/FAudio-zlib.txt',
                     'd3d12/NOTICE.txt', 'd3d12/LICENSE-metal-shader-converter-headers.txt',
                     'd3d12/METAL-SHADER-CONVERTER-AGREEMENT.txt',
                     'd3d12/canary_layout_ps.dxil', 'd3d12/canary_layout_vs.dxil',
                     'd3d12/canary_ps.dxil', 'd3d12/canary_vs.dxil',
                     'Frameworks/StikJIT.framework/universal.js',
                     'Frameworks/StikJIT.framework/legacy.js']:
            put(name)
        put('Info.plist', plistlib.dumps({'CFBundleExecutable': 'Madeira'}))
        put('Madeira', macho(filetype=2))
        put('Frameworks/StikJIT.framework/StikJIT', macho())
        put('Frameworks/StikJIT.framework/Info.plist', plistlib.dumps({'CFBundleExecutable': 'StikJIT'}))
        put('PlugIns/MadeiraJITHelper.appex/Info.plist', plistlib.dumps({'CFBundleExecutable': 'MadeiraJITHelper'}))
        put('PlugIns/MadeiraJITHelper.appex/MadeiraJITHelper', macho(filetype=2))
        put('d3d12/libmetalirconverter.dylib', macho())
        for farm, machine in [('aarch64-windows', 0xaa64), ('arm64ec-windows', 0x8664)]:
            for name in ['ntdll.dll', 'kernel32.dll', 'kernelbase.dll', 'user32.dll',
                         'win32u.dll']:
                put(f'{farm}/{name}', pe(machine))
        put('arm64ec-windows/xtajit64.dll', pe(0x8664))
        put('arm64ec-windows/wintypes.dll', pe(0x8664))
        put('arm64ec-windows/faultrep.dll', pe(0x8664))
        for name in ('services.exe', 'wineboot.exe'):
            put('aarch64-windows/' + name, pe(0xaa64))
        for name in ('xaudio2_7.dll', 'xaudio2_8.dll', 'xaudio2_9.dll'):
            put('arm64ec-windows/' + name, pe(0x8664))
        for name in ['concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
                     'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcamp140.dll',
                     'vccorlib140.dll', 'vcomp140.dll', 'vcruntime140.dll',
                     'vcruntime140_1.dll', 'vcruntime140_threads.dll']:
            put('x86_64-vcruntime/' + name, pe(0x8664))

    def errors(self, **requirements):
        return checker.check_bundle(self.root, **requirements)

    def test_base_bundle_passes_without_optional_features(self):
        self.assertEqual(self.errors(), [])

    def test_cjk_font_and_license_are_required_for_source_and_device(self):
        for path in ('cjk-fonts/NotoSansCJKsc-Regular.otf', 'cjk-fonts/LICENSE.txt'):
            for source in (True, False):
                with self.subTest(path=path, source=source):
                    (self.root / path).unlink()
                    self.assertTrue(any(path in error for error in self.errors(source=source)))
                    self.put(path)

    def test_runtime_repair_helpers_are_required_in_both_package_modes(self):
        for name in ('services.exe', 'wineboot.exe'):
            path = 'aarch64-windows/' + name
            for source in (True, False):
                for data in (None, b'', pe(0x8664), b'not a PE'):
                    with self.subTest(name=name, source=source, data=data is None):
                        if data is None:
                            (self.root / path).unlink()
                        else:
                            self.put(path, data)
                        self.assertTrue(any(path in error for error in self.errors(source=source)))
                        self.put(path, pe(0xaa64))

    def test_missing_winrt_provider_is_reported(self):
        (self.root / 'arm64ec-windows/wintypes.dll').unlink()
        self.assertTrue(any('arm64ec-windows/wintypes.dll' in error for error in self.errors()))

    def test_winrt_provider_wrong_architecture_is_rejected(self):
        self.put('arm64ec-windows/wintypes.dll', pe(0x14c))
        self.assertTrue(any('wintypes.dll: PE machine' in error for error in self.errors()))

    def test_empty_winrt_provider_is_rejected(self):
        self.put('arm64ec-windows/wintypes.dll', b'')
        self.assertTrue(any('wintypes.dll: missing or empty' in error for error in self.errors()))

    def test_missing_xaudio_runtime_is_reported(self):
        (self.root / 'arm64ec-windows/xaudio2_9.dll').unlink()
        self.assertTrue(any('xaudio2_9.dll' in error for error in self.errors()))

    def test_xaudio_wrong_architecture_is_rejected(self):
        self.put('arm64ec-windows/xaudio2_8.dll', pe(0x14c))
        self.assertTrue(any('xaudio2_8.dll' in error for error in self.errors()))

    def test_empty_xaudio_runtime_is_rejected(self):
        self.put('arm64ec-windows/xaudio2_7.dll', b'')
        self.assertTrue(any('xaudio2_7.dll' in error for error in self.errors()))

    def test_missing_shared_resource_fails(self):
        (self.root / 'prefix-template.tar.gz').unlink()
        self.assertTrue(any('prefix-template' in error for error in self.errors()))

    def test_missing_jit_framework_fails(self):
        (self.root / 'Frameworks/StikJIT.framework/StikJIT').unlink()
        self.assertTrue(any('StikJIT' in error for error in self.errors()))

    def test_missing_dock_or_notices_fails(self):
        self.assertTrue(any('dockhost' in error for error in self.errors(require_steam=True)))
        self.put('arm64ec-windows/dockhost.exe', pe(0x8664))
        self.assertTrue(any('dock-notices' in error for error in self.errors(require_steam=True)))
        self.put('arm64ec-windows/dock-notices.txt')
        self.assertEqual(self.errors(require_steam=True), [])

    def test_placeholder_i386_directory_does_not_establish_wow64(self):
        self.put('i386-windows/.gitkeep')
        self.assertTrue(any('i386-windows/ntdll.dll' in error for error in self.errors(require_wow64=True)))

    def test_wow64_core_and_native_bridge_must_have_correct_machines(self):
        for name in ['ntdll.dll', 'kernel32.dll', 'kernelbase.dll', 'user32.dll',
                     'win32u.dll', 'winegstreamer.dll', 'wmadmod.dll']:
            self.put('i386-windows/' + name, pe(0x14c))
        for name in ['wow64.dll', 'wow64win.dll', 'xtajit.dll']:
            self.put('aarch64-windows/' + name, pe(0xaa64))
        self.assertEqual(self.errors(require_wow64=True), [])
        self.put('i386-windows/ntdll.dll', pe(0x8664))
        self.assertTrue(any('machine' in error for error in self.errors(require_wow64=True)))

    def test_wrong_dock_machine_fails(self):
        self.put('arm64ec-windows/dockhost.exe', pe(0xaa64))
        self.put('arm64ec-windows/dock-notices.txt')
        self.assertTrue(any('machine' in error for error in self.errors(require_steam=True)))

    def test_missing_vcruntime_license_or_codec_resource_fails(self):
        for name in ['x86_64-vcruntime/msvcp140_2.dll',
                     'licenses/THIRD-PARTY-NOTICES.txt', 'd3d12/libmetalirconverter.dylib']:
            (self.root / name).unlink()
            self.assertTrue(any(name in error for error in self.errors()))

    def test_wrong_pe_machine_fails_despite_file_presence(self):
        self.put('aarch64-windows/ntdll.dll', pe(0x14c))
        self.assertTrue(any('machine' in error for error in self.errors()))

    def test_truncated_or_disguised_pe_fails(self):
        self.put('x86_64-vcruntime/vcruntime140.dll', b'not a DLL')
        self.assertTrue(any('PE' in error for error in self.errors()))

    def test_msi_requires_matching_pair(self):
        self.assertTrue(any('MSI' in error for error in self.errors(require_msi=True)))
        self.put('arm64ec-windows/msiexec.exe', pe(0x8664))
        self.assertTrue(any('msi.dll' in error for error in self.errors()))
        self.put('arm64ec-windows/msi.dll', pe(0x8664))
        self.assertEqual(self.errors(require_msi=True), [])

    def test_mono_requires_runtime_assembly_and_license(self):
        self.put('arm64ec-windows/mscoree.dll', pe(0x8664))
        self.put('wine-mono/bin/libmono-2.0-x86_64.dll', pe(0x8664))
        self.assertTrue(any('mscorlib.dll' in error for error in self.errors(require_mono=True)))
        self.put('wine-mono/lib/mono/4.5/mscorlib.dll', pe(0x14c))
        self.put('wine-mono/COPYING')
        self.assertEqual(self.errors(require_mono=True), [])

    def test_mono_wow64_requires_matching_32bit_loader(self):
        self.put('arm64ec-windows/mscoree.dll', pe(0x8664))
        self.put('wine-mono/bin/libmono-2.0-x86_64.dll', pe(0x8664))
        self.put('wine-mono/lib/mono/4.5/mscorlib.dll', pe(0x14c))
        self.put('wine-mono/COPYING')
        self.assertTrue(any('i386-windows/mscoree.dll' in error
                            for error in self.errors(require_mono=True, require_wow64=True)))
        self.put('i386-windows/mscoree.dll', pe(0x8664))
        self.assertTrue(any('mscoree.dll: PE machine' in error
                            for error in self.errors(require_mono=True, require_wow64=True)))

    def test_macos_converter_cannot_pass_ios_packaging(self):
        self.put('d3d12/libmetalirconverter.dylib', macho(1))
        self.assertTrue(any('iOS' in error for error in self.errors()))

    def test_invalid_executable_plist_is_reported_without_traceback(self):
        self.put('Info.plist', plistlib.dumps(['Madeira']))
        self.assertTrue(any('Info.plist' in error for error in self.errors()))

    def test_source_mode_does_not_claim_compiled_app_readiness(self):
        (self.root / 'Madeira').unlink()
        self.assertTrue(any('Madeira:' in error for error in self.errors()))
        self.assertEqual(self.errors(source=True), [])

    def test_malformed_xml_plist_returns_report(self):
        self.put('Info.plist', b'<?xml version="1.0"?><plist><dict>')
        self.assertTrue(any('Info.plist' in error for error in self.errors()))

    def test_executables_and_dylibs_require_correct_filetype(self):
        self.put('Madeira', macho(filetype=6))
        self.put('Frameworks/StikJIT.framework/StikJIT', macho(filetype=2))
        self.put('d3d12/libmetalirconverter.dylib', macho(filetype=2))
        errors = self.errors()
        for name in ['Madeira:', 'StikJIT:', 'libmetalirconverter.dylib:']:
            self.assertTrue(any(name in error and 'filetype' in error for error in errors))

    def test_main_debug_dependency_missing_or_wrong_platform_fails(self):
        self.put('Madeira', macho(filetype=2, dependency='@rpath/Madeira.debug.dylib'))
        self.assertTrue(any('Madeira.debug.dylib' in error for error in self.errors()))
        self.put('Madeira.debug.dylib', macho(platform=1))
        self.assertTrue(any('Madeira.debug.dylib' in error and 'iOS' in error for error in self.errors()))
        self.put('Madeira.debug.dylib', macho())
        self.assertEqual(self.errors(), [])

    def test_helper_debug_dependency_resolves_in_helper_directory(self):
        self.put('PlugIns/MadeiraJITHelper.appex/MadeiraJITHelper',
                 macho(filetype=2, dependency='@rpath/MadeiraJITHelper.debug.dylib'))
        self.put('MadeiraJITHelper.debug.dylib', macho())
        self.assertTrue(any('missing app-local' in error for error in self.errors()))
        self.put('PlugIns/MadeiraJITHelper.appex/MadeiraJITHelper.debug.dylib', macho())
        self.assertEqual(self.errors(), [])

    def test_missing_weak_dylib_allowed_but_bundled_payload_validated(self):
        self.put('Madeira', macho(filetype=2, dependency='@rpath/Optional.dylib', weak=True))
        self.assertEqual(self.errors(), [])
        self.put('Optional.dylib', macho(platform=1))
        self.assertTrue(any('Optional.dylib' in error and 'iOS' in error for error in self.errors()))

    def test_system_swift_rpath_does_not_need_host_dyld_file(self):
        self.put('Madeira', macho(filetype=2, dependency='@rpath/libswiftCore.dylib',
                                 rpaths=('/usr/lib/swift', '@executable_path')))
        self.assertEqual(self.errors(), [])
        self.put('libswiftCore.dylib', macho(platform=1))
        self.assertTrue(any('libswiftCore.dylib' in error and 'iOS' in error for error in self.errors()))

    def test_system_rpath_does_not_hide_missing_app_debug_dylib(self):
        self.put('Madeira', macho(filetype=2, dependency='@rpath/Madeira.debug.dylib',
                                 rpaths=('/usr/lib/swift', '@executable_path')))
        self.assertTrue(any('Madeira.debug.dylib' in error for error in self.errors()))

    def test_normalized_paths_cannot_escape_system_namespace(self):
        for dependency, rpaths in [('/usr/lib/../../tmp/Required.dylib', ()),
                                   ('@rpath/libswiftCore.dylib', ('/usr/lib/swift/../../../tmp',))]:
            with self.subTest(dependency=dependency):
                self.put('Madeira', macho(filetype=2, dependency=dependency, rpaths=rpaths))
                self.assertTrue(any('missing app-local' in error for error in self.errors()))

    def test_sparse_pe_headers_or_corrupt_sections_fail(self):
        for mutation in ['optional', 'table', 'raw', 'body', 'truncated']:
            with self.subTest(mutation=mutation):
                data = pe(0x14c)
                if mutation == 'optional':
                    data[88:312] = bytes(224)
                elif mutation == 'table':
                    data[312:352] = bytes(40)
                elif mutation == 'raw':
                    struct.pack_into('<I', data, 312 + 20, 1024)
                elif mutation == 'body':
                    data[512:] = bytes(512)
                else:
                    data = data[:330]
                self.put('i386-windows/ntdll.dll', data)
                self.assertTrue(any('i386-windows/ntdll.dll:' in error
                                    for error in self.errors(require_wow64=True)))

    def test_cli_fails_with_actionable_path(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.root), '--require-steam'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('dockhost.exe', result.stdout)


if __name__ == '__main__':
    unittest.main()
