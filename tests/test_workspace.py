from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from colornt.desktop import App
from colornt.io import write_json
from colornt.workspace_prepare import prepare_owned_roots, probe_workspace, prepare_case_flag


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_refuses_protected_and_nonempty_unowned_roots(self):
        protected = self.root / 'input'
        protected.mkdir()
        data = protected / 'original'
        data.write_bytes(b'input')
        unowned = self.root / 'unowned'
        unowned.mkdir()
        (unowned / 'other').write_bytes(b'other')
        with patch('colornt.workspace_prepare.probe_workspace') as probe:
            result = prepare_owned_roots([protected, unowned], self.root, [protected])
        self.assertEqual(result['status'], 'failed')
        probe.assert_not_called()
        self.assertEqual(data.read_bytes(), b'input')
        self.assertTrue((unowned / 'other').exists())

    def test_probe_returns_api_stage_error_and_cleans_its_owned_directory(self):
        def fail(path, diagnostic):
            diagnostic.update(stage='SetCaseSensitiveInfo', environment={'token': {'elevated': False}, 'filesystem': 'NTFS'})
            raise PermissionError(5, 'denied')
        with patch('colornt.workspace_prepare.prepare_case_flag', side_effect=fail):
            result = probe_workspace(self.root)
        self.assertEqual(result['stage'], 'SetCaseSensitiveInfo')
        self.assertEqual(result['error_code'], 5)
        self.assertEqual(result['case_checks'], [])
        self.assertFalse(list(self.root.iterdir()))

    def test_helper_success_and_failure_contracts_are_separate(self):
        roots = [self.root / 'w', self.root / 'h']
        failure = {'status': 'failed', 'stage': 'SetCaseSensitiveInfo', 'error': 'denied', 'error_code': 5}
        with patch('colornt.workspace_prepare.probe_workspace', side_effect=[failure, {'status': 'success'}]), patch('colornt.workspace_fs.enable_case_sensitive'):
            result = prepare_owned_roots(roots, self.root, [])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['roots'][1]['status'], 'success')
        self.assertFalse((roots[0] / 'colornt-root.json').exists())
        self.assertTrue((roots[1] / 'colornt-root.json').exists())

    def test_scoped_permission_is_restored_when_case_set_fails(self):
        events = []
        @contextmanager
        def permission(path, diagnostic):
            events.append('grant')
            try:
                yield
            finally:
                events.append('restore')
        def fail(path, diagnostic):
            diagnostic['stage'] = 'SetCaseSensitiveInfo'
            raise PermissionError(5, 'denied')
        with patch('colornt.workspace_fs.enable_case_sensitive', side_effect=fail), patch('colornt.workspace_acl.owner_delete_child', side_effect=permission):
            with self.assertRaises(PermissionError):
                prepare_case_flag(self.root, {})
        self.assertEqual(events, ['grant', 'restore'])

    def test_ui_automatically_consumes_matching_helper_failure_and_exit_without_result(self):
        app = App.__new__(App)
        app.status, app.workspace_button, app.set_idle = Mock(), Mock(), Mock()
        app.logs = []
        result = self.root / 'result.json'
        app.workspace_preparation = {'handle': 1, 'result': result, 'nonce': 'current'}
        write_json(result, {'status': 'failed', 'nonce': 'current', 'roots': [{'status': 'failed', 'stage': 'SetCaseSensitiveInfo', 'error_code': 5}]})
        with patch('colornt.workspace_prepare.preparation_exited'):
            app.poll_workspace_preparation()
        app.set_idle.assert_called_once()
        self.assertIn('error 5', app.status.set.call_args.args[0])
        self.assertIsNone(app.workspace_preparation)
        result.unlink()
        app.workspace_preparation = {'handle': 2, 'result': result, 'nonce': 'next'}
        with patch('colornt.workspace_prepare.preparation_exited', return_value=True):
            app.poll_workspace_preparation()
        self.assertIn('without a result', app.status.set.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
