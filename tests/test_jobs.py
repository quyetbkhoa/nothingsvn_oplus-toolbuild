from pathlib import Path
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from colornt import desktop_build as engine
from colornt.io import read_json, write_json, copy_file, WRITE_GUARD
from colornt.android_links import copy_entry
from colornt.images import expand_sparse, SPARSE_MAGIC
import struct
from colornt.jobs import (BuildJob, JobError, W_BUDGET, RESERVE, cache_ok, required_parts,
                          source_stamp, storage_fits, seal_cache, donor_sdk, writer_lock)
from colornt.tools import JOB_GUARD, ROOT, _run_native


class ProductJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        adapter = read_json(ROOT / 'profiles/desktop-pjf110-cph2745.json')
        rule = self.root / 'adapter.json'
        write_json(rule, adapter)
        work = self.root / 'work'
        folders = {}
        for role, label in (('base', 'a'), ('donor', 'b')):
            source = self.root / role
            source.mkdir()
            write_json(source / 'firmware_info.json', adapter['identities'][role])
            for name in ('system', 'my_manifest', 'my_product', 'my_stock', 'odm', 'system_ext'):
                (source / (name + '.img')).write_bytes(b'fixture image')
            summary = engine.source_summary(source)
            cache = work / ('gui-live-' + label)
            fs = cache / 'fs'
            (fs / 'config').mkdir(parents=True)
            for name in summary['images']:
                (fs / name).mkdir()
                for suffix in ('fs_config', 'file_contexts', 'fs_options'):
                    (fs / 'config' / f'{name}_{suffix}').write_text('')
            (fs / 'system/system').mkdir()
            (fs / 'system/system/build.prop').write_text('ro.build.version.sdk=36\n')
            for name in summary['images']:
                paths = [name] + [p.relative_to(fs).as_posix() for p in engine.android_walk(fs / name)]
                (fs / 'config' / f'{name}_fs_config').write_text('\n'.join(p + ' 0 0 0755' for p in paths))
            write_json(cache / 'extraction.json', {'status': 'complete', 'source': str(source),
                'partitions': [{'name': name, 'status': 'extracted', 'image': item['header']} for name, item in summary['images'].items()]})
            seal_cache(cache, source_stamp(summary))
            folders[role] = source
        config = {'desktop_work_root': str(work), 'project_data_root': str(self.root),
                  'desktop_fallback_root': str(self.root / 'fallback')}
        selection = {**{role: str(path) for role, path in folders.items()},
                     'output_parent': str(self.root / 'out'), 'mods': {}}
        return rule, config, selection

    def test_quota_and_free_space_are_separate_and_h_fallback_can_fit(self):
        self.assertFalse(storage_fits(W_BUDGET - 10, 20, 0, 1000, 1000, False, True, True, reserve=0))
        self.assertTrue(storage_fits(W_BUDGET - 10, 20, 5, 1000, 1000, False, False, True, reserve=0))
        self.assertFalse(storage_fits(0, 60, 60, 100, 100, True, False, False, reserve=0))
        self.assertTrue(storage_fits(0, 60, 60, 100, 100, False, False, False, reserve=0))

    def test_shared_writer_lock_rejects_second_job_and_releases_after_failure(self):
        lock = self.root / 'work/product-job.lock'
        with writer_lock(lock):
            with self.assertRaises(JobError) as raised:
                with writer_lock(lock):
                    self.fail('Second writer admitted')
            self.assertEqual(raised.exception.code, 'busy')
        with writer_lock(lock):
            self.assertTrue(lock.exists())

    def test_structural_stamp_changes_when_same_path_image_is_modified(self):
        rule, config, selection = self.fixture()
        source = Path(selection['base'])
        before = engine.source_summary(source)
        stamp = source_stamp(before)
        cache = Path(config['desktop_work_root']) / 'gui-live-a'
        parts = required_parts(read_json(rule), before, 'base')
        write_json(cache / 'source-stamp.json', {'stamp': stamp})
        self.assertTrue(cache_ok(cache, before, parts, stamp))
        image = source / 'my_product.img'
        s = image.stat()
        os.utime(image, ns=(s.st_atime_ns, s.st_mtime_ns + 1_000_000_000))
        after = engine.source_summary(source)
        self.assertNotEqual(stamp, source_stamp(after))
        self.assertFalse(cache_ok(cache, after, parts, source_stamp(after)))

    def test_prepare_uses_internal_rules_and_reuses_core_cache_for_apps(self):
        rule, config, selection = self.fixture()
        selection['mods'] = {}
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), \
             patch('colornt.workspace_fs.enable_case_sensitive'), patch('colornt.jobs.engine.extract') as extract:
            job = BuildJob(selection)
            request = job.prepare()
            extract.assert_not_called()
        self.assertEqual(request['adapter'], str(rule))
        self.assertEqual(request['target_sdk'], 36)
        self.assertTrue(request['cleanup_verification'])
        self.assertNotIn('adapter', selection)
        self.assertNotIn('base_cache', selection)
        self.assertNotIn('capacity', selection)
        self.assertTrue(Path(request['base_cache']).name.startswith('gui-live-'))

    def test_native_guard_stops_a_running_process_on_cancel(self):
        def stop():
            raise JobError('cancelled', 'stop')
        token = JOB_GUARD.set(stop)
        try:
            with self.assertRaises(JobError):
                _run_native([sys.executable, '-c', 'import time; time.sleep(5)'])
        finally:
            JOB_GUARD.reset(token)

    def test_native_exit_error_is_consistent_with_and_without_guard(self):
        import subprocess
        for guard in (None, lambda: None):
            token = JOB_GUARD.set(guard)
            try:
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    _run_native([sys.executable, '-c', 'raise SystemExit(7)'])
                self.assertEqual(error.exception.returncode, 7)
            finally:
                JOB_GUARD.reset(token)

    def test_zip_cache_reuses_prepared_input_and_invalidates_when_zip_changes(self):
        rule, config, selection = self.fixture()
        registry = {}
        for role, label in (('base', 'a'), ('donor', 'b')):
            folder = Path(selection[role])
            effective = engine.source_summary(folder)
            archive = self.root / (role + '.zip')
            with zipfile.ZipFile(archive, 'w') as z:
                for file in folder.iterdir():
                    z.write(file, file.name)
            original = engine.source_summary(archive)
            cache = Path(config['desktop_work_root']) / ('gui-live-' + label)
            write_json(cache / 'source-stamp.json', {'stamp': source_stamp(effective)})
            registry[source_stamp(original)] = {'original_stamp': source_stamp(original),
                'prepared_stamp': source_stamp(effective), 'folder': str(folder),
                'identity': effective['identity'], 'extracted': str(cache)}
            selection[role] = str(archive)
        write_json(Path(config['desktop_work_root']) / 'session-index.json', registry)
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), \
             patch('colornt.workspace_fs.enable_case_sensitive'), patch('colornt.jobs.engine.extract') as extract, \
             patch('colornt.jobs.engine.prepare_source') as prepare:
            job = BuildJob(selection)
            request = job.prepare()
            extract.assert_not_called()
            prepare.assert_not_called()
        self.assertEqual(Path(request['base']), self.root / 'base')
        archive = Path(selection['base'])
        s = archive.stat()
        os.utime(archive, ns=(s.st_atime_ns, s.st_mtime_ns + 1_000_000_000))
        self.assertIsNone(job.find_cache(engine.source_summary(archive), ['system'], 'base'))

    def test_missing_case_permission_is_reported_before_extracting_any_rom(self):
        rule, config, selection = self.fixture()
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), \
             patch('colornt.workspace_fs.enable_case_sensitive', side_effect=PermissionError(5, 'case-sensitive Android workspace')), \
             patch('colornt.jobs.engine.extract') as extract:
            with self.assertRaises(JobError) as error:
                BuildJob(selection).run()
            self.assertEqual(error.exception.code, 'workspace_permission')
            extract.assert_not_called()

    def test_img_mapping_can_use_an_arbitrary_filename_without_renaming_input(self):
        image = self.root / 'custom-kernel.img'
        image.write_bytes(b'ANDROID!' + bytes(56))
        result = engine.replacement_map([{'path': str(image), 'partition': 'boot'}])
        self.assertEqual(result, {'boot': image.resolve()})
        self.assertTrue(image.exists())
        with self.assertRaisesRegex(ValueError, 'unique'):
            engine.replacement_map([{'path': str(image), 'partition': 'boot'}] * 2)

    def test_verification_cleanup_keeps_metadata_and_rejects_an_outside_tree(self):
        work = self.root / 'build'
        work.mkdir()
        write_json(work / 'build.json', {'status': 'building'})
        unpacked = work / 'verify-system'
        (unpacked / 'fs/config').mkdir(parents=True)
        (unpacked / 'fs/config/system_fs_config').write_text('system/file 0 0 0644\n')
        (unpacked / 'fs/system').mkdir()
        (unpacked / 'fs/system/file').write_bytes(b'content')
        write_json(unpacked / 'extraction.json', {'status': 'complete'})
        engine.cleanup_verified_tree(work, unpacked)
        self.assertFalse(unpacked.exists())
        self.assertTrue((work / 'verification-records/verify-system/config/system_fs_config').exists())
        protected = self.root / 'verify-input'
        protected.mkdir()
        with self.assertRaisesRegex(ValueError, 'outside'):
            engine.cleanup_verified_tree(work, protected)
        self.assertTrue(protected.exists())

    def test_cache_rejects_case_folded_tree_even_when_headers_and_counts_look_valid(self):
        rule, config, selection = self.fixture()
        source = engine.source_summary(selection['donor'])
        cache = Path(config['desktop_work_root']) / 'gui-live-b'
        part = cache / 'fs/my_product'
        (part / 'ES-Vodafone').mkdir()
        (cache / 'fs/config/my_product_fs_config').write_text('my_product 0 0 0755\nmy_product/ES-Vodafone 0 0 0755\nmy_product/es-vodafone 0 0 0755\n')
        self.assertFalse(cache_ok(cache, source, ['my_product'], source_stamp(source), allow_legacy=True))

    def test_cache_rejects_truncated_entry_metadata_changes_and_unsealed_legacy(self):
        _, config, selection = self.fixture()
        summary = engine.source_summary(selection['donor'])
        cache = Path(config['desktop_work_root']) / 'gui-live-b'
        stamp = source_stamp(summary)
        self.assertTrue(cache_ok(cache, summary, ['system'], stamp))
        (cache / 'fs/system/system/build.prop').write_bytes(b'')
        self.assertFalse(cache_ok(cache, summary, ['system'], stamp, allow_legacy=True))
        seal_cache(cache, stamp)
        (cache / 'fs/config/system_fs_config').write_text('changed')
        self.assertFalse(cache_ok(cache, summary, ['system'], stamp))
        (cache / 'cache-integrity.json').unlink()
        self.assertFalse(cache_ok(cache, summary, ['system'], stamp, allow_legacy=True))

    def test_sdk_missing_invalid_or_duplicate_fails_without_default(self):
        _, config, _ = self.fixture()
        cache = Path(config['desktop_work_root']) / 'gui-live-b'
        prop = cache / 'fs/system/system/build.prop'
        for value in ('', 'ro.build.version.sdk=bad\n', 'ro.build.version.sdk=0\n',
                      'ro.build.version.sdk=36\nro.build.version.sdk=36\n'):
            prop.write_text(value)
            with self.assertRaises(JobError):
                donor_sdk(cache)

    def test_copy_cancel_between_chunks_preserves_destination_and_partial_neighbor(self):
        src, dst = self.root / 'source', self.root / 'dest'
        src.write_bytes(b'x' * (3 * 1024**2))
        dst.write_bytes(b'original')
        neighbor = self.root / 'dest.partial'
        neighbor.write_bytes(b'protected')
        calls = []
        def cancel():
            calls.append(True)
            if len(calls) == 2:
                raise JobError('cancelled', 'stop')
        token = JOB_GUARD.set(cancel)
        try:
            with self.assertRaises(JobError):
                copy_entry(src, dst)
        finally:
            JOB_GUARD.reset(token)
        self.assertEqual(len(calls), 2)
        self.assertEqual(dst.read_bytes(), b'original')
        self.assertEqual(neighbor.read_bytes(), b'protected')
        self.assertFalse(list(self.root.glob('.colornt-copy-*')))

    def test_sparse_cancel_and_quota_admission_cover_raw_fill_and_holes(self):
        for kind in (0xCAC1, 0xCAC2, 0xCAC3):
            src, dst = self.root / f'{kind}.img', self.root / f'{kind}.raw'
            expanded = 3 * 1024**2
            payload = b'x' * expanded if kind == 0xCAC1 else b'ABCD' if kind == 0xCAC2 else b''
            src.write_bytes(struct.pack('<I4H4I', SPARSE_MAGIC, 1, 0, 28, 12, 4096, expanded // 4096, 1, 0)
                            + struct.pack('<2H2I', kind, 0, expanded // 4096, 12 + len(payload)) + payload)
            admitted = []
            def quota(path, size):
                if sum(admitted) + size > 1024**2:
                    raise JobError('space', 'quota')
                admitted.append(size)
            token = WRITE_GUARD.set(quota)
            try:
                with self.assertRaises(JobError):
                    expand_sparse(src, dst)
            finally:
                WRITE_GUARD.reset(token)
            self.assertFalse(dst.exists())
            self.assertFalse(dst.with_name(dst.name + '.partial').exists())
            token = JOB_GUARD.set(lambda: (_ for _ in ()).throw(JobError('cancelled', 'stop')))
            try:
                with self.assertRaises(JobError):
                    expand_sparse(src, dst)
            finally:
                JOB_GUARD.reset(token)
            self.assertFalse(dst.exists())

    def test_write_reservation_stops_real_copy_before_w_cap(self):
        _, config, selection = self.fixture()
        with patch('colornt.jobs.local_config', return_value=config):
            job = BuildJob(selection)
        job.session = self.root / 'session'
        job.initial_w = W_BUDGET - RESERVE - 1024**2
        src, dst = self.root / 'src', self.root / 'dst'
        src.write_bytes(b'x' * (2 * 1024**2))
        token = WRITE_GUARD.set(job.reserve_write)
        try:
            with patch('colornt.jobs.volume', return_value='W:'), patch('colornt.jobs.free_bytes', return_value=10 * 1024**3):
                with self.assertRaises(JobError):
                    copy_file(src, dst)
        finally:
            WRITE_GUARD.reset(token)
        self.assertEqual(job.pending_w, 1024**2)
        self.assertFalse(dst.exists())

    def test_sparse_hole_reservation_survives_forced_quota_rescan(self):
        _, config, selection = self.fixture()
        with patch('colornt.jobs.local_config', return_value=config):
            job = BuildJob(selection)
        job.session = self.root / 'session'
        job.session.mkdir()
        job.monitored = [job.session]
        job.initial_w = W_BUDGET - RESERVE - 1024**2
        src, dst = self.root / 'holes.img', job.session / 'holes.raw'
        src.write_bytes(struct.pack('<I4H4I', SPARSE_MAGIC, 1, 0, 28, 12, 4096, 512, 2, 0)
                        + struct.pack('<2H2I', 0xCAC3, 0, 256, 12) * 2)
        guard = JOB_GUARD.set(lambda: job.guard(force=True))
        reserve = WRITE_GUARD.set(job.reserve_write)
        try:
            with patch('colornt.jobs.volume', return_value='W:'), patch('colornt.jobs.free_bytes', return_value=10 * 1024**3):
                with self.assertRaises(JobError):
                    expand_sparse(src, dst)
        finally:
            WRITE_GUARD.reset(reserve)
            JOB_GUARD.reset(guard)
        self.assertEqual(job.scanned_growth_w, 1024**2)
        self.assertFalse(dst.exists())

    def test_sparse_cleanup_preserves_temp_created_by_another_writer_before_open(self):
        src, dst = self.root / 'sparse.img', self.root / 'expanded.img'
        src.write_bytes(struct.pack('<I4H4I', SPARSE_MAGIC, 1, 0, 28, 12, 4096, 1, 1, 0)
                        + struct.pack('<2H2I', 0xCAC3, 0, 1, 12))
        temp = dst.with_name(dst.name + '.partial')
        original_open = Path.open
        def raced_open(path, mode='r', *args, **kwargs):
            if path == temp and mode == 'xb':
                with original_open(temp, 'wb') as stream:
                    stream.write(b'other writer')
                raise FileExistsError('Other writer won the create')
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', new=raced_open):
            with self.assertRaises(FileExistsError):
                expand_sparse(src, dst)
        self.assertEqual(temp.read_bytes(), b'other writer')

    def test_invalid_selection_fails_before_allocation(self):
        rule, config, selection = self.fixture()
        for change in ({'mods': {'does_not_exist': True}}, {'apks': [str(self.root / 'missing.apk')], 'mods': {'apps': True}},
                       {'mods': {'boot_animation': True}, 'boot_animation': str(self.root / 'missing.zip')}):
            with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), patch.object(BuildJob, 'allocate') as allocate:
                with self.assertRaises((ValueError, OSError)):
                    BuildJob(dict(selection, **change)).prepare()
                allocate.assert_not_called()

    def test_c_mod_conflict_and_invalid_apk_content_fail_before_allocation(self):
        rule, config, selection = self.fixture()
        invalid_apk = self.root / 'invalid.apk'
        invalid_apk.write_bytes(b'not an apk')
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), patch.object(BuildJob, 'allocate') as allocate:
            with self.assertRaises(zipfile.BadZipFile):
                BuildJob(dict(selection, mods={'apps': True}, apks=[str(invalid_apk)])).prepare()
            allocate.assert_not_called()
        image = self.root / 'custom.img'
        image.write_bytes(b'ANDROID!' + bytes(56))
        selection.update(mods={'apps': True}, apks=[str(invalid_apk)], replacements=[{'partition': 'my_product', 'path': str(image)}])
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), patch('colornt.jobs.engine.inspect_apks'), patch.object(BuildJob, 'allocate') as allocate:
            with self.assertRaises(JobError) as raised:
                BuildJob(selection).prepare()
            self.assertEqual(raised.exception.code, 'conflict')
            allocate.assert_not_called()

    def test_prepare_failure_cleans_owned_incomplete_tree_before_build(self):
        rule, config, selection = self.fixture()
        # No legacy sealing is allowed, forcing new extraction.
        for label in ('a', 'b'):
            (Path(config['desktop_work_root']) / ('gui-live-' + label) / 'cache-integrity.json').unlink()
        def failing_extract(source, dest, parts, hashes=False):
            (dest / 'raw').mkdir(parents=True)
            (dest / 'raw/partial.img').write_bytes(b'x' * 1024**2)
            write_json(dest / 'extraction.json', {'status': 'failed'})
            raise JobError('cancelled', 'stop')
        with patch('colornt.jobs.local_config', return_value=config), patch('colornt.jobs.select_rules', return_value=rule), patch('colornt.workspace_fs.enable_case_sensitive'), patch('colornt.jobs.engine.extract', side_effect=failing_extract):
            job = BuildJob(selection)
            with self.assertRaises(JobError):
                job.run()
        self.assertFalse((job.session / 'base').exists())
        self.assertTrue((job.session / 'diagnostics/base/extraction.json').exists())
        self.assertTrue((Path(selection['base']) / 'system.img').exists())


if __name__ == '__main__':
    unittest.main()
