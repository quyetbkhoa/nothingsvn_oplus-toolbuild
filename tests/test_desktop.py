from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from colornt.desktop_build import (MODS, android_walk, apply_file_mods, check_identity, identity_from_fs, image_map, inspect_apks,
                                  inspect_request, prepare_source, read_debloat,
                                  replacement_map, safe_zip_name, source_summary,
                                  validate_animation, validate_options, verify_roundtrip)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def folder(self, name):
        folder = self.root / name
        (folder / 'SYSTEM').mkdir(parents=True)
        (folder / 'SYSTEM/system.img').write_bytes(b'unknown test container')
        (folder / 'firmware_info.json').write_text(json.dumps({'product_name': name}))
        return folder

    def test_folder_inventory_no_digest(self):
        folder = self.folder('A')
        report = source_summary(folder)
        self.assertEqual(report['images']['system']['path'], 'SYSTEM/system.img')
        self.assertNotIn('sha256', str(report))
        self.assertEqual(prepare_source(folder, self.root / 'unused')[0], folder)
        self.assertFalse((self.root / 'unused').exists())

    def test_android_walk_keeps_case_distinct_paths(self):
        with patch('colornt.desktop_build.os.walk', return_value=[(str(self.root), ['ES-Vodafone', 'es-vodafone'], [])]):
            self.assertEqual([p.name for p in android_walk(self.root)], ['ES-Vodafone', 'es-vodafone'])

    def test_file_mod_batch_preflight_does_not_apply_first_animation_on_late_failure(self):
        stage = self.root / 'stage'
        (stage / 'my_product').mkdir(parents=True)
        target = stage / 'my_product/boot.zip'
        target.write_bytes(b'original')
        animation = self.root / 'new.zip'
        with zipfile.ZipFile(animation, 'w') as z:
            z.writestr('desc.txt', '1080 2400 60\np 0 0 part0\n')
            z.writestr('part0/f.png', b'fixture')
        request = {'mods': {'boot_animation': True, 'reboot_animation': True},
                   'boot_animation': str(animation), 'reboot_animation': str(animation)}
        adapter = {'boot_animation_target': 'my_product/boot.zip', 'reboot_animation_target': 'my_product/missing.zip'}
        with self.assertRaisesRegex(ValueError, 'target'):
            apply_file_mods(stage, request, adapter)
        self.assertEqual(target.read_bytes(), b'original')

    def test_fff_zip_preserves_grouped_layout_and_ignores_scripts(self):
        archive = self.root / 'rom.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('wrapper/SYSTEM/system.img', b'fake image')
            z.writestr('wrapper/firmware_info.json', '{"product_name":"A"}')
            z.writestr('wrapper/flash.bat', 'must not run')
        folder, identity = prepare_source(archive, self.root / 'new')
        self.assertTrue((folder / 'wrapper/SYSTEM/system.img').is_file())
        self.assertFalse((folder / 'wrapper/flash.bat').exists())
        self.assertEqual(identity['product_name'], 'A')

    def test_zip_traversal_and_windows_paths_rejected(self):
        for name in ('../escape', '/absolute', 'C:/escape', 'dir/../escape',
                     'dir/CON.txt', 'dir/a:stream', 'dir/a.', 'dir//a'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_zip_name(name)

    def test_zip_duplicate_case_and_link_rejected_before_creation(self):
        archive = self.root / 'rom.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('system.img', 'x')
            z.writestr('SYSTEM.img', 'y')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            prepare_source(archive, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_image_duplicate_and_super_rejected(self):
        folder = self.folder('A')
        (folder / 'system.img').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            image_map(folder)
        (folder / 'system.img').unlink()
        (folder / 'super.img').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'FFF'):
            image_map(folder)

    def test_unimplemented_and_unknown_mods_are_blocked(self):
        validate_options({})
        for name, (_, supported) in MODS.items():
            validate_options({name: False})
            if not supported:
                with self.assertRaisesRegex(ValueError, 'not implemented'):
                    validate_options({name: True})
        with self.assertRaises(ValueError):
            validate_options({'apps': 'false'})
        with self.assertRaises(ValueError):
            validate_options({'invented': True})

    def test_wrong_pair_rejected(self):
        adapter = {'identities': {'base': {'product_name': 'A'}, 'donor': {'android_version': '16'}}}
        with self.assertRaisesRegex(ValueError, 'adapter requires'):
            check_identity(adapter, {'product_name': 'other'}, {'android_version': '16'})

    def test_ota_identity_uses_region_product_and_marketing_version(self):
        (self.root / 'my_manifest').mkdir()
        (self.root / 'system/system').mkdir(parents=True)
        (self.root / 'my_manifest/build.prop').write_text('ro.product.name=DONOR900IN\nro.product.model=DONOR900\nro.build.display.id=DONOR900_16.0.99(EX01)\nro.build.version.ota=DONOR900_11.A.999\n')
        (self.root / 'system/system/build.prop').write_text('ro.build.version.release=16\nro.build.version.security_patch=2026-09-01\n')
        result = identity_from_fs(self.root)
        self.assertEqual(result['product_name'], 'DONOR900IN')
        self.assertEqual(result['version_name'], 'DONOR900_16.0.99(EX01)')
        self.assertIsNone(result['arb_index'])

    def test_small_pipeline_c_override_layout_and_vbmeta_switch(self):
        self.run_small_pipeline(False)

    def test_small_pipeline_native_failure_is_not_complete(self):
        self.run_small_pipeline(True)

    def run_small_pipeline(self, fail):
        from colornt.desktop_build import build
        from colornt.io import read_json
        import struct
        parts = ['system', 'my_manifest', 'my_product', 'odm', 'boot', 'vbmeta']
        folders, caches = {}, {}
        def erofs(data):
            raw = bytearray(4096)
            struct.pack_into('<I', raw, 1024, 0xE0F5E1E2)
            raw[1036] = 12
            raw[1072:1088] = bytes(range(16))
            raw[:len(data)] = data
            return raw
        for role, name in (('base', 'A'), ('donor', 'B')):
            folder = self.root / name
            (folder / 'GROUP').mkdir(parents=True)
            (folder / 'firmware_info.json').write_text(json.dumps({'product_name': name, 'version_name': name + '_16.0', 'android_version': '16'}))
            cache = self.root / (role + '-cache')
            (cache / 'fs/config').mkdir(parents=True)
            for part in parts:
                data = bytes(b'ANDROID!' + bytes(56)) if part == 'boot' else bytes(b'AVB0' + bytes(4092)) if part == 'vbmeta' else erofs(name.encode())
                (folder / 'GROUP' / (part + '.img')).write_bytes(data)
                (cache / 'fs' / part).mkdir()
                (cache / 'fs' / part / 'fixture').write_text(name)
                for suffix in ('fs_config', 'file_contexts', 'fs_options'):
                    (cache / 'fs/config' / (part + '_' + suffix)).write_text('fixture\n')
            (cache / 'extraction.json').write_text(json.dumps({'status': 'complete', 'source': str(folder), 'partitions': [{'name': n, 'status': 'extracted'} for n in parts]}))
            folders[role], caches[role] = folder, cache
        adapter = self.root / 'adapter.json'
        adapter.write_text(json.dumps({'schema': 1, 'kind': 'desktop-port-adapter', 'id': 'test',
            'identities': {'base': {'product_name': 'A'}, 'donor': {'product_name': 'B'}},
            'hardware_files': [], 'property_policy': 'props.json', 'app_partition': 'my_product',
            'dynamic_partitions': parts[:4], 'dynamic_capacity': 1_000_000}))
        (self.root / 'props.json').write_text('{}')
        c = self.root / 'boot.img'
        c.write_bytes(b'ANDROID!C' + bytes(55))
        request = {'schema': 1, 'base': str(folders['base']), 'donor': str(folders['donor']),
                   'base_cache': str(caches['base']), 'donor_cache': str(caches['donor']),
                   'adapter': str(adapter), 'output': str(self.root / 'output'),
                   'mods': {'vbmeta_flags': True}, 'replacements': [str(c)]}
        def repack(tree, config, output, *args, **kwargs):
            output.write_bytes(erofs(b'rebuilt'))
        def verify(*args):
            if fail:
                raise ValueError('native verification failed')
            return {'status': 'pass', 'partition': args[2]}
        with patch('colornt.desktop_build.ROOT', self.root), patch('colornt.delivery.ROOT', self.root), patch('colornt.desktop_build.executable'), \
             patch('colornt.desktop_build.android_directory', side_effect=lambda p: p.mkdir(parents=True)), \
             patch('colornt.desktop_build.enable_case_sensitive'), \
             patch('colornt.desktop_build.port_properties', return_value={'status': 'applied'}), \
             patch('colornt.desktop_build.repack_erofs', side_effect=repack), \
             patch('colornt.desktop_build.extract'), patch('colornt.desktop_build.verify_roundtrip', side_effect=verify):
            if fail:
                with self.assertRaisesRegex(ValueError, 'verification failed'):
                    build(request)
                self.assertEqual(read_json(self.root / 'output.build.json')['status'], 'failed')
                self.assertFalse((self.root / 'output/firmware_info.json').exists())
            else:
                report = build(request)
                self.assertEqual(report['status'], 'package-blocked')
                self.assertTrue(report['package_validation']['filesystem_verified'])
                self.assertFalse(report['flash_ready'])
                self.assertTrue((self.root / 'output/Flash-ROM.bat').is_file())
                self.assertEqual(read_json(self.root / 'output/ColorNT-Flash/build-report.json')['status'], 'package-blocked')
                self.assertEqual((self.root / 'output/GROUP/boot.img').read_bytes(), c.read_bytes())
                self.assertEqual((self.root / 'output/GROUP/system.img').read_bytes(), (folders['donor'] / 'GROUP/system.img').read_bytes())
                from colornt.images import inspect_image
                self.assertEqual(inspect_image(self.root / 'output/GROUP/vbmeta.img')['flags'], 3)
                metadata = read_json(self.root / 'output/firmware_info.json')
                self.assertEqual(metadata['version_name'], 'A_16.0')
                self.assertFalse(metadata['colornt_device_validated'])

    def test_animation_requires_real_frame_parts(self):
        archive = self.root / 'anim.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('desc.txt', '1080 2400 60\np 0 0 part0\n')
        with self.assertRaisesRegex(ValueError, 'frames'):
            validate_animation(archive)
        with zipfile.ZipFile(archive, 'a') as z:
            z.writestr('part0/frame.png', b'fixture')
        self.assertEqual(validate_animation(archive)['dimensions'], [1080, 2400, 60])

    def test_debloat_restricts_to_app_directories(self):
        path = self.root / 'paths.txt'
        path.write_text('# comment\nmy_product/app/TestApp\n')
        self.assertEqual(read_debloat(path), ['my_product/app/TestApp'])
        for value in ('my_product', 'my_product/etc', '../input', 'my_product/app',
                      'system_ext/priv-app/X\nsystem_ext/priv-app/X'):
            path.write_text(value)
            with self.assertRaises(ValueError):
                read_debloat(path)

    def test_oplus_animation_global_header_and_comments(self):
        archive = self.root / 'anim.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('desc.txt', '# global width height offsetx offsety fps\ng 1440 777 0 688 60\np 0 0 part0\n')
            z.writestr('part0/frame.png', b'fixture')
        result = validate_animation(archive)
        self.assertEqual(result['dimensions'], [1440, 777, 60])
        self.assertEqual(result['offsets'], [0, 688])

    def test_roundtrip_detects_contents_empty_dirs_and_metadata(self):
        for name in ('stage', 'actual'):
            root = self.root / name
            (root / 'system/empty').mkdir(parents=True)
            (root / 'system/file').write_bytes(b'hello')
            (root / 'config').mkdir()
            for suffix in ('fs_config', 'file_contexts'):
                (root / 'config' / ('system_' + suffix)).write_text('system/file value\n')
        stage, actual = self.root / 'stage', self.root / 'actual'
        self.assertEqual(verify_roundtrip(stage, actual, 'system')['status'], 'pass')
        (actual / 'system/empty').rmdir()
        with self.assertRaisesRegex(ValueError, 'round-trip'):
            verify_roundtrip(stage, actual, 'system')
        (actual / 'system/empty').mkdir()
        (actual / 'config/system_fs_config').write_text('system/file wrong\n')
        with self.assertRaisesRegex(ValueError, 'metadata|fs_config'):
            verify_roundtrip(stage, actual, 'system')

    def test_apk_duplicates_split_and_shared_uid_rejected(self):
        apk = self.root / 'app.apk'
        with zipfile.ZipFile(apk, 'w') as z:
            z.writestr('AndroidManifest.xml', b'fixture')
        info = {'package': 'org.example.test', 'application': {}, 'sdk': {}}
        manifest = {'tags': [{'tag': 'manifest', 'attributes': {}}]}
        with patch('colornt.desktop_build.apk_info', return_value=info), patch('colornt.android.binary_manifest', return_value=manifest):
            self.assertEqual(len(inspect_apks([str(apk)])), 1)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                inspect_apks([str(apk), str(apk)])
            manifest['tags'][0]['attributes']['split'] = 'config.arm64'
            with self.assertRaisesRegex(ValueError, 'split'):
                inspect_apks([str(apk)])

    def test_output_overlap_and_c_mod_conflict_rejected(self):
        a, b = self.folder('A'), self.folder('B')
        adapter = self.root / 'adapter.json'
        adapter.write_text(json.dumps({'schema': 1, 'kind': 'desktop-port-adapter', 'id': 'test',
            'identities': {'base': {'product_name': 'A'}, 'donor': {'product_name': 'B'}},
            'hardware_files': [], 'app_partition': 'my_product'}))
        request = {'base': str(a), 'donor': str(b), 'adapter': str(adapter), 'output': str(a / 'nested')}
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            inspect_request(request)


if __name__ == '__main__':
    unittest.main()
