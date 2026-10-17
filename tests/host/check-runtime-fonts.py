#!/usr/bin/env python3
"""Offline fixtures execute the actual Foundation CJK prefix migration helper."""
import hashlib
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FONT = 'NotoSansCJKsc-Regular.otf'
LINK = FONT + ',Noto Sans CJK SC'
FONTS = r'Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts'
LINKS = r'Software\\Microsoft\\Windows NT\\CurrentVersion\\FontLink\\SystemLink'
HIVE = ('WINE REGISTRY Version 2\n;; fixture\n\n'
        '[Software\\\\Unrelated] 123\n"Keep"="unchanged"\n\n'
        f'[{FONTS}] 456\n"Custom Font"="custom.ttf"\n\n'
        f'[{LINKS}] 789\n"Tahoma"=str(7):"custom.ttf,Custom Family\\0other.ttf,Other\\0\\0"\n'
        '"User Font"=str(7):"keep.ttf,Keep\\0\\0"\n')


class RuntimeFontsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='.madeira-cjk-build-', dir=Path.home())
        scratch = Path(cls.build.name)
        driver = scratch / 'main.m'
        driver.write_text(r'''#import <Foundation/Foundation.h>
#import "RuntimeFonts.h"
#include <errno.h>
int madeira_fixture_directory(const char *path, NSError **error);
int main(int argc, char **argv) {
 @autoreleasepool {
  NSError *error = nil;
  if (argc != 2 && argc != 4) return 2;
  BOOL ok;
  if (argc == 2) {
   int status = madeira_fixture_directory(argv[1], &error), code = errno;
   ok = status == 0;
   if (!ok) fprintf(stderr, "fixture-directory errno=%d\n", code);
  } else { ok = madeira_seed_cjk_fonts(argv[1], argv[2], argv[3], &error); }
  if (!ok) fprintf(stderr, "%s\n", error.localizedDescription.UTF8String ?: "missing error");
  return ok ? 0 : 1;
 }
}
''')
        helper_source = scratch / 'RuntimeFonts.m'
        helper_source.write_text((ROOT / 'app/Madeira/RuntimeFonts.m').read_text() + r'''
int madeira_fixture_directory(const char *path, NSError **error) {
 int fd = Directory(path, error);
 if (fd < 0) return -1;
 close(fd); return 0;
}
''')
        cls.binary = scratch / 'fixture'
        result = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra',
                                 '-Werror', '-Wno-deprecated-declarations',
                                 '-framework', 'Foundation', '-I', str(ROOT / 'app/Madeira'),
                                 str(helper_source), str(driver),
                                 '-o', str(cls.binary)], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError('actual CJK helper compilation failed:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='.madeira-cjk-', dir=Path.home())
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.prefix = self.root / 'prefix'
        self.fontdir = self.prefix / 'drive_c/windows/Fonts'
        self.fontdir.mkdir(parents=True)
        self.resources = self.root / 'cjk-fonts'
        self.resources.mkdir()
        self.payload = bytes(range(256)) * 4097 + b'font fixture'
        (self.resources / FONT).write_bytes(self.payload)
        (self.resources / 'LICENSE.txt').write_text('fixture license')
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.hive = self.prefix / 'system.reg'
        self.hive.write_text(HIVE)
        self.hive.chmod(0o640)
        self.backup = self.prefix / 'system.reg.madeira-cjk-before.reg'

    def run_helper(self, success=True, digest=None, prefix=None, sandbox=False):
        command = [str(self.binary), str(prefix or self.prefix),
                   str(self.resources), digest or self.digest]
        if sandbox:
            command = ['/usr/bin/sandbox-exec', '-p',
                       '(version 1)(allow default)(deny file-read-data (literal '
                       + json.dumps(str(Path.home().parent)) + '))'] + command
        result = subprocess.run(command,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        if not success:
            self.assertTrue(result.stderr.strip(), 'failure must carry an error')
        return result

    def test_installs_when_ancestor_above_home_is_denied(self):
        self.run_helper(sandbox=True)
        self.assertEqual((self.fontdir / FONT).read_bytes(), self.payload)
        self.assertEqual(self.backup.read_text(), HIVE)
        self.assertIn(LINK, self.hive.read_text())

    def test_trusted_home_itself_opens_and_sibling_prefix_is_rejected(self):
        profile = '(version 1)(allow default)(deny file-read-data (literal ' + json.dumps(str(Path.home().parent)) + '))'
        for path, status in ((Path.home(), 0), (Path(str(Path.home()) + '-sibling'), 1)):
            with self.subTest(path=path):
                result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile,
                                         str(self.binary), str(path)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stderr)
                if status:
                    self.assertIn(f'fixture-directory errno={errno.EINVAL}', result.stderr)

    def test_missing_directory_reports_safe_step_and_errno(self):
        result = self.run_helper(False, prefix=self.root / 'missing')
        self.assertIn('step=open-directory', result.stderr)
        self.assertIn('errno=2', result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_migration_preserves_custom_settings_and_is_idempotent(self):
        self.run_helper()
        result = self.hive.read_text()
        self.assertIn('[Software\\\\Unrelated] 123\n"Keep"="unchanged"', result)
        self.assertIn('"Custom Font"="custom.ttf"', result)
        self.assertIn('"User Font"=str(7):"keep.ttf,Keep\\0\\0"', result)
        self.assertIn('custom.ttf,Custom Family\\0other.ttf,Other\\0' + LINK + '\\0\\0', result)
        for family in ('Tahoma', 'Microsoft Sans Serif', 'Segoe UI'):
            self.assertEqual(sum(line.startswith('"' + family + '"=') for line in result.splitlines()), 1)
        self.assertEqual(result.count(LINK), 3)
        self.assertIn('"Noto Sans CJK SC (TrueType)"="' + FONT + '"', result)
        self.assertNotIn('MS Shell Dlg', result)
        self.assertEqual((self.fontdir / FONT).read_bytes(), self.payload)
        self.assertEqual(self.backup.read_text(), HIVE)
        self.assertEqual(self.hive.stat().st_mode & 0o777, 0o640)
        before = (self.hive.stat().st_ino, self.hive.stat().st_mtime_ns,
                  self.backup.stat().st_ino, (self.fontdir / FONT).stat().st_ino)
        self.run_helper()
        self.assertEqual(self.hive.read_text(), result)
        self.assertEqual(before, (self.hive.stat().st_ino, self.hive.stat().st_mtime_ns,
                                 self.backup.stat().st_ino, (self.fontdir / FONT).stat().st_ino))

    def test_absent_sections_are_added(self):
        self.hive.write_text('WINE REGISTRY Version 2\n\n[Software\\\\Other]\n"a"="b"\n')
        self.run_helper()
        self.assertEqual(self.hive.read_text().count(LINK), 3)

    def test_canonical_wine_single_visible_terminator(self):
        self.hive.write_text(HIVE.replace('other.ttf,Other\\0\\0"', 'other.ttf,Other\\0"'))
        self.run_helper()
        self.assertIn('other.ttf,Other\\0' + LINK + '\\0"', self.hive.read_text())
        first = self.hive.read_bytes()
        self.run_helper()
        self.assertEqual(self.hive.read_bytes(), first)

    def test_wine_escaped_custom_names_are_retained(self):
        original = HIVE.replace('"Custom Font"', r'"Custom \x5b8b\x4f53"').replace(
            'custom.ttf,Custom Family', r'custom.ttf,Custom \x5b8b\x4f53')
        self.hive.write_text(original)
        self.run_helper()
        self.assertIn(r'"Custom \x5b8b\x4f53"="custom.ttf"', self.hive.read_text())
        self.assertIn(r'custom.ttf,Custom \x5b8b\x4f53\0', self.hive.read_text())

    def test_escaped_section_headers_share_target_identity(self):
        escaped_fonts = FONTS.replace('Software', r'\x0053oftware')
        escaped_links = LINKS.replace('Software', r'\x0053oftware')
        original = HIVE.replace(FONTS, escaped_fonts).replace(LINKS, escaped_links)
        self.hive.write_text(original)
        self.run_helper()
        result = self.hive.read_text()
        self.assertIn('[' + escaped_fonts + '] 456', result)
        self.assertIn('[' + escaped_links + '] 789', result)
        self.assertNotIn('[' + FONTS + ']', result)
        self.assertNotIn('[' + LINKS + ']', result)
        self.assertIn('custom.ttf,Custom Family\\0other.ttf,Other\\0' + LINK + '\\0\\0', result)
        self.assertEqual(result.count(LINK), 3)
        # Wine loads both spellings into the same section, so duplicates fail closed.
        # Remove the earlier migration's backup from this phase: it must not
        # reject the replacement hive before duplicate detection is exercised.
        self.backup.rename(self.root / 'earlier-backup.reg')
        self.hive.write_text(HIVE + f'\n[{escaped_links}]\n')
        unchanged = self.hive.read_bytes()
        self.run_helper(False)
        self.assertEqual(self.hive.read_bytes(), unchanged)

    def test_foreign_font_is_never_overwritten(self):
        target = self.fontdir / FONT
        target.write_bytes(b'foreign font')
        self.run_helper(False)
        self.assertEqual(target.read_bytes(), b'foreign font')
        self.assertEqual(self.hive.read_text(), HIVE)
        self.assertFalse(self.backup.exists())

    def test_fifo_files_are_rejected_without_blocking(self):
        for target in (self.hive, self.resources / FONT, self.fontdir / FONT, self.backup):
            with self.subTest(path=target):
                retained = target.with_name(target.name + '.fixture-retained')
                had_file = target.exists()
                if had_file:
                    target.rename(retained)
                os.mkfifo(target, 0o600)
                try:
                    result = subprocess.run([str(self.binary), str(self.prefix),
                        str(self.resources), self.digest], capture_output=True,
                        text=True, timeout=2)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertTrue(result.stderr.strip())
                finally:
                    target.unlink()
                    if had_file:
                        retained.rename(target)
        self.assertEqual(self.hive.read_text(), HIVE)
        self.assertFalse(self.backup.exists())

    def test_source_hash_and_existing_destination_hash_are_rechecked(self):
        self.run_helper(False, digest='0' * 64)
        self.assertFalse((self.fontdir / FONT).exists())
        self.run_helper()
        target = self.fontdir / FONT
        inode = target.stat().st_ino
        target.write_bytes(b'x' * len(self.payload))
        self.assertEqual(inode, target.stat().st_ino)
        before = self.hive.read_bytes()
        self.run_helper(False)
        self.assertEqual(self.hive.read_bytes(), before)
        self.assertEqual(target.read_bytes(), b'x' * len(self.payload))

    def test_symlinks_at_every_untrusted_path_are_rejected(self):
        linked = self.root / 'linked-prefix'
        linked.symlink_to(self.prefix, target_is_directory=True)
        self.run_helper(False, prefix=linked)
        for relative in ('drive_c', 'drive_c/windows', 'drive_c/windows/Fonts', 'system.reg'):
            path = self.prefix / relative
            retained = self.root / ('retained-' + relative.replace('/', '-'))
            path.rename(retained)
            path.symlink_to(retained, target_is_directory=retained.is_dir())
            self.run_helper(False)
            path.unlink()
            retained.rename(path)
        target = self.fontdir / FONT
        target.symlink_to(self.resources / FONT)
        self.run_helper(False)
        target.unlink()
        source = self.resources / FONT
        retained_source = self.resources / 'retained-source.otf'
        source.rename(retained_source)
        source.symlink_to(retained_source)
        self.run_helper(False)
        self.assertEqual(self.hive.read_text(), HIVE)

    def test_malformed_or_duplicate_targets_fail_before_font_copy(self):
        bad_hives = [
            HIVE.replace('"Tahoma"=str(7):', '"Tahoma"="wrong"\n"unused"=str(7):'),
            HIVE + '"Tahoma"=str(7):"duplicate\\0\\0"\n',
            HIVE + f'\n[{LINKS}]\n',
            HIVE.replace('other.ttf,Other\\0\\0"', 'other.ttf,Other"'),
            HIVE + '\n[broken\n',
            'not a wine hive\n',
            HIVE + 'x' * (16 * 1024 * 1024),
        ]
        for content in bad_hives:
            with self.subTest(content=content[:80]):
                self.hive.write_text(content)
                self.run_helper(False)
                self.assertEqual(self.hive.read_text(), content)
                self.assertFalse((self.fontdir / FONT).exists())
                self.assertFalse(self.backup.exists())

    def test_conflicting_registration_and_blocked_backup_leave_original(self):
        self.hive.write_text(HIVE.replace('"Custom Font"="custom.ttf"',
            '"Noto Sans CJK SC (TrueType)"="foreign.ttf"'))
        self.run_helper(False)
        self.assertFalse((self.fontdir / FONT).exists())
        self.hive.write_text(HIVE)
        self.backup.mkdir()
        self.run_helper(False)
        self.assertEqual(self.hive.read_text(), HIVE)
        self.assertFalse((self.fontdir / FONT).exists())
        self.backup.rmdir()
        self.backup.symlink_to(self.hive)
        self.run_helper(False)
        self.assertEqual(self.hive.read_text(), HIVE)

    def test_incomplete_or_foreign_backup_is_retained_and_rejected(self):
        for previous in (b'', HIVE[:40].encode(), b'foreign backup'):
            with self.subTest(backup=previous):
                self.backup.write_bytes(previous)
                self.run_helper(False)
                self.assertEqual(self.backup.read_bytes(), previous)
                self.assertEqual(self.hive.read_text(), HIVE)
                self.assertFalse((self.fontdir / FONT).exists())


if __name__ == '__main__':
    unittest.main()
