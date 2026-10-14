from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from colornt import reference_qa
from colornt.io import sha256, write_json


class ReferenceQATests(unittest.TestCase):
    def test_prior_checks_reuse_only_for_exact_identity_and_bytes_and_keep_known_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / 'stage'; stage.mkdir()
            for name in ('policy.cil', 'framework.apk', 'factory.apk'):
                (stage / name).write_bytes(name.encode())
            evidence = {'candidate_id': 'prior', 'manifest_sha256': 'reference-manifest',
                'identities': {'base': {'product_name': 'A'}, 'donor': {'product_name': 'B'}},
                'selinux': {'inputs': [{'path': 'policy.cil', 'sha256': sha256(stage / 'policy.cil')}],
                            'policy_version': 33, 'compiled_policy_sha256': 'policy',
                            'compiler_sha256': 'compiler', 'libsepol_sha256': 'library'},
                'apk_checks': [{'path': name, 'sha256': sha256(stage / name), 'signature_verified': verified}
                               for name, verified in [('framework.apk', True), ('factory.apk', False)]],
                'apk_verifier': {'name': 'recorded verifier'}}
            write_json(root / 'profiles/evidence/pjf110-cph2745-booted-20261014.json', evidence)
            with patch.object(reference_qa, 'ROOT', root):
                result = reference_qa.staged_reference_checks(stage, {'product_name': 'A'}, {'product_name': 'B'})
                self.assertEqual(result['host_cil_compile']['status'], 'REUSED-PASS-EXACT-INPUTS')
                self.assertEqual(result['apk_cryptographic_signatures']['status'], 'REUSED-EXACT-FILE-RESULTS')
                self.assertFalse(result['apk_cryptographic_signatures']['checks'][1]['signature_verified'])
                self.assertEqual(result['device_boot'], 'NOT_TESTED')
                (stage / 'policy.cil').write_bytes(b'changed')
                (stage / 'framework.apk').write_bytes(b'changed')
                result = reference_qa.staged_reference_checks(stage, {'product_name': 'A'}, {'product_name': 'B'})
                self.assertEqual(result['host_cil_compile']['status'], 'NOT_RUN')
                self.assertEqual(result['apk_cryptographic_signatures']['status'], 'NOT_RUN')
                result = reference_qa.staged_reference_checks(stage, {'product_name': 'wrong'}, {'product_name': 'B'})
                self.assertNotIn('reference', result)
