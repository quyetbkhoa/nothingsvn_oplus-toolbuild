import copy
import os
from pathlib import Path
import tempfile
import unittest

from colornt.android_links import MAGIC
from colornt.io import read_json
from colornt.properties import Properties, identity_value, port_properties


class PropertyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a, self.b, self.stage = [self.root / x for x in ('a', 'b', 'stage')]
        for root in (self.a, self.b, self.stage):
            root.mkdir()
        (self.a / 'build.prop').write_bytes(b'model=TEST100\ndevice=ABC123\nandroid=16\nresolution=1200,2600\n')
        raw = b'# Preserve DONOR900 in this comment\r\nmodel=DONOR900\r\ndevice=XYZ789\r\nandroid=16\r\nname = DONOR900IN  \r\nfingerprint=OnePlus/DONOR900IN/XYZ789:16/BUILD/123:user/release-keys\r\nresolution=1400,3000\r\nversion=donor-987\r\n'
        (self.b / 'build.prop').write_bytes(raw)
        (self.stage / 'build.prop').write_bytes(raw)
        def ref(role, key):
            return dict(role=role, path='build.prop', key=key)
        self.policy = dict(schema=1, kind='port-properties', id='test',
                           conditions=[dict(source=ref(role, 'android'), equals='16') for role in ('base', 'donor')],
                           identity={role:{key:ref(role,key) for key in ('model','device')} for role in ('base','donor')},
                           files=[dict(path='build.prop', baseline='donor', operations=[
                               dict(kind='identity', key='name', form='product-name', occurrences=1),
                               dict(kind='identity', key='fingerprint', form='fingerprint', occurrences=1),
                               dict(kind='copy', key='resolution', source=ref('base','resolution'), occurrences=1)])])
        self.policy['identity']['product_suffixes'] = ['IN']

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, **kwargs):
        return port_properties(self.stage, self.a, self.b, self.policy, **kwargs)

    def test_reusable_other_model_and_hardware_values_preserve_donor_tail(self):
        original_a = (self.a / 'build.prop').read_bytes()
        original_b = (self.b / 'build.prop').read_bytes()
        target = self.stage / 'build.prop'
        os.utime(target, ns=(1700000000000000000, 1700000000000000000))
        self.assertEqual(self.apply()['status'], 'applied')
        self.assertIn(b'name = TEST100IN  \r\n', target.read_bytes())
        self.assertIn(b'OnePlus/TEST100IN/ABC123:16/BUILD/123:user/release-keys', target.read_bytes())
        self.assertIn(b'resolution=1200,2600\r\nversion=donor-987', target.read_bytes())
        self.assertIn(b'# Preserve DONOR900 in this comment', target.read_bytes())
        self.assertEqual(target.stat().st_mtime_ns, 1700000000000000000)
        self.assertEqual((self.a / 'build.prop').read_bytes(), original_a)
        self.assertEqual((self.b / 'build.prop').read_bytes(), original_b)

    def test_dry_run_does_not_mutate(self):
        before = (self.stage / 'build.prop').read_bytes()
        result = self.apply(dry_run=True)
        self.assertEqual(result['status'], 'planned')
        self.assertNotEqual(result['files'][0]['before_sha256'], result['files'][0]['after_sha256'])
        self.assertEqual((self.stage / 'build.prop').read_bytes(), before)

    def test_qualified_reconstruction_accepts_only_exact_applied_values(self):
        self.apply()
        before = (self.stage / 'build.prop').read_bytes()
        with self.assertRaisesRegex(ValueError, 'baseline/occurrence mismatch'):
            self.apply()
        self.apply(allow_applied=True)
        self.assertEqual((self.stage / 'build.prop').read_bytes(), before)
        (self.stage / 'build.prop').write_bytes(before.replace(b'resolution=1200,2600', b'resolution=1234,5678'))
        changed = (self.stage / 'build.prop').read_bytes()
        with self.assertRaisesRegex(ValueError, 'baseline/occurrence mismatch'):
            self.apply(allow_applied=True)
        self.assertEqual((self.stage / 'build.prop').read_bytes(), changed)

    def test_discovery_derives_real_counts_for_different_models(self):
        from colornt.discovery import derive_policy
        for root in (self.b, self.stage):
            with (root / 'build.prop').open('ab') as stream:
                stream.write(b'resolution=1500,3200\r\n')
        policy = derive_policy(self.a, self.b, self.policy)
        self.assertEqual(policy['files'][0]['operations'][-1]['occurrences'], 2)
        port_properties(self.stage, self.a, self.b, policy)
        self.assertEqual(Properties((self.stage / 'build.prop').read_bytes()).values('resolution'), ['1200,2600'] * 2)
        self.assertIn(b'TEST100IN', (self.stage / 'build.prop').read_bytes())
        self.assertIn(b'version=donor-987', (self.stage / 'build.prop').read_bytes())

    def test_discovery_rejects_missing_hardware_contract(self):
        from colornt.discovery import derive_policy
        (self.a / 'build.prop').write_bytes((self.a / 'build.prop').read_bytes().replace(b'resolution=1200,2600\n', b''))
        with self.assertRaisesRegex(ValueError, 'required property contract'):
            derive_policy(self.a, self.b, self.policy)

    def test_late_bad_source_aborts_whole_batch(self):
        before = (self.stage / 'build.prop').read_bytes()
        self.policy['files'].append(dict(path='second.prop', baseline='donor', operations=[
            dict(kind='copy', key='other', source=dict(role='base',path='build.prop',key='missing'), occurrences=1)]))
        for root in (self.b,self.stage):
            (root/'second.prop').write_bytes(b'other=keep\n')
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.apply()
        self.assertEqual((self.stage / 'build.prop').read_bytes(), before)
        self.assertEqual((self.stage / 'second.prop').read_bytes(), b'other=keep\n')

    def test_unknown_region_and_false_prefix_are_rejected(self):
        for value, form in [('DONOR900EU','product-name'), ('DONOR9000_16.0','model-prefix')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                identity_value(value,form,dict(model='TEST100',device='ABC123'),dict(model='DONOR900',device='XYZ789'),['IN'])

    def test_duplicate_occurrences_are_explicit(self):
        for root in (self.b, self.stage):
            with (root/'build.prop').open('ab') as stream:
                stream.write(b'resolution=1500,3200\r\n')
        with self.assertRaisesRegex(ValueError,'occurrence'):
            self.apply()
        self.policy['files'][0]['operations'][-1]['occurrences'] = 2
        self.apply()
        self.assertEqual(Properties((self.stage/'build.prop').read_bytes()).values('resolution'),['1200,2600']*2)

    def test_absent_copy_adds_property_without_losing_last_line(self):
        for root in (self.b,self.stage):
            (root/'build.prop').write_bytes((root/'build.prop').read_bytes().replace(b'resolution=1400,3000\r\n',b'').rstrip(b'\r\n'))
        self.policy['files'][0]['operations'][-1]['occurrences'] = 0
        self.apply()
        self.assertIn(b'version=donor-987\r\nresolution=1200,2600\r\n',(self.stage/'build.prop').read_bytes())

    def test_import_directives_are_preserved_without_host_resolution(self):
        directive=b'import /odm/etc/${ro.boot.prjname}/build.prop\r\n'
        for root in (self.b,self.stage):
            with (root/'build.prop').open('ab') as stream:
                stream.write(directive)
        self.apply()
        self.assertTrue((self.stage/'build.prop').read_bytes().endswith(directive))

    def test_mismatched_stage_condition_and_unknown_operation_fail(self):
        before=(self.stage/'build.prop').read_bytes()
        policies=[]
        bad=copy.deepcopy(self.policy)
        bad['conditions'][0]['equals']='17'
        policies.append(bad)
        bad=copy.deepcopy(self.policy)
        bad['files'][0]['operations'][-1]['kind']='magic'
        policies.append(bad)
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                port_properties(self.stage,self.a,self.b,policy)
        (self.stage/'build.prop').write_bytes(before.replace(b'resolution=1400,3000',b'resolution=already-changed'))
        with self.assertRaisesRegex(ValueError,'baseline'):
            self.apply()

    def test_reject_input_overlap_traversal_and_android_links(self):
        with self.assertRaisesRegex(ValueError,'overlaps'):
            port_properties(self.a,self.a,self.b,self.policy)
        self.policy['files'][0]['path']='../escape'
        with self.assertRaisesRegex(ValueError,'relative path'):
            self.apply()
        self.policy['files'][0]['path']='build.prop'
        (self.stage/'build.prop').write_bytes(MAGIC+('/protected\0').encode('utf-16-le'))
        with self.assertRaisesRegex(ValueError,'Android link'):
            self.apply()

    def test_staged_hardlink_cannot_modify_input(self):
        target=self.stage/'build.prop'
        before=(self.b/'build.prop').read_bytes()
        target.unlink()
        os.link(self.b/'build.prop',target)
        with self.assertRaisesRegex(ValueError,'hardlink'):
            self.apply()
        self.assertEqual((self.b/'build.prop').read_bytes(),before)

    def test_policy_covers_every_observed_manifest_changed_key(self):
        repo=Path(__file__).resolve().parents[1]
        details=read_json(repo/'reports/PATCH-DETAILS.json')
        manifest=next(x for x in details['modified'] if x['path']=='my_manifest/build.prop')
        policy=read_json(repo/'recipes/oplus-a16-in-core-properties.json')
        observed={x['key'] for x in manifest['comparisons']['B']['properties']}
        declared={x['key'] for x in policy['files'][0]['operations']}
        self.assertEqual(declared,observed)


if __name__ == '__main__':
    unittest.main()
