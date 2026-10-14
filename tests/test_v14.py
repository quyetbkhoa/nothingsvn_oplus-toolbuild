from contextlib import redirect_stdout
import hashlib
import io
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt import cli, product, v14
from colornt.io import write_json


class V14Tests(unittest.TestCase):
    def test_cli_explicit_bundle_is_opt_in_and_does_not_change_default_pure_port(self):
        for selected in (False, True):
            args = ['port', '--base', 'A', '--donor', 'B', '--output-parent', 'out']
            if selected: args += ['--v14-full']
            with patch.object(product, 'create', return_value={'status': 'experimental-package-ready'}) as build, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            self.assertEqual(build.call_args.args[0]['mods']['v14_bundle'], selected)
            self.assertFalse(build.call_args.args[0]['mods']['apps'])

    def test_recipe_mutation_is_rejected_before_asset_access(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(v14, 'ROOT', Path(tmp)):
            path = Path(tmp) / v14.RECIPE
            write_json(path, {'variant': {'install_mods': True, 'install_toolbox': True}})
            with self.assertRaisesRegex(ValueError, 'recipe is missing/changed'):
                v14.recipe()

    def test_wrong_input_cannot_use_an_exact_version_bundle(self):
        data = {'input_identity': {'base': {'product_name': 'known-A'}, 'donor': {'product_name': 'known-B'}}}
        with patch.object(v14, 'recipe', return_value=data), patch.object(v14, 'assets') as assets:
            with self.assertRaisesRegex(ValueError, 'requires exact base'):
                v14.inspect({'identity': {'product_name': 'other-A'}}, {'identity': {'product_name': 'known-B'}})
            assets.assert_not_called()

    def test_reference_cache_has_to_match_used_file_bytes_and_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cache = root / 'cache'; source = root / 'source'; source.mkdir()
            (cache / 'fs/system').mkdir(parents=True)
            payload = cache / 'fs/system/used'; payload.write_bytes(b'reference')
            (cache / 'fs/system/not-exported').write_bytes(b'extra must not enter stage')
            header = {'format': 'erofs', 'size': 4096}
            write_json(cache / 'extraction.json', {'status': 'complete', 'source': str(source),
                'partitions': [{'name': 'system', 'status': 'extracted', 'image': header}]})
            row = {'source': 'base', 'source_path': 'system/used', 'size': 9, 'sha256': hashlib.sha256(b'reference').hexdigest()}
            data = {'files': [row]}
            summary = {'path': str(source), 'images': {'system': {'header': header}}}
            with patch.object(v14, 'recipe', return_value=data):
                self.assertTrue(v14.reference_cache(cache, summary, 'base'))
                payload.write_bytes(b'changed!!')
                self.assertFalse(v14.reference_cache(cache, summary, 'base'))
                # Header/size preflight is explicitly not content certification;
                # reconstruct.stage must still reject wrong bytes before IMG.
                self.assertTrue(v14.reference_cache(cache, summary, 'base', content=False))
                payload.write_bytes(b'wrong-size')
                self.assertFalse(v14.reference_cache(cache, summary, 'base', content=False))
                payload.write_bytes(b'reference'); summary['images']['system']['header'] = {'format': 'erofs', 'size': 8192}
                self.assertFalse(v14.reference_cache(cache, summary, 'base'))

    def test_policy_compiler_must_be_pinned_before_any_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(v14, 'ROOT', Path(tmp)), patch('subprocess.run') as process:
            write_json(Path(tmp) / 'config/project.local.json', {'desktop_policy_compiler': str(Path(tmp) / 'missing')})
            with self.assertRaisesRegex(ValueError, 'compiler unavailable/changed'):
                v14.compile_policy(Path(tmp) / 'stage', Path(tmp))
            process.assert_not_called()

    def test_extra_partition_cache_needs_exact_source_and_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'B'; source.mkdir()
            cache = root / 'extra'; (cache / 'fs/odm').mkdir(parents=True)
            header = {'format': 'erofs', 'size': 4096}
            state = {'status': 'complete', 'source': str(source), 'partitions': [
                {'name': 'odm', 'status': 'extracted', 'image': header}]}
            write_json(cache / 'extraction.json', state)
            summary = {'path': str(source), 'images': {'odm': {'header': header}}}
            v14.check_partition_cache(cache / 'fs', 'odm', summary)
            state['partitions'][0]['image'] = {'format': 'erofs', 'size': 8192}
            write_json(cache / 'extraction.json', state)
            with self.assertRaisesRegex(ValueError, 'different/incomplete input'):
                v14.check_partition_cache(cache / 'fs', 'odm', summary)

    def test_runtime_style_compile_preserves_failed_neverallow_audit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(v14, 'ROOT', Path(tmp)):
            root = Path(tmp); compiler = root / 'secilc'; compiler.write_bytes(b'fixture')
            write_json(root / 'config/project.local.json', {'desktop_policy_compiler': str(compiler)})
            def digest(path):
                return 'ae83114646bd22993a2f8856062a7bd168853897af02337bde386fb79d938e5b'
            def native(args, log):
                if '-N' not in args:
                    log.write(b'neverallow check failed\n'); log.flush()
                    raise subprocess.CalledProcessError(255, args)
                (root / 'v14-host-policy-v33.policy').write_bytes(b'compiled fixture')
            library = subprocess.CompletedProcess([], 0, stdout='52f9f0665b6e98f7dcd6e52d87506bffa33a1dcc0797f24db994ca2587ee87f7  library\n')
            with patch.object(v14, 'sha256', side_effect=digest), patch('subprocess.run', return_value=library), \
                 patch('colornt.tools._run_native', side_effect=native) as process:
                result = v14.compile_policy(root / 'stage', root)
            self.assertEqual(process.call_count, 2)
            self.assertEqual(result['neverallow_audit']['status'], 'FAIL')
            self.assertEqual(result['status'], 'HOST-COMPILE-PASS')
            self.assertIn('-N', result['compile_flags'])

    def test_non_neverallow_compiler_failure_remains_fatal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(v14, 'ROOT', Path(tmp)):
            root = Path(tmp); compiler = root / 'secilc'; compiler.write_bytes(b'fixture')
            write_json(root / 'config/project.local.json', {'desktop_policy_compiler': str(compiler)})
            def native(args, log):
                log.write(b'Failed to resolve type\n'); log.flush()
                raise subprocess.CalledProcessError(255, args)
            library = subprocess.CompletedProcess([], 0, stdout='52f9f0665b6e98f7dcd6e52d87506bffa33a1dcc0797f24db994ca2587ee87f7  library\n')
            with patch.object(v14, 'sha256', return_value='ae83114646bd22993a2f8856062a7bd168853897af02337bde386fb79d938e5b'), \
                 patch('subprocess.run', return_value=library), patch('colornt.tools._run_native', side_effect=native) as process:
                with self.assertRaises(subprocess.CalledProcessError):
                    v14.compile_policy(root / 'stage', root)
            self.assertEqual(process.call_count, 1)
