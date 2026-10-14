"""Only offline/mock execution: never spawn adb/fastboot or touch a device."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('flash_script', Path(__file__).resolve().parents[1] / 'scripts/flash_from_bootloader.py')
flash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flash)


class FakeFastboot:
    def __init__(self, failure=None, slot='a', cow=False):
        self.calls, self.mode, self.failure, self.slot, self.cow = [], False, failure, slot, cow

    def run(self, args, timeout=None):
        self.calls.append(list(args))
        if args == self.failure:
            raise RuntimeError('injected failure')
        if args == ['reboot', 'fastboot']:
            self.mode = True
        elif args == ['reboot', 'bootloader']:
            self.mode = False
        if args == ['getvar', 'all']:
            return 'partition-size:system_a-cow: 0x1000\n' if self.cow else 'partition-size:system_a: 0x1000\n'
        return 'OKAY\n'

    def getvar(self, name):
        self.calls.append(['getvar', name])
        if name.startswith('partition-size:'):
            return '0x100000'
        if name.startswith('is-logical:'):
            return 'yes'
        return {'product': 'target-userspace' if self.mode else 'target-bootloader',
                'unlocked': 'yes', 'current-slot': self.slot, 'slot-count': '2',
                'is-userspace': 'yes' if self.mode else 'no'}[name]


class FlashScriptTests(unittest.TestCase):
    def test_failed_boot_and_slot_commands_are_counted_as_device_mutation_attempts(self):
        for args in (['boot', 'recovery.img'], ['set_active', 'a'], ['format:f2fs', 'metadata']):
            with self.subTest(args=args), redirect_stdout(io.StringIO()):
                driver = flash.Fastboot('mock-fastboot', None, io.StringIO())
                result = subprocess.CompletedProcess([], 0, '', 'FAILED (remote: rejected)')
                with patch.object(flash.subprocess, 'run', return_value=result):
                    with self.assertRaises(RuntimeError):
                        driver.run(args)
                self.assertEqual(driver.mutating_attempts, 1)

    def fixture(self, root):
        folder = root / 'ROM with spaces & symbols'
        folder.mkdir()
        names = set(flash.BOTH_SLOTS) | set(flash.BOOTSTRAP) | (set(flash.LOGICAL_A) - flash.LOG_ONLY_IMAGES) | {'modem'} | flash.UNOBSERVED_IMAGES
        images, rows = {}, []
        for name in names:
            image = folder / (name + '.img')
            image.write_bytes(b'fixture')
            images[name] = image
            rows.append({'partition': name, 'path': image.name, 'image': {'size': 7}})
        (folder / 'firmware_info.json').write_text(json.dumps({'product_name': 'PJF110'}))
        report = {'output': str(folder), 'status': 'package-blocked', 'images': rows,
                  'base': {'identity': {'product_name': 'PJF110'}}, 'package_validation': {'experimental_package_ready': False}}
        Path(str(folder) + '.build.json').write_text(json.dumps(report))
        return folder, images, report

    def test_offline_plan_and_blocked_execute_never_start_a_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(flash.subprocess, 'run') as process, redirect_stdout(io.StringIO()):
            folder, _, _ = self.fixture(Path(tmp))
            self.assertEqual(flash.main(['--rom', str(folder)]), 0)
            with self.assertRaisesRegex(ValueError, 'PACKAGE BLOCKED'):
                flash.main(['--rom', str(folder), '--execute'])
            process.assert_not_called()

    def test_inventory_change_is_rejected_before_a_device_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, images, _ = self.fixture(Path(tmp))
            images['system'].write_bytes(b'truncated')
            with self.assertRaisesRegex(ValueError, 'size differs'):
                flash.load_package(folder)
            images['system'].unlink()
            with self.assertRaisesRegex(ValueError, 'Missing required IMG'):
                flash.load_package(folder)

    def test_slot_phase_order_and_explicit_wipe_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, images, _ = self.fixture(Path(tmp))
            plan = flash.make_plan(images)
            self.assertEqual(len(plan), 84)
            commands = [args for _, args in plan]
            self.assertNotIn(['-w'], commands)
            self.assertNotIn(['reboot'], commands)
            targets = [args[1] for args in commands if args[0] == 'flash']
            self.assertNotIn('recovery_a', targets)
            self.assertNotIn('engineering_cdt_a', targets)
            self.assertNotIn('system_b', targets)
            self.assertIn('abl_a', targets); self.assertIn('abl_b', targets)
            fb = FakeFastboot()
            with patch.object(flash.time, 'sleep'):
                flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images, True, True), 'target-userspace')
            self.assertEqual(fb.calls[-2:], [['-w'], ['reboot']])

    def test_remote_failure_never_continues_to_next_flash_wipe_or_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, images, _ = self.fixture(Path(tmp))
            failed = ['flash', 'system_a', str(images['system'])]
            fb = FakeFastboot(failure=failed)
            with patch.object(flash.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'injected'):
                flash.execute_plan(fb, images, 'target-bootloader', flash.make_plan(images, True, True), 'target-userspace')
            self.assertEqual(fb.calls[-1], failed)
            self.assertNotIn(['-w'], fb.calls); self.assertNotIn(['reboot'], fb.calls)

    def test_wrong_slot_and_cow_state_stop_at_their_validation_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, images, _ = self.fixture(Path(tmp))
            plan = flash.make_plan(images, True, True)
            fb = FakeFastboot(slot='b')
            with self.assertRaisesRegex(RuntimeError, 'slot A'):
                flash.execute_plan(fb, images, 'target-bootloader', plan, 'target-userspace')
            self.assertFalse(any(c[0] == 'flash' for c in fb.calls))
            fb = FakeFastboot(cow=True)
            with patch.object(flash.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'COW'):
                flash.execute_plan(fb, images, 'target-bootloader', plan, 'target-userspace')
            self.assertFalse(any(c[:2] == ['flash', 'abl_a'] for c in fb.calls))
            self.assertNotIn(['-w'], fb.calls)

    def test_runner_uses_argument_array_and_checks_exit_failure_and_timeout(self):
        log = io.StringIO()
        runner = flash.Fastboot('fake-tool.exe', 'test-serial', log)
        args = ['flash', 'system_a', 'C:/ROM with spaces & symbols/system.img']
        with redirect_stdout(io.StringIO()), patch.object(flash.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'FAILED')) as process:
            with self.assertRaisesRegex(RuntimeError, 'Command failed'):
                runner.run(args)
            self.assertEqual(process.call_args.args[0], ['fake-tool.exe', '-s', 'test-serial'] + args)
            self.assertFalse(process.call_args.kwargs['shell'])
        with redirect_stdout(io.StringIO()), patch.object(flash.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                runner.run(['devices'])


if __name__ == '__main__':
    unittest.main()
