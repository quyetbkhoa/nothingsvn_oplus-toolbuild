from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt.flash_bundle import bundle_output
from colornt.io import WRITE_GUARD
import test_flash_script as fixtures


class FlashBundleTests(unittest.TestCase):
    def test_moved_output_uses_local_report_and_still_blocks_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, _, report = fixtures.FlashScriptTests().fixture(root)
            self.assertTrue(bundle_output(folder, report)['target_supported'])
            destination = root / 'another location' / 'ROM with spaces & symbols'
            destination.parent.mkdir()
            folder.rename(destination)
            _, images, loaded = fixtures.flash.load_package(destination)
            self.assertEqual(len(images), len(report['images']))
            self.assertEqual(loaded['status'], 'package-blocked')
            with patch.object(fixtures.flash.subprocess, 'run') as process, redirect_stdout(io.StringIO()):
                self.assertEqual(fixtures.flash.main(['--rom', str(destination)]), 0)
                with self.assertRaisesRegex(ValueError, 'PACKAGE BLOCKED'):
                    fixtures.flash.main(['--rom', str(destination), '--execute'])
                process.assert_not_called()
            launcher = (destination / 'Flash-ROM.bat').read_bytes()
            self.assertIn(b'%~dp0ColorNT-Flash', launcher)
            self.assertNotIn(b'..\\..\\scripts', launcher)

    def test_fresh_sidecar_failure_is_not_overridden_by_local_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, _, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            bundle_output(folder, report)
            report['status'] = 'failed'
            Path(str(folder) + '.build.json').write_text(json.dumps(report))
            self.assertEqual(fixtures.flash.load_package(folder)[2]['status'], 'failed')

    def test_moved_bundle_retains_failed_boot_when_ready_fields_are_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, _, report = fixtures.FlashScriptTests().fixture(root)
            report['status'] = 'experimental-package-ready'
            report['package_validation']['experimental_package_ready'] = True
            report['device_test'] = {'boot': 'failed', 'evidence': 'local boot feedback'}
            bundle_output(folder, report)
            moved = root / 'moved';folder.rename(moved)
            _, images, loaded = fixtures.flash.load_package(moved)
            self.assertEqual(loaded['device_test'], report['device_test'])
            with patch.object(fixtures.flash.subprocess, 'run') as process, redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'recorded device boot failure'):
                    fixtures.flash.main(['--rom', str(moved), '--execute', '--wipe'])
                process.assert_not_called()

    def test_unrecognized_target_launcher_does_not_invoke_sample_flash_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, _, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            report['base']['identity']['product_name'] = 'OTHER_DEVICE'
            self.assertFalse(bundle_output(folder, report)['target_supported'])
            launcher = (folder / 'Flash-ROM.bat').read_text()
            self.assertNotIn('python ', launcher)
            self.assertIn('exit /b 2', launcher)

    def test_companion_write_respects_quota_and_existing_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, _, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            def deny(path, size):
                raise ValueError('quota denied before companion bytes')
            token = WRITE_GUARD.set(deny)
            try:
                with self.assertRaisesRegex(ValueError, 'quota denied'):
                    bundle_output(folder, report)
            finally:
                WRITE_GUARD.reset(token)
            self.assertFalse((folder / 'Flash-ROM.bat').exists())
        with tempfile.TemporaryDirectory() as tmp:
            folder, _, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            (folder / 'Flash-ROM.bat').write_text('keep original')
            with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
                bundle_output(folder, report)
            self.assertEqual((folder / 'Flash-ROM.bat').read_text(), 'keep original')
