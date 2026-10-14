import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from colornt import jobs, portable, policy_runtime


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = {'files': [{'source': 'assets', 'source_path': 'files/mod', 'size': 5,
                               'sha256': hashlib.sha256(b'valid').hexdigest()}],
                     'metadata': [], 'images': [], 'asset_bytes': 5}

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, entries):
        path = self.root / 'data.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return path

    def run_import(self, path, checked):
        with patch.object(portable, 'ROOT', self.root), patch('colornt.v14.recipe', return_value=self.data), \
             patch('colornt.reconstruct.verify_assets', return_value=checked), \
             patch('colornt.workspace_prepare.prepare_case_flag'):
            return portable.import_v14_pack(path)

    def test_pack_member_inventory_and_sizes_checked_before_extraction(self):
        for entries in ([('files/mod', b'valid'), ('../escaped', b'x')], [('files/mod', b'wrong-size')], []):
            path = self.pack(entries)
            with self.assertRaisesRegex(ValueError, 'unexpected|byte total'):
                self.run_import(path, {'status': 'pass'})
            self.assertFalse((self.root / 'assets').exists())
            self.assertFalse((self.root.parent / 'escaped').exists())

    def test_failed_digest_verification_removes_only_fresh_import(self):
        protected = self.root / 'keep'; protected.write_bytes(b'preserved')
        path = self.pack([('files/mod', b'valid')])
        with self.assertRaisesRegex(ValueError, 'content/link/metadata'):
            self.run_import(path, {'status': 'fail', 'issues': ['tampered']})
        self.assertFalse((self.root / 'assets/local/pjf110-cph2745').exists())
        self.assertEqual(list((self.root / 'assets/local').iterdir()), [])
        self.assertEqual(protected.read_bytes(), b'preserved')

    def test_validated_store_is_installed_and_never_overwritten(self):
        path = self.pack([('files/mod', b'valid')])
        self.assertEqual(self.run_import(path, {'status': 'pass'})['status'], 'installed')
        target = self.root / 'assets/local/pjf110-cph2745/files/mod'
        self.assertEqual(target.read_bytes(), b'valid')
        with self.assertRaisesRegex(ValueError, 'preserve it'):
            self.run_import(path, {'status': 'fail'})
        self.assertEqual(target.read_bytes(), b'valid')

    def test_default_work_roots_follow_install_and_output_without_h_drive(self):
        with patch.object(jobs, 'ROOT', self.root), patch.object(jobs, 'local_config', return_value={}):
            job = jobs.BuildJob({'output_parent': str(self.root / 'chosen-output')})
        self.assertEqual(job.project, self.root.resolve())
        self.assertEqual(job.fallback, (self.root / 'chosen-output/.colornt-work').absolute())
        self.assertEqual(job.work_base, self.root / 'work')

    def test_policy_missing_or_changed_is_fatal_before_any_process(self):
        with patch.object(policy_runtime, 'ROOT', self.root), patch('subprocess.run') as process:
            with self.assertRaisesRegex(ValueError, 'unavailable/changed'):
                policy_runtime.setup()
            target = self.root / 'tools/local/policy-runtime/secilc-static'
            target.parent.mkdir(parents=True); target.write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'unavailable/changed'):
                policy_runtime.setup()
            process.assert_not_called()
