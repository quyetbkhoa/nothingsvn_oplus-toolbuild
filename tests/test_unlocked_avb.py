import copy
import importlib.util
import json
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from colornt.avb import inspect_avb
from colornt.desktop_build import validate_options
from colornt.package_validation import validate_records
from colornt.unlocked_avb import POLICY, prepare_top_level
from test_flash_script import flash


def root_bytes(flags=3, rollback=5):
    h = bytearray(256)
    h[:4] = b'AVB0'
    struct.pack_into('>II', h, 4, 1, 0)
    struct.pack_into('>QII', h, 112, rollback, flags, 0)
    h[128:136] = b'fixture\0'
    return bytes(h) + bytes(4096 - 256)


class UnlockedAvbTests(unittest.TestCase):
    def fixture(self, root):
        top = root / 'vbmeta.img';top.write_bytes(root_bytes())
        meta = inspect_avb(top)
        policy = {'kind': POLICY, 'explicitly_requested': True, 'top_metadata_sha256': meta['metadata_sha256'],
                  'rollback_index': 5, 'rollback_location': 0, 'requires_unlocked': True,
                  'requires_allow_verification_error': True, 'avbtool_metadata_verification': 'pass-unsigned-NONE'}
        records = [{'partition': 'vbmeta', 'image': {'size': 4096}, 'avb': meta}]
        for name in ('vbmeta_system', 'vbmeta_vendor'):
            records.append({'partition': name, 'image': {'size': 4096},
                            'avb': {'status': 'metadata-inspected', 'flags': 0, 'header_aux_hash_matches': True, 'descriptors': []}})
        records.append({'partition': 'system_ext', 'image': {'size': 4096}, 'source_has_avb_footer': True,
                        'avb': {'status': 'no-avb-metadata'}})
        records[1]['avb']['descriptors'] = [{'kind': 'hashtree', 'partition': 'system_ext', 'image_size': 8192,
                                           'tree_offset': 8192, 'tree_size': 4096}]
        return top, records, policy

    def validate(self, records, policy):
        return validate_records(records, [r['partition'] for r in records], True,
                                [{'partition': 'system_ext', 'status': 'pass'}], ['system_ext'], policy)

    def test_explicit_policy_retains_inactive_errors_without_claiming_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, records, policy = self.fixture(Path(tmp))
            blocked = self.validate(records, None)
            self.assertEqual(blocked['status'], 'package-blocked')
            result = self.validate(records, policy)
            self.assertEqual(result['status'], 'experimental-package-ready')
            self.assertEqual(len(result['inactive_avb_issues']), 3)
            self.assertFalse(result['signature_verified']);self.assertFalse(result['device_verified'])
            self.assertFalse(result['partition_contents_verified'])
            self.assertTrue(result['runtime_conditions'])

    def test_wrong_root_or_child_flags_or_missing_partition_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, records, policy = self.fixture(Path(tmp))
            for field, value in [('flags', 0), ('canonical_unsigned_root', False), ('metadata_sha256', 'wrong')]:
                altered = copy.deepcopy(records);altered[0]['avb'][field] = value
                self.assertEqual(self.validate(altered, policy)['status'], 'package-blocked')
            altered = copy.deepcopy(records);altered[1]['avb']['flags'] = 3
            self.assertEqual(self.validate(altered, policy)['status'], 'package-blocked')
            altered = copy.deepcopy(records);altered[-1]['image']['format'] = 'invalid'
            self.assertEqual(self.validate(altered, policy)['status'], 'package-blocked')
            missing = validate_records(records[:-1], ['system_ext'], True, [], ['system_ext'], policy)
            self.assertEqual(missing['status'], 'package-blocked')

    def test_flash_gate_reads_actual_small_vbmeta_instead_of_only_a_ready_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            top, records, policy = self.fixture(Path(tmp))
            report = {'status': 'experimental-package-ready', 'package_validation': self.validate(records, policy), 'avb_policy': policy}
            flash.execution_gate(report, 'confirmed-target', {'vbmeta': top})
            data = bytearray(top.read_bytes());data[120:124] = bytes(4);top.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'Current vbmeta bytes'):
                flash.execution_gate(report, 'confirmed-target', {'vbmeta': top})
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                validate_options({'unlocked_avb': True, 'vbmeta_flags': True})

    def test_boot_failure_blocks_flash_even_if_offline_ready_fields_are_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            top, records, policy = self.fixture(Path(tmp))
            report = {'status': 'experimental-package-ready', 'package_validation': self.validate(records, policy),
                      'avb_policy': policy}
            for feedback in ({'device_test': {'boot': 'failed'}}, {'device_boot_failed': True},
                             {'package_validation': dict(report['package_validation'], device_boot_failed=True)}):
                with self.subTest(feedback=feedback), self.assertRaisesRegex(ValueError, 'recorded device boot failure'):
                    flash.execution_gate(dict(report, **feedback), 'confirmed-target', {'vbmeta': top})

    def test_prepare_refuses_failed_boot_before_creating_backups_or_changing_images(self):
        spec = importlib.util.spec_from_file_location('prepare_policy',
            Path(__file__).resolve().parents[1] / 'scripts/prepare_unlocked_output.py')
        prepare = importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);output = root / 'failed-rom';output.mkdir()
            top = output / 'vbmeta.img';top.write_bytes(root_bytes())
            sidecar = Path(str(output) + '.build.json')
            for feedback in ({'device_test': {'boot': 'failed'}}, {'device_boot_failed': True},
                             {'package_validation': {'device_boot_failed': True}}):
                with self.subTest(feedback=feedback):
                    sidecar.write_text(json.dumps(feedback))
                    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    with patch.object(prepare, 'prepare_top_level') as generator:
                        with self.assertRaisesRegex(ValueError, 'DEVICE BOOT FAILED'):
                            prepare.prepare(output)
                        generator.assert_not_called()
                    self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_failed_metadata_verification_keeps_original_vbmeta_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);top, _, _ = self.fixture(root);before = top.read_bytes()
            def native(command, stream):
                if 'make_vbmeta_image' in command:
                    Path(command[command.index('--output')+1]).write_bytes(root_bytes())
                else:
                    raise ValueError('verification failed')
            with patch('colornt.unlocked_avb.generator_tool', return_value=(Path('fake.py'), {'version': 'test', 'sha256': 'test'})), patch('colornt.unlocked_avb._run_native', side_effect=native):
                with self.assertRaisesRegex(ValueError, 'verification failed'):
                    prepare_top_level(top)
            self.assertEqual(top.read_bytes(), before)
            self.assertFalse(list(root.glob('.colornt-vbmeta-*')))


if __name__ == '__main__':
    unittest.main()
