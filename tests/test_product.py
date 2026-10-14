from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt import cli, product
from colornt.desktop import App
from colornt.io import write_json


class Value:
    def __init__(self, value): self.value = value
    def get(self): return self.value


class ProductTests(unittest.TestCase):
    def test_gui_and_cli_default_selection_have_same_policy_mods_and_output_parent(self):
        app = App.__new__(App)
        app.paths = {k: Value(v) for k, v in {'base': 'A', 'donor': 'B', 'output_parent': 'chosen output'}.items()}
        app.mods = {k: Value(False) for k in ('apps', 'v14_bundle', 'boot_animation', 'reboot_animation')}
        app.install_unlocked = Value(True)
        app.apks, app.replacements, app.animations, app.capacity_source = [], [], {}, ''
        gui = app.selection()
        command = product.selection('A', 'B', 'chosen output')
        for key in command:
            self.assertEqual(gui[key], command[key])
        self.assertFalse(any(gui['mods'].values()))
        app.install_unlocked.value = False
        self.assertIsNone(app.selection()['installation_policy'])

    def test_port_cli_dispatches_same_product_pipeline_and_allows_output_location(self):
        result = {'status': 'experimental-package-ready', 'output': 'out/new', 'delivery': {'state': 'AWAITING_USER'}}
        with patch.object(product, 'create', return_value=result) as build, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['port', '--base', 'A', '--donor', 'B', '--output-parent', 'my output']), 0)
        selected = build.call_args.args[0]
        self.assertEqual(selected['output_parent'], 'my output')
        self.assertEqual(selected['installation_policy'], 'unlocked-rebuilt-metadata-v1')
        self.assertFalse(any(selected['mods'].values()))

    def test_plan_cannot_dispatch_a_build_and_blocked_plan_has_nonzero_exit(self):
        with patch.object(product, 'preview', return_value={'status': 'analysis-required'}) as plan, \
                patch.object(product, 'create') as build, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['port', '--base', 'A', '--donor', 'B', '--output-parent', 'out', '--plan']), 2)
        build.assert_not_called(); plan.assert_called_once()

    def test_cli_options_preserve_partition_mapping_and_explicit_source_policy(self):
        with patch.object(product, 'create', return_value={'status': 'package-blocked'}) as build, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['port', '--base', 'A', '--donor', 'B', '--output-parent', 'out',
                                      '--source-metadata', '--apk', 'app.apk', '--img', 'boot=C:/path/custom.img',
                                      '--boot-animation', 'anim.zip']), 2)
        selected = build.call_args.args[0]
        self.assertIsNone(selected['installation_policy'])
        self.assertTrue(selected['mods']['apps']); self.assertTrue(selected['mods']['boot_animation'])
        self.assertEqual(selected['replacements'], [{'partition': 'boot', 'path': 'C:/path/custom.img'}])

    def test_bad_img_and_report_overlap_are_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(product, 'create') as build, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(tmp); (root / 'A').mkdir(); (root / 'B').mkdir()
            command = ['port', '--base', str(root / 'A'), '--donor', str(root / 'B'), '--output-parent', str(root / 'out')]
            self.assertEqual(cli.main(command + ['--img', 'missing-equals']), 1)
            self.assertEqual(cli.main(command + ['--report', str(root / 'A/report.json')]), 1)
            existing = root / 'existing.json'; existing.write_text('preserve')
            self.assertEqual(cli.main(command + ['--report', str(existing)]), 1)
            self.assertEqual(existing.read_text(), 'preserve'); build.assert_not_called()

    def test_existing_candidate_cannot_become_an_output_parent_even_in_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / 'accepted'; rom.mkdir()
            write_json(rom / 'BUILD-MANIFEST.json', {'state': 'accepted'})
            for parent in (rom, rom / 'nested'):
                with self.assertRaisesRegex(ValueError, 'existing ROM/candidate'):
                    product.check_output_parent(parent)
            product.check_output_parent(rom.parent)

    def test_unknown_pair_preview_is_read_only_and_does_not_invent_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for role in ('A', 'B'):
                folder = root / role; folder.mkdir()
                (folder / 'system.img').write_bytes(b'not a full build fixture')
                write_json(folder / 'firmware_info.json', {'product_name': role})
            selected = product.selection(root / 'A', root / 'B', root / 'out')
            with patch('colornt.jobs.engine.extract') as native:
                self.assertEqual(product.preview(selected)['status'], 'analysis-required')
                native.assert_not_called()
            self.assertFalse((root / 'out').exists())
