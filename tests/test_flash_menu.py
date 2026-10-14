from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_flash_script import flash, FakeFastboot
import test_flash_script as flash_fixtures
from test_unlocked_avb import root_bytes
from colornt.avb import inspect_avb


class MenuFastboot(FakeFastboot):
    def run(self, args, timeout=None):
        if args == ['devices']:
            self.calls.append(list(args))
            return 'test-serial\tfastboot\n'
        return super().run(args, timeout)


class FlashMenuTests(unittest.TestCase):
    def fixture(self, root):
        folder, images, report = flash_fixtures.FlashScriptTests().fixture(root)
        images['vbmeta'].write_bytes(root_bytes())
        next(r for r in report['images'] if r['partition'] == 'vbmeta')['image']['size'] = 4096
        report.update(status='experimental-package-ready',
            package_validation={'assembled': True, 'filesystem_verified': True, 'structural_checks_passed': True,
                                'structural_issues': [], 'experimental_package_ready': True},
            avb_policy={'kind': 'unlocked-top-level-disabled-v1', 'requires_unlocked': True,
                        'requires_allow_verification_error': True, 'avbtool_metadata_verification': 'pass-unsigned-NONE',
                        'top_metadata_sha256': inspect_avb(images['vbmeta'])['metadata_sha256']})
        Path(str(folder) + '.build.json').write_text(json.dumps(report))
        executable = root / 'not-a-real-tool.exe';executable.write_bytes(b'never execute')
        return folder, executable

    def test_menu_flash_choice_wipes_only_for_option_two_and_reboots_after_success(self):
        for choice in ('1', '2'):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as tmp:
                folder, executable = self.fixture(Path(tmp))
                fb = MenuFastboot()
                with patch('builtins.input', return_value=choice), patch.object(flash, 'Fastboot', return_value=fb), \
                        patch.object(flash.subprocess, 'run', side_effect=AssertionError('No real process allowed')), \
                        patch.object(flash.time, 'sleep'), redirect_stdout(io.StringIO()):
                    self.assertEqual(flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable),
                                                '--expected-product', 'target-bootloader',
                                                '--expected-fastbootd-product', 'target-userspace']), 0)
                self.assertEqual(['-w'] in fb.calls, choice == '2')
                self.assertEqual(fb.calls[-1], ['reboot'])

    def test_menu_exit_and_plan_never_start_a_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, _ = self.fixture(Path(tmp))
            for choice in ('0', '3'):
                with patch('builtins.input', return_value=choice), redirect_stdout(io.StringIO()), \
                        patch.object(flash.subprocess, 'run') as process, patch.object(flash, 'Fastboot') as runner:
                    self.assertEqual(flash.main(['--menu', '--rom', str(folder)]), 0)
                    process.assert_not_called();runner.assert_not_called()

    def test_invalid_menu_input_reprompts_and_failure_before_wipe_preserves_stop_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, executable = self.fixture(Path(tmp))
            fb = MenuFastboot(failure=['flash', 'modem_b', str(folder / 'modem.img')])
            with patch('builtins.input', side_effect=['invalid', '2', 'stop']), patch.object(flash, 'Fastboot', return_value=fb), \
                    patch.object(flash.time, 'sleep'), redirect_stdout(io.StringIO()), \
                    patch.object(flash.subprocess, 'run', side_effect=AssertionError('No real process allowed')):
                with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                    flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable),
                                '--expected-product', 'target-bootloader', '--expected-fastbootd-product', 'target-userspace'])
            self.assertNotIn(['-w'], fb.calls);self.assertNotIn(['reboot'], fb.calls)

    def test_connection_menu_uses_only_read_commands_even_for_a_blocked_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, executable = self.fixture(Path(tmp))
            p = Path(str(folder) + '.build.json');report = json.loads(p.read_text())
            report['status'] = 'package-blocked';report['package_validation']['experimental_package_ready'] = False
            p.write_text(json.dumps(report))
            fb = MenuFastboot()
            with patch('builtins.input', return_value='4'), patch.object(flash, 'Fastboot', return_value=fb), \
                    patch.object(flash.subprocess, 'run', side_effect=AssertionError('No real process')), redirect_stdout(io.StringIO()):
                self.assertEqual(flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable)]), 0)
            self.assertEqual(fb.calls, [['devices']] + [['getvar', n] for n in
                                                     ('product', 'unlocked', 'current-slot', 'slot-count', 'is-userspace')])

    def test_usb_failure_before_first_flash_reports_zero_mutating_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, executable = self.fixture(Path(tmp))
            responses = [flash.subprocess.CompletedProcess([], 0, 'test-serial\tfastboot\n', ''),
                         flash.subprocess.CompletedProcess([], 1, '', 'FAILED (AdbWriteEndpointSync failed: error (31))')]
            printed = io.StringIO()
            with patch('builtins.input', return_value='2'), patch.object(flash.subprocess, 'run', side_effect=responses) as process, \
                    redirect_stdout(printed), self.assertRaisesRegex(RuntimeError, 'USB TRANSPORT ERROR'):
                flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable),
                            '--expected-product', 'target-bootloader', '--expected-fastbootd-product', 'target-userspace'])
            self.assertEqual(len(process.call_args_list), 2)
            self.assertIn('NO FLASH/WIPE/REBOOT COMMAND WAS STARTED', printed.getvalue())
            self.assertNotIn('[PLAN]', printed.getvalue())

    def test_read_only_check_rejects_wipe_and_execute_flags(self):
        for flag in ('--execute', '--wipe', '--reboot'):
            with self.assertRaisesRegex(ValueError, 'Read-only connection check'):
                flash.main(['--check-device', flag])

    def test_transport_failed_with_zero_exit_still_stops_before_mutation(self):
        for message in ('usb_read failed: error (31)',
                        'Status read failed (Too many links)',
                        'Write to device failed (Too many links)'):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                folder, executable = self.fixture(Path(tmp))
                responses = [flash.subprocess.CompletedProcess([], 0, 'test-serial\tfastboot\n', ''),
                             flash.subprocess.CompletedProcess([], 0, '', 'getvar:product FAILED (' + message + ')')]
                printed = io.StringIO()
                with patch('builtins.input', return_value='2'), \
                        patch.object(flash.subprocess, 'run', side_effect=responses) as process, \
                        redirect_stdout(printed), self.assertRaisesRegex(RuntimeError, 'USB TRANSPORT ERROR'):
                    flash.main(['--menu', '--rom', str(folder), '--fastboot', str(executable),
                                '--expected-product', 'target-bootloader',
                                '--expected-fastbootd-product', 'target-userspace'])
                self.assertEqual(process.call_count, 2)
                self.assertIn('NO FLASH/WIPE/REBOOT COMMAND WAS STARTED', printed.getvalue())


if __name__ == '__main__':
    unittest.main()
