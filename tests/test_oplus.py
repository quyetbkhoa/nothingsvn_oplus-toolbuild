import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt.io import read_json
from colornt.oplus import analyze_oplus_pair, identify_oplus
from colornt.tools import ROOT


class OplusRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, role, manufacturer, token, android='16', region='IN'):
        fs = self.root / role
        (fs / 'my_manifest').mkdir(parents=True)
        (fs / 'system/system').mkdir(parents=True)
        (fs / 'my_manifest/build.prop').write_text(
            f'ro.product.manufacturer={manufacturer}\nro.product.model={role.upper()}100\n'
            f'ro.product.device={role.upper()}DEV\nro.oplus.pipeline_key={region}\n', encoding='utf-8')
        (fs / 'system/system/build.prop').write_text(f'ro.build.version.release={android}\n', encoding='utf-8')
        if token:
            target = fs / 'my_stock/etc/extension/com.oplus.app-features.xml'
            target.parent.mkdir(parents=True)
            target.write_text('<features><app_feature name="com.oplus.ota.brand" '
                              f'args="String:{token}"/></features>', encoding='utf-8')
        return fs

    def test_all_nine_brand_directions_recognized_without_granting_build_support(self):
        brands = [('OPPO', 'oppo'), ('realme', 'realme'), ('OnePlus', 'oneplus-exp')]
        for index, (a, b) in enumerate(itertools.product(brands, repeat=2)):
            with self.subTest(a=a, b=b):
                report = analyze_oplus_pair(self.fixture(f'a{index}', *a), self.fixture(f'b{index}', *b))
                self.assertTrue(report['oplus_family_recognized'])
                self.assertFalse(report['device_validated'])
                self.assertFalse(report['flash_ready'])
                self.assertEqual(report['cross_brand'], a != b)
                self.assertEqual(bool(report['blockers']), a[0] != 'OnePlus' or b[0] != 'OnePlus')

    def test_missing_os_brand_is_reported_unknown_and_region_or_version_blocks_rules(self):
        a = self.fixture('a', 'OnePlus', None, android='15')
        b = self.fixture('b', 'OnePlus', None, region='EU')
        report = analyze_oplus_pair(a, b)
        self.assertIsNone(report['base']['os_name'])
        self.assertEqual(report['status'], 'rules-required')
        self.assertEqual(len(report['blockers']), 2)

    def test_feature_brand_can_differ_from_factory_manufacturer(self):
        fs = self.fixture('a', 'OPPO', 'realme')
        identity = identify_oplus(fs)
        self.assertEqual(identity['manufacturer'], 'OPPO')
        self.assertEqual(identity['os_brand'], 'Realme')
        self.assertEqual(identity['os_name'], 'RealmeUI')

    def test_rejects_unknown_manufacturer_and_conflicting_brand_feature(self):
        fs = self.fixture('a', 'Other', 'oppo')
        with self.assertRaisesRegex(ValueError, 'manufacturer'):
            identify_oplus(fs)
        fs = self.fixture('b', 'OPPO', 'oppo')
        xml = fs / 'my_stock/etc/extension/com.oplus.app-features.xml'
        xml.write_text('<features><app_feature name="com.oplus.ota.brand" args="String:oppo"/>'
                       '<app_feature name="com.oplus.ota.brand" args="String:realme"/></features>', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            identify_oplus(fs)

    def test_brand_xml_rejects_entity_declarations_in_utf16(self):
        fs = self.fixture('a', 'OPPO', 'oppo')
        target = fs / 'my_stock/etc/extension/com.oplus.app-features.xml'
        target.write_bytes(('<?xml version="1.0" encoding="UTF-16"?>'
                            '<!DOCTYPE features [<!ENTITY brand "String:oppo">]>'
                            '<features><app_feature name="com.oplus.ota.brand" args="&brand;"/></features>').encode('utf-16'))
        with self.assertRaisesRegex(ValueError, 'declarations'):
            identify_oplus(fs)

    def test_auto_adapter_selection_does_not_require_manual_file_or_allow_unknown_pair(self):
        from colornt.desktop_build import inspect_request
        adapter = read_json(ROOT / 'profiles/desktop-pjf110-cph2745.json')
        a = {'identity': adapter['identities']['base'], 'images': {}, 'kind': 'fff-folder'}
        b = {'identity': adapter['identities']['donor'], 'images': {}, 'kind': 'fff-folder'}
        for name in ('a', 'b'):
            (self.root / name).mkdir()
        request = {'base': str(self.root / 'a'), 'donor': str(self.root / 'b'),
                   'output': str(self.root / 'output'), 'work_root': str(self.root / 'work')}
        with patch('colornt.desktop_build.source_summary', side_effect=[a, b]), patch('colornt.desktop_build.executable'):
            plan = inspect_request(request)
        self.assertEqual(plan['adapter'], adapter['id'])
        self.assertTrue(plan['adapter_path'].endswith('desktop-pjf110-cph2745.json'))
        self.assertNotIn('adapter', request)
        b = dict(b, identity=dict(b['identity'], product_name='RMXTEST'))
        with patch('colornt.desktop_build.source_summary', side_effect=[a, b]):
            with self.assertRaisesRegex(ValueError, 'Automatic port rules'):
                inspect_request(request)


if __name__ == '__main__':
    unittest.main()
