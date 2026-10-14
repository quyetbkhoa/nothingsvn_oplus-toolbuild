from pathlib import Path
import tempfile
import unittest
import struct
from colornt.package_validation import validate_records, validate_folder
from colornt.desktop import package_result_message
from colornt.io import write_json
from test_avb import vbmeta


def record(name, size=4096, **avb):
    return {'partition': name, 'image': {'format': 'avb-vbmeta' if name.startswith('vbmeta') else 'erofs', 'size': size},
            'avb': dict(status='metadata-inspected', flags=0, header_aux_hash_matches=True, descriptors=[], **avb)}


class PackageValidationTests(unittest.TestCase):
    def codes(self, result):
        return {i['code'] for i in result['blockers']}

    def test_size_footer_and_hashtree_span_mismatches_block_package(self):
        owner, target = record('vbmeta'), record('system', 4096)
        owner['avb']['descriptors'] = [{'kind': 'hashtree', 'partition': 'system', 'image_size': 8192,
                                       'tree_offset': 8192, 'tree_size': 4096}]
        target['source_has_avb_footer'] = True
        result = validate_records([owner, target], ['vbmeta', 'system'], True)
        self.assertTrue({'descriptor-size-mismatch', 'required-footer-missing', 'hashtree-or-fec-span-invalid'} <= self.codes(result))
        self.assertEqual(result['status'], 'package-blocked')
        self.assertFalse(result['experimental_package_ready'])

    def test_chain_key_flags_cycle_missing_target_and_metadata_are_detected(self):
        root, child = record('vbmeta'), record('vbmeta_system')
        root['avb']['descriptors'] = [{'kind': 'chain', 'partition': 'vbmeta_system', 'rollback_location': 1, 'public_key_sha256': 'expected'},
                                    {'kind': 'hash', 'partition': 'absent', 'image_size': 4096}]
        child['avb'].update(flags=3, public_key_sha256='other', header_aux_hash_matches=False,
                            descriptors=[{'kind': 'chain', 'partition': 'vbmeta', 'rollback_location': 2}])
        result = validate_records([root, child], ['vbmeta', 'vbmeta_system', 'boot'], False)
        self.assertTrue({'chain-public-key-mismatch', 'nonzero-chained-vbmeta-flags', 'avb-chain-cycle',
                         'descriptor-target-missing', 'missing-required-partition', 'missing-or-invalid-firmware-metadata',
                         'vbmeta-header-aux-hash-mismatch'} <= self.codes(result))
        self.assertFalse(result['assembled'])
        self.assertNotIn('assembled', result['states'])

    def test_structurally_valid_metadata_never_claims_signature_or_device_completion(self):
        result = validate_records([record('vbmeta'), record('system')], ['vbmeta', 'system'], True,
                                  [{'partition': 'system', 'status': 'pass'}], ['system'])
        self.assertTrue(result['structural_checks_passed'])
        self.assertEqual(result['states'], ['assembled', 'filesystem-verified', 'package-blocked'])
        self.assertFalse(result['signature_verified'])
        self.assertFalse(result['device_verified'])
        self.assertIn('signing-or-unlocked-test-policy-unresolved', self.codes(result))
        failed = validate_records([record('system')], ['system'], True, [], ['system'])
        self.assertFalse(failed['filesystem_verified'])
        self.assertNotIn('đã kiểm chứng', package_result_message(failed))
        self.assertNotIn('đã kiểm chứng', package_result_message(dict(failed, assembled=False)))

    def test_real_bounded_folder_inspection_detects_truncated_avb_and_missing_a_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, output = root / 'a', root / 'output'
            a.mkdir(); output.mkdir()
            for p in (a, output):
                write_json(p / 'firmware_info.json', {'product_name': 'A', 'version_name': 'v1'})
                (p / 'vbmeta.img').write_bytes(vbmeta())
            (a / 'boot.img').write_bytes(b'ANDROID!' + bytes(56))
            (output / 'vbmeta.img').write_bytes(b'AVB0' + bytes(128))
            result = validate_folder(output, a)
            self.assertTrue({'invalid-avb-metadata', 'missing-required-partition'} <= self.codes(result))
            self.assertFalse(result['assembled'])


if __name__ == '__main__':
    unittest.main()
