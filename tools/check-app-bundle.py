#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check iPhoneOS packaging inputs, not signing or device/runtime behavior.

Optional features are required only when selected. Existing MSI/Mono payloads
are checked for consistency even when not required. Source mode checks staged
resources, not a complete built app. Never inspect user containers or accounts.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import struct
from xml.parsers.expat import ExpatError

CORE_DLLS = ('ntdll.dll', 'kernel32.dll', 'kernelbase.dll', 'user32.dll',
             'win32u.dll')
XAUDIO_DLLS = ('xaudio2_7.dll', 'xaudio2_8.dll', 'xaudio2_9.dll')
FARMS = {'aarch64-windows': 0xaa64, 'arm64ec-windows': 0x8664,
         'i386-windows': 0x14c}
VCRUNTIME_DLLS = (
    'concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcamp140.dll',
    'vccorlib140.dll', 'vcomp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
    'vcruntime140_threads.dll',
)
SHARED = (
    'prefix-template.tar.gz', 'cacert.pem', 'madeira-jit.js', 'Madeira JIT.shortcut',
    'cjk-fonts/NotoSansCJKsc-Regular.otf', 'cjk-fonts/LICENSE.txt',
    'nls/c_1252.nls', 'nls/c_20127.nls', 'nls/c_28591.nls', 'nls/c_437.nls',
    'nls/l_intl.nls', 'licenses/LICENSE-MADEIRA-GPL-3.0.txt',
    'licenses/LICENSE-MADEIRA-EXCEPTION.txt', 'licenses/THIRD-PARTY-NOTICES.txt',
    'legal/THIRD-PARTY-NOTICES.md', 'd3d12/NOTICE.txt',
    'legal/LICENSES/FAudio-zlib.txt',
    'd3d12/LICENSE-metal-shader-converter-headers.txt',
    'd3d12/METAL-SHADER-CONVERTER-AGREEMENT.txt',
    'd3d12/canary_layout_ps.dxil', 'd3d12/canary_layout_vs.dxil',
    'd3d12/canary_ps.dxil', 'd3d12/canary_vs.dxil',
)


def pe_machine(path: Path) -> int:
    """Read the DOS and COFF headers without loading the whole DLL."""
    with path.open('rb') as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b'MZ':
            raise ValueError('invalid PE DOS header')
        offset = struct.unpack_from('<I', dos, 60)[0]
        if not 64 <= offset <= 1 << 20:
            raise ValueError('invalid PE header offset')
        stream.seek(offset)
        header = stream.read(24)
        if len(header) != 24 or header[:4] != b'PE\0\0':
            raise ValueError('invalid PE COFF header')
        machine, sections = struct.unpack_from('<HH', header, 4)
        optional_size = struct.unpack_from('<H', header, 20)[0]
        if not 1 <= sections <= 96 or optional_size < 96:
            raise ValueError('invalid PE section count or optional header size')
        optional = stream.read(optional_size)
        expected_magic = 0x10b if machine == 0x14c else 0x20b
        if len(optional) != optional_size or struct.unpack_from('<H', optional)[0] != expected_magic:
            raise ValueError('invalid PE optional header magic or size')
        minimum = 96 if expected_magic == 0x10b else 112
        if optional_size < minimum:
            raise ValueError('truncated PE optional header')
        image_size, headers_size = struct.unpack_from('<II', optional, 56)
        section_table_end = offset + 24 + optional_size + sections * 40
        file_size = path.stat().st_size
        if not section_table_end <= headers_size <= file_size or image_size < headers_size:
            raise ValueError('invalid PE header/image bounds')
        table = stream.read(sections * 40)
        if len(table) != sections * 40:
            raise ValueError('truncated PE section table')
        mapped = False
        for index in range(sections):
            entry = table[index * 40:(index + 1) * 40]
            virtual_size, address, raw_size, raw_offset = struct.unpack_from('<4I', entry, 8)
            if (not entry[:8].strip(b'\0') or not max(virtual_size, raw_size)
                    or address < headers_size or address + max(virtual_size, raw_size) > image_size):
                raise ValueError('invalid PE section virtual bounds')
            if raw_size:
                if raw_offset < headers_size or raw_offset + raw_size > file_size:
                    raise ValueError('invalid PE section raw bounds')
                stream.seek(raw_offset)
                remaining = raw_size
                while remaining:
                    chunk = stream.read(min(remaining, 65536))
                    if not chunk:
                        raise ValueError('truncated PE mapped section')
                    mapped |= any(chunk)
                    remaining -= len(chunk)
        if not mapped:
            raise ValueError('PE has no nonzero mapped section data')
        return machine


def check_ios_binary(path: Path, filetype: int = 6) -> tuple[list[tuple[str, bool]], list[str]]:
    """Validate arm64 thin Mach-O and its declared iOS platform.

    The current device bundle ships thin arm64 images. A fat image is rejected
    rather than allowing an unexamined architecture/platform slice to pass.
    """
    with path.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError('truncated Mach-O header')
        magic, cpu, _, actual_type, count, size, _, _ = struct.unpack('<8I', header)
        if magic != 0xfeedfacf or cpu != 0x100000c:
            raise ValueError('expected thin arm64 Mach-O for iOS')
        if actual_type != filetype:
            raise ValueError(f'Mach-O filetype {actual_type}, expected {filetype}')
        if size > 16 << 20 or count > size // 8:
            raise ValueError('invalid Mach-O load commands')
        commands = stream.read(size)
        if len(commands) != size:
            raise ValueError('truncated Mach-O load commands')
        offset = 0
        ios = False
        dependencies, rpaths = [], []
        for _ in range(count):
            if offset + 8 > size:
                raise ValueError('truncated Mach-O load command')
            command, length = struct.unpack_from('<II', commands, offset)
            if length < 8 or offset + length > size:
                raise ValueError('invalid Mach-O load command size')
            if command in (0xc, 0x8000001f, 0x80000018, 0x8000001c):
                minimum = 12 if command == 0x8000001c else 24
                if length < minimum:
                    raise ValueError('truncated Mach-O dylib/rpath command')
                start = struct.unpack_from('<I', commands, offset + 8)[0]
                value = commands[offset + start:offset + length]
                if start < minimum or start >= length or b'\0' not in value:
                    raise ValueError('invalid Mach-O dylib/rpath string')
                name = value.split(b'\0', 1)[0].decode('utf-8')
                if command == 0x8000001c:
                    rpaths.append(name)
                else:
                    dependencies.append((name, command == 0x80000018))
            if command == 0x32:
                if length < 24 or struct.unpack_from('<I', commands, offset + 8)[0] != 2:
                    raise ValueError('Mach-O platform is not device iOS')
                ios = True
            elif command == 0x25 and length >= 16:  # LC_VERSION_MIN_IPHONEOS
                ios = True
            offset += length
        if not ios:
            raise ValueError('missing device iOS platform declaration')
        return dependencies, rpaths


def check_bundle(root: Path, *, source: bool = False, require_steam: bool = False,
                 require_wow64: bool = False, require_mono: bool = False,
                 require_msi: bool = False) -> list[str]:
    errors = []

    def present(relative):
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f'{relative}: missing or empty file')
            return False
        return True

    visited = set()

    def binary(relative, machine=None, filetype=6, executable_dir=None, inherited_rpaths=()):
        if not present(relative):
            return
        try:
            if machine is None:
                path = (root / relative).resolve()
                context = (path, filetype, executable_dir)
                if context in visited:
                    return
                visited.add(context)
                dependencies, rpaths = check_ios_binary(path, filetype)
                executable_dir = executable_dir or path.parent

                def expand(name):
                    return Path(name.replace('@executable_path', str(executable_dir))
                                .replace('@loader_path', str(path.parent)))

                search = [expand(name) for name in rpaths] + list(inherited_rpaths)
                for name, weak in dependencies:
                    canonical = Path(os.path.normpath(str(expand(name))))
                    if (name.startswith('/') and any(canonical.is_relative_to(directory)
                            for directory in (Path('/usr/lib'), Path('/System/Library')))):
                        continue  # SDK/dyld-shared-cache payloads are not app resources.
                    if name.startswith('@rpath/'):
                        candidates = [directory / name[7:] for directory in search]
                    else:
                        candidates = [expand(name)]
                    local = [candidate.resolve() for candidate in candidates
                             if candidate.resolve().is_relative_to(root.resolve())]
                    target = next((candidate for candidate in local if candidate.is_file()), None)
                    if target is None:
                        # Only Apple's Swift install-name namespace can be satisfied
                        # by a system rpath without a file on this macOS host. An
                        # unrelated app dylib still needs its bundled payload.
                        swift_system = (name.startswith('@rpath/libswift')
                                        and name.endswith('.dylib')
                                        and '/' not in name[7:]
                                        and any(Path(os.path.normpath(str(candidate))).parent == Path('/usr/lib/swift')
                                                for candidate in candidates))
                        if weak or swift_system:
                            continue
                        errors.append(f'{relative}: missing app-local dylib dependency {name}')
                        continue
                    binary(str(target.relative_to(root.resolve())), filetype=6,
                           executable_dir=executable_dir, inherited_rpaths=search)
            else:
                actual = pe_machine(root / relative)
                if actual != machine:
                    raise ValueError(f'PE machine 0x{actual:04x}, expected 0x{machine:04x}')
        except (OSError, ValueError) as error:
            errors.append(f'{relative}: {error}')

    def executable(bundle, filetype=2):
        relative = str(bundle / 'Info.plist')
        if not present(relative):
            return
        try:
            with (root / relative).open('rb') as stream:
                info = plistlib.load(stream)
            if not isinstance(info, dict):
                raise ValueError('Info.plist is not a dictionary')
            name = info.get('CFBundleExecutable')
            if (not isinstance(name, str) or not name or name in ('.', '..')
                    or Path(name).name != name):
                raise ValueError('invalid CFBundleExecutable')
            binary(str(bundle / name), filetype=filetype,
                   executable_dir=(root / bundle).resolve())
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            errors.append(f'{relative}: {error}')

    for relative in SHARED:
        present(relative)
    binary('d3d12/libmetalirconverter.dylib')
    for farm in ('aarch64-windows', 'arm64ec-windows'):
        for name in CORE_DLLS:
            binary(f'{farm}/{name}', FARMS[farm])
    binary('arm64ec-windows/xtajit64.dll', 0x8664)
    binary('arm64ec-windows/wintypes.dll', 0x8664)
    binary('arm64ec-windows/faultrep.dll', 0x8664)
    for name in ('services.exe', 'wineboot.exe'):
        binary('aarch64-windows/' + name, 0xaa64)
    for name in XAUDIO_DLLS:
        binary('arm64ec-windows/' + name, 0x8664)
    for name in VCRUNTIME_DLLS:
        binary('x86_64-vcruntime/' + name, 0x8664)
    if not source:
        executable(Path('.'))
        executable(Path('PlugIns/MadeiraJITHelper.appex'))
        executable(Path('Frameworks/StikJIT.framework'), filetype=6)
        for name in ('universal.js', 'legacy.js'):
            present('Frameworks/StikJIT.framework/' + name)
    if require_steam or (root / 'arm64ec-windows/dockhost.exe').exists():
        binary('arm64ec-windows/dockhost.exe', 0x8664)
        present('arm64ec-windows/dock-notices.txt')
    if require_wow64:
        for name in CORE_DLLS + ('winegstreamer.dll', 'wmadmod.dll'):
            binary('i386-windows/' + name, 0x14c)
        for name in ('wow64.dll', 'wow64win.dll', 'xtajit.dll'):
            binary('aarch64-windows/' + name, 0xaa64)
    pairs = 0
    for farm, machine in FARMS.items():
        exe = (root / farm / 'msiexec.exe').exists()
        dll = (root / farm / 'msi.dll').exists()
        if exe or dll:
            binary(f'{farm}/msiexec.exe', machine)
            binary(f'{farm}/msi.dll', machine)
            pairs += int(exe and dll)
    if require_msi and pairs == 0:
        errors.append('MSI: no farm contains both msiexec.exe and msi.dll')
    if require_mono or (root / 'wine-mono').exists():
        binary('arm64ec-windows/mscoree.dll', 0x8664)
        binary('wine-mono/bin/libmono-2.0-x86_64.dll', 0x8664)
        if require_wow64:
            binary('i386-windows/mscoree.dll', 0x14c)
            binary('wine-mono/bin/libmono-2.0-x86.dll', 0x14c)
        binary('wine-mono/lib/mono/4.5/mscorlib.dll', 0x14c)
        present('wine-mono/COPYING')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path, help='device .app or staged source resource directory')
    parser.add_argument('--source', action='store_true', help='check staged resources only; omit compiled app/JIT helper')
    for feature in ('steam', 'wow64', 'mono', 'msi'):
        parser.add_argument('--require-' + feature, action='store_true')
    parser.add_argument('--json', action='store_true', help='emit a JSON report to stdout')
    arguments = vars(parser.parse_args())
    root = arguments.pop('bundle')
    as_json = arguments.pop('json')
    errors = check_bundle(root, **arguments)
    mode = 'source resources' if arguments['source'] else 'device app packaging'
    if as_json:
        print(json.dumps({'path': str(root), 'mode': mode, 'passed': not errors,
                          'errors': errors, 'runtime_verified': False}, indent=2))
    else:
        for error in errors:
            print('FAIL: ' + error)
        print(f'{"FAIL" if errors else "PASS"}: {mode}; runtime and signing not verified')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
