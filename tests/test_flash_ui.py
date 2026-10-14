"""UI/recovery/layout regressions with mocked tools; never access a device."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_flash_script import flash, FakeFastboot
from test_flash_menu import MenuFastboot
import test_flash_script as fixtures
import test_flash_menu as menu_fixtures


class RecoveringFastboot(FakeFastboot):
    def __init__(self, failures):
        super().__init__()
        self.failures = dict(failures)

    def run(self, args, timeout=None):
        if args[0] == 'flash' and self.failures.get(args[1], 0):
            self.calls.append(list(args))
            self.failures[args[1]] -= 1
            raise RuntimeError('FAILED (remote: partition rejected)')
        return super().run(args, timeout)


class FlashUITests(unittest.TestCase):
    def test_layouts_allowlist_case_and_unknown_images(self):
        for firmware, system in [('BOOTLOADER', 'SYSTEM'), ('firmware-update', 'system'), ('CRITICAL', 'nested/os')]:
            with self.subTest(layout=firmware), tempfile.TemporaryDirectory() as tmp:
                folder, images, report = fixtures.FlashScriptTests().fixture(Path(tmp))
                for name, original in images.items():
                    dest = folder / (system if name in flash.LOGICAL_A else firmware) / original.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    original.rename(dest)
                    row = next(r for r in report['images'] if r['partition'] == name)
                    row['path'] = dest.relative_to(folder).as_posix()
                (folder / 'random.IMG').write_bytes(b'NEVER FLASH')
                (folder / 'userdata.img').write_bytes(b'NEVER ERASE')
                Path(str(folder) + '.build.json').write_text(json.dumps(report))
                _, found, loaded = flash.load_package(folder)
                self.assertNotIn('random', found)
                self.assertNotIn('userdata', found)
                self.assertEqual(set(loaded['ignored_images']), {'random.IMG', 'userdata.img'})
                targets = [a[1] for _, a in flash.make_plan(found) if a[0] == 'flash']
                self.assertFalse(any(n.startswith(('random', 'userdata', 'recovery', 'engineering_cdt')) for n in targets))

    def test_repackaged_file_requires_and_verifies_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, images, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            image = images['system']
            dest = folder / 'system' / 'system.IMG'; dest.parent.mkdir(); image.rename(dest)
            with self.assertRaisesRegex(ValueError, 'requires report SHA256'):
                flash.load_package(folder)
            row = next(r for r in report['images'] if r['partition'] == 'system')
            row['image']['sha256'] = hashlib.sha256(dest.read_bytes()).hexdigest()
            Path(str(folder) + '.build.json').write_text(json.dumps(report))
            self.assertEqual(flash.load_package(folder)[1]['system'], dest)
            dest.write_bytes(b'corrupt')  # same size
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                flash.load_package(folder)

    def test_duplicate_allowed_partition_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, _, _ = fixtures.FlashScriptTests().fixture(Path(tmp))
            nested = folder / 'firmware-update'; nested.mkdir()
            (nested / 'BOOT.IMG').write_bytes(b'fixture')
            with self.assertRaisesRegex(ValueError, 'Duplicate partition'):
                flash.load_package(folder)

    def test_repackaged_candidate_uses_bound_manifest_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, images, report = fixtures.FlashScriptTests().fixture(Path(tmp))
            report_bytes = Path(str(folder) + '.build.json').read_bytes()
            (folder / 'BUILD-REPORT.json').write_bytes(report_bytes)
            rows = [{'partition': r['partition'], 'path': r['path'], 'size': r['image']['size'],
                     'sha256': hashlib.sha256(images[r['partition']].read_bytes()).hexdigest()} for r in report['images']]
            manifest = {'build_report_sha256': hashlib.sha256(report_bytes).hexdigest(), 'images': rows}
            (folder / 'BUILD-MANIFEST.json').write_text(json.dumps(manifest))
            dest = folder / 'system' / 'system.img'; dest.parent.mkdir(); images['system'].rename(dest)
            self.assertEqual(flash.load_package(folder)[1]['system'], dest)
            dest.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                flash.load_package(folder)
            (folder / 'BUILD-REPORT.json').write_bytes(report_bytes + b' ')
            with self.assertRaisesRegex(ValueError, 'manifest does not match'):
                flash.load_package(folder)

    def test_retry_rejects_changed_identity_and_retains_unresolved_failure(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(flash.time, 'sleep'):
            _, images, _ = fixtures.FlashScriptTests().fixture(Path(tmp))
            fb = RecoveringFastboot({'system_a': 1})
            def retry(partition, reason):
                fb.slot = 'b'
                return 'retry'
            failures = []
            with self.assertRaisesRegex(RuntimeError, 'slot A'):
                flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images, True, True),
                                   'target-userspace', on_failure=retry, failures=failures)
            self.assertEqual([r['partition'] for r in failures], ['system_a'])
            self.assertEqual(sum(a[:2] == ['flash', 'system_a'] for a in fb.calls), 1)
            self.assertNotIn(['-w'], fb.calls)

    def test_color_alignment_compact_output_and_full_raw_log(self):
        out, log = io.StringIO(), io.StringIO()
        with redirect_stdout(out):
            fb = flash.Fastboot('never-run.exe', 'serial', log, console=flash.Console(color=True))
            response = subprocess.CompletedProcess([], 0, '',
                "Sending sparse 'my_product_a' 1/2 (4 KB) OKAY [1s]\n"
                "Writing 'my_product_a' OKAY [2s]\nSending sparse 'my_product_a' 2/2 (4 KB) OKAY [1s]\n"
                "Writing 'my_product_a' OKAY [2s]\nFinished. Total time: 6s\n")
            with patch.object(flash.subprocess, 'run', return_value=response):
                fb.run(['flash', 'my_product_a', 'C:/long ROM path/my_product.img'])
        self.assertIn('\033[32mOKAY', out.getvalue())
        plain = re.sub(r'\x1b\[[0-9;]*m', '', out.getvalue())
        status = [s for s in plain.splitlines() if 'OKAY' in s]
        self.assertEqual(len(status), 2)
        self.assertEqual(status[0].index('OKAY'), status[1].index('OKAY'))
        self.assertNotIn('long ROM path', plain)
        self.assertNotIn('Total time', plain)
        self.assertIn('long ROM path', log.getvalue())
        self.assertIn('Total time', log.getvalue())

    def test_failed_write_preserves_green_sending_and_red_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            fb = flash.Fastboot('fake.exe', None, io.StringIO(), console=flash.Console(color=True))
            response = subprocess.CompletedProcess([], 0, '', "Sending 'boot_a' OKAY\nWriting 'boot_a' FAILED (remote: denied)\n")
            with patch.object(flash.subprocess, 'run', return_value=response), self.assertRaisesRegex(RuntimeError, 'denied'):
                fb.run(['flash', 'boot_a', 'boot.img'])
        self.assertIn('\033[32mOKAY', out.getvalue())
        self.assertIn('\033[31mFAILED', out.getvalue())
        self.assertIn('denied', out.getvalue())

    def test_retry_only_failed_operation_and_rechecks_identity(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(flash.time, 'sleep'):
            _, images, _ = fixtures.FlashScriptTests().fixture(Path(tmp))
            fb = RecoveringFastboot({'system_a': 1})
            callback = unittest.mock.Mock(return_value='retry')
            log = io.StringIO()
            failures = flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images, True, True),
                                          'target-userspace', on_failure=callback, log=log)
            flashes = [a[1] for a in fb.calls if a[0] == 'flash']
            self.assertEqual(flashes.count('system_a'), 2)
            self.assertEqual(flashes.count('abl_a'), 1)
            self.assertEqual(failures, [])
            idx = next(i for i, a in enumerate(fb.calls) if a[:2] == ['flash', 'system_a'])
            self.assertEqual(fb.calls[idx + 1], ['getvar', 'product'])
            self.assertEqual(fb.calls[-2:], [['-w'], ['reboot']])
            self.assertIn('ACTION: retry', log.getvalue())

    def test_skip_all_later_errors_summary_first_and_no_wipe_reboot(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as printed, patch.object(flash.time, 'sleep'):
            _, images, _ = fixtures.FlashScriptTests().fixture(Path(tmp))
            fb = RecoveringFastboot({'system_a': 1, 'modem_a': 1})
            callback = unittest.mock.Mock(return_value='skip')
            path = Path(tmp) / 'result.log'; log = flash.SessionLog(path)
            flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images, True, True),
                               'target-userspace', on_failure=callback, failures=log.failures, log=log)
            log.close()
            self.assertEqual(callback.call_count, 1)
            self.assertEqual([r['partition'] for r in log.failures], ['system_a', 'modem_a'])
            self.assertIn(['flash', 'modem_b', str(images['modem'])], fb.calls)
            self.assertNotIn(['-w'], fb.calls); self.assertNotIn(['reboot'], fb.calls)
            header = path.read_text().split('FULL COMMAND')[0]
            self.assertIn('system_a', header); self.assertIn('modem_a', header); self.assertIn('partition rejected', header)
            self.assertIn('0%', printed.getvalue()); self.assertIn('100%', printed.getvalue())

    def test_mode_failure_cannot_be_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(flash.time, 'sleep'):
            _, images, _ = fixtures.FlashScriptTests().fixture(Path(tmp))
            fb = FakeFastboot(failure=['reboot', 'fastboot'])
            callback = unittest.mock.Mock(return_value='skip')
            with self.assertRaises(RuntimeError):
                flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images), 'target-userspace', on_failure=callback)
            callback.assert_not_called()
            self.assertNotIn(['flash', 'abl_a', str(images['abl'])], fb.calls)

    def test_main_skip_returns_failure_and_publishes_log(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(flash.time, 'sleep'):
            folder, executable = menu_fixtures.FlashMenuTests().fixture(Path(tmp))
            fb = MenuFastboot(failure=['flash', 'system_a', str(folder / 'system.img')])
            with patch('builtins.input', side_effect=['2', 'skip']), patch.object(flash, 'Fastboot', return_value=fb), \
                    patch.object(flash.subprocess, 'run', side_effect=AssertionError('No device process')):
                result = flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable),
                                     '--expected-product', 'target-bootloader', '--expected-fastbootd-product', 'target-userspace'])
            self.assertEqual(result, 1)
            self.assertNotIn(['-w'], fb.calls); self.assertNotIn(['reboot'], fb.calls)
            logs = list(Path(tmp).glob('*.flash-*.log'))
            self.assertEqual(len(logs), 1)
            self.assertIn('system_a', logs[0].read_text().split('FULL COMMAND')[0])

    def test_failure_prompt_invalid_retry_skip_and_eof(self):
        for inputs, expected in [(['bad', 'r'], 'retry'), (['k'], 'skip'), ([''], 'stop')]:
            with patch('builtins.input', side_effect=inputs), redirect_stdout(io.StringIO()):
                self.assertEqual(flash.choose_failure('system_a', 'reason'), expected)
        with patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()):
            self.assertEqual(flash.choose_failure('system_a', 'reason'), 'stop')
