#!/usr/bin/env python3
"""Reject missing or incompatible fault reporting providers before packaging."""
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'bundle_fixtures', Path(__file__).with_name('check-app-bundle.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class FaultrepPackageTests(unittest.TestCase):
    def setUp(self):
        fixtures.BundleTests.setUp(self)
        self.put('arm64ec-windows/faultrep.dll', fixtures.pe(0x8664))

    def faultrep_errors(self, source):
        return [error for error in fixtures.checker.check_bundle(self.root, source=source)
                if 'arm64ec-windows/faultrep.dll' in error]

    def test_arm64ec_provider_passes_source_and_device_packaging(self):
        for source in (True, False):
            with self.subTest(source=source):
                self.assertEqual(fixtures.checker.check_bundle(self.root, source=source), [])

    def test_i386_provider_elsewhere_does_not_satisfy_64_bit_game(self):
        (self.root / 'arm64ec-windows/faultrep.dll').unlink()
        self.put('i386-windows/faultrep.dll', fixtures.pe(0x14c))
        for source in (True, False):
            with self.subTest(source=source):
                self.assertEqual(self.faultrep_errors(source),
                                 ['arm64ec-windows/faultrep.dll: missing or empty file'])

    def test_incompatible_providers_are_rejected_in_both_modes(self):
        for machine in (0x14c, 0xaa64):
            self.put('arm64ec-windows/faultrep.dll', fixtures.pe(machine))
            for source in (True, False):
                with self.subTest(machine=machine, source=source):
                    self.assertEqual(self.faultrep_errors(source), [
                        f'arm64ec-windows/faultrep.dll: PE machine 0x{machine:04x}, expected 0x8664'])

    def test_empty_provider_is_rejected_in_both_modes(self):
        self.put('arm64ec-windows/faultrep.dll', b'')
        for source in (True, False):
            with self.subTest(source=source):
                self.assertEqual(self.faultrep_errors(source),
                                 ['arm64ec-windows/faultrep.dll: missing or empty file'])

    def test_malformed_provider_is_rejected_in_both_modes(self):
        self.put('arm64ec-windows/faultrep.dll', b'not a DLL')
        for source in (True, False):
            with self.subTest(source=source):
                self.assertEqual(self.faultrep_errors(source),
                                 ['arm64ec-windows/faultrep.dll: invalid PE DOS header'])


if __name__ == '__main__':
    unittest.main()
