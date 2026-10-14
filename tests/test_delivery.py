import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from colornt import delivery
from colornt.io import read_json, write_json


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = patch.object(delivery, 'ROOT', self.root); self.context.start()
        (self.root / 'colornt').mkdir()
        (self.root / 'colornt/source.py').write_text('# pinned source\n')
        (self.root / 'props.json').write_text('{}')
        self.adapter = self.root / 'adapter.json'
        write_json(self.adapter, {'hardware_files': [], 'property_policy': 'props.json'})
        self.request = {}
        for role in ('base', 'donor'):
            folder = self.root / role; folder.mkdir()
            (folder / 'system.img').write_bytes(b'fixture IMG')
            write_json(folder / 'firmware_info.json', {'product_name': role, 'version_name': role})
            self.request[role] = str(folder)
            (self.root / (role + '-cache/fs/system')).mkdir(parents=True)
            (self.root / (role + '-cache/fs/system/payload')).write_text(role)
        self.output = self.root / 'new-candidate'; self.output.mkdir()
        (self.output / 'system.img').write_bytes(b'new IMG fixture')
        write_json(self.output / 'firmware_info.json', {'product_name': 'base', 'version_name': 'donor'})
        self.stage = self.root / 'stage'
        (self.stage / 'system').mkdir(parents=True)
        (self.stage / 'system/payload').write_text('donor')
        self.report = {'output': str(self.output), 'base': {'path': self.request['base'], 'identity': {'product_name': 'base'}},
                       'donor': {'identity': {'product_name': 'donor'}}, 'status': 'experimental-package-ready',
                       'roundtrip': [{'partition': 'system', 'status': 'pass'}], 'adapter_path': str(self.adapter),
                       'rule_state': delivery.rule_state(self.adapter),
                       'base_extracted': str(self.root / 'base-cache'), 'donor_extracted': str(self.root / 'donor-cache'),
                       'images': [{'partition': 'system', 'path': 'system.img', 'image': {'size': 15}}],
                       'mods': {}, 'apks': [], 'replacements': {}, 'budget': {'used': 15, 'capacity': 1024},
                       'device_test': {'boot': 'USER_CONFIRMED_PASS'}}
        self.inputs, self.sources = delivery.input_state(self.request), delivery.source_index()

    def tearDown(self):
        self.context.stop(); self.temp.cleanup()

    def export(self):
        with patch('colornt.package_validation.validate_folder', return_value={'status': 'experimental-package-ready'}):
            return delivery.export_candidate(self.report, self.request, self.stage, self.inputs, self.sources)

    def test_export_freezes_metadata_source_and_checker_detects_payload_or_metadata_changes(self):
        record = self.export()
        manifest = read_json(self.output / 'BUILD-MANIFEST.json')
        self.assertEqual(record['state'], 'AWAITING_USER')
        self.assertEqual(manifest['device_test'], {'boot': 'NOT_TESTED', 'hardware': 'NOT_TESTED'})
        self.assertFalse(manifest['production_signed'])
        self.assertEqual((self.output / 'BUILD-SOURCE/colornt/source.py').read_bytes(),
                         (self.root / 'colornt/source.py').read_bytes())
        self.assertIn('system/payload,file,donor', (self.output / 'SOURCE-MAP.csv').read_text())
        result = subprocess.run([sys.executable, str(self.output / 'Check-Candidate.py')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('boot/hardware NOT_TESTED', result.stdout)
        (self.output / 'system.img').write_bytes(b'bad IMG fixture')
        result = subprocess.run([sys.executable, str(self.output / 'Check-Candidate.py')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn('system.img', result.stderr)
        (self.output / 'system.img').write_bytes(b'new IMG fixture')
        (self.output / 'BUILD-SOURCE/colornt/source.py').write_text('# changed source\n')
        result = subprocess.run([sys.executable, str(self.output / 'Check-Candidate.py')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn('source.py', result.stderr)

    def test_new_ready_boolean_cannot_bypass_real_independent_image_gate(self):
        with self.assertRaisesRegex(ValueError, 'Independent delivery package gate'):
            delivery.export_candidate(self.report, self.request, self.stage, self.inputs, self.sources)
        self.assertFalse((self.output / 'BUILD-MANIFEST.json').exists())

    def test_changed_input_source_or_rules_block_freeze_before_artifacts(self):
        for target, message in [(self.root / 'base/system.img', 'Protected A/B'),
                                (self.root / 'colornt/source.py', 'Build source/tools'),
                                (self.root / 'props.json', 'Port rules')]:
            before = target.read_bytes(); stat = target.stat()
            target.write_bytes(before + b'changed')
            with self.assertRaisesRegex(ValueError, message): self.export()
            self.assertFalse((self.output / 'BUILD-MANIFEST.json').exists())
            target.write_bytes(before); os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_inventory_size_change_and_duplicate_delivery_are_rejected(self):
        self.report['images'][0]['image']['size'] = 99
        with self.assertRaisesRegex(ValueError, 'path/size'): self.export()
        self.report['images'][0]['image']['size'] = 15
        self.export()
        before = (self.output / 'BUILD-MANIFEST.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'): self.export()
        self.assertEqual((self.output / 'BUILD-MANIFEST.json').read_bytes(), before)

    def test_output_hashing_honors_cancellation(self):
        with self.assertRaisesRegex(ValueError, 'Cancelled'):
            delivery.image_digest(self.output / 'system.img', lambda: True)

    def test_delivery_requires_actual_folder_spelling_for_added_images(self):
        (self.output / 'SYSTEM').mkdir()
        (self.output / 'system.img').rename(self.output / 'SYSTEM/system.img')
        self.report['images'][0]['path'] = 'system/system.img'
        with self.assertRaisesRegex(ValueError, 'path/size'):
            self.export()
        self.report['images'][0]['path'] = 'SYSTEM/system.img'
        self.assertEqual(self.export()['state'], 'AWAITING_USER')
