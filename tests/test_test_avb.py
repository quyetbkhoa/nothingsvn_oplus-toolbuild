from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt.test_avb import add_footer, footer_size
from colornt.desktop_build import inspect_request, normalize_installation_request
from colornt.jobs import required_parts


class FreshTestAvbTests(unittest.TestCase):
    def test_gui_cli_switch_has_same_canonical_request_without_mutating_selection(self):
        selection = {'mods': {'unlocked_avb': True, 'apps': False}}
        request = normalize_installation_request(selection)
        self.assertEqual(request['installation_policy'], 'unlocked-rebuilt-metadata-v1')
        self.assertFalse(any(request['mods'].values()))
        self.assertTrue(selection['mods']['unlocked_avb'])
        self.assertEqual(normalize_installation_request(request), request)

    def test_fresh_avb_cache_scope_includes_vendor_and_os_but_not_donor_only_parts(self):
        adapter = {'hardware_files': ['my_product/etc/config', 'system_ext/apex/vndk.apex'],
                   'dynamic_partitions': ['system', 'system_ext', 'vendor', 'odm', 'product', 'my_region']}
        summary = {'images': {k: {} for k in ['system', 'system_ext', 'vendor', 'odm', 'product', 'my_region', 'my_preload']}}
        a = required_parts(adapter, summary, 'base', fresh_avb=True)
        b = required_parts(adapter, summary, 'donor', fresh_avb=True)
        self.assertIn('vendor', a)
        self.assertIn('product', b)
        self.assertIn('my_region', b)
        self.assertNotIn('vendor', b)
        self.assertNotIn('my_preload', b)

    def test_conservative_footer_budget_covers_tree_and_metadata(self):
        for size in (4096, 1024 * 1024, 10 * 1024**3):
            budget = footer_size(size)
            self.assertEqual(budget % 4096, 0)
            self.assertGreaterEqual(budget - size, (size + 127) // 128 + 69632)
        for size in (0, -4096, 4097):
            with self.assertRaises(ValueError):
                footer_size(size)

    def test_footer_rejects_old_avb_and_non_erofs_before_native(self):
        with patch('colornt.test_avb.inspect_image', return_value={'format': 'erofs', 'avb_footer': {'major': 1}}), \
             patch('colornt.test_avb.native') as tool:
            with self.assertRaisesRegex(ValueError, 'newly repacked'):
                add_footer(Path('source.img'), 'system', Path('log'))
            tool.assert_not_called()
        with patch('colornt.test_avb.inspect_image', return_value={'format': 'ext'}):
            with self.assertRaises(ValueError):
                add_footer(Path('source.img'), 'system', Path('log'))

    def test_unknown_or_conflicting_install_policy_rejected_before_input_reads(self):
        for extra in [{'installation_policy': 'unknown'},
                      {'installation_policy': 'unlocked-rebuilt-metadata-v1', 'mods': {'unlocked_avb': True}},
                      {'installation_policy': 'unlocked-rebuilt-metadata-v1', 'mods': {'vbmeta_flags': True}}]:
            with patch('colornt.desktop_build.source_summary') as source:
                with self.assertRaises(ValueError):
                    inspect_request(dict(base='a', donor='b', output='c', **extra))
                source.assert_not_called()
