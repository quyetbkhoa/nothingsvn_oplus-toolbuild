"""Simple A/B/output interface shared by the local GUI and toolbuild CLI."""
from pathlib import Path
import uuid

from . import desktop_build as engine
from .io import read_json
from .jobs import BuildJob, local_config, select_rules
from .test_avb import INSTALLATION_POLICY


def selection(base, donor, output_parent, *, unlocked=True, apks=(), replacements=(), animations=None, v14=False):
    mods = {'apps': bool(apks), 'boot_animation': False, 'reboot_animation': False, 'v14_bundle': bool(v14)}
    result = {'base': str(base), 'donor': str(donor), 'output_parent': str(output_parent),
              'mods': mods, 'apks': list(map(str, apks)), 'replacements': list(replacements),
              'installation_policy': INSTALLATION_POLICY if unlocked else None}
    for key, value in (animations or {}).items():
        if key not in ('boot_animation', 'reboot_animation'):
            raise ValueError('Unknown animation selection: ' + key)
        mods[key] = True
        result[key] = str(value)
    return result


def preview(selected):
    """Read metadata/headers only; no extraction, devices, or output creation."""
    parent = Path(selected['output_parent']).resolve()
    check_output_parent(parent)
    summaries = [engine.source_summary(selected[role]) for role in ('base', 'donor')]
    for summary in summaries:
        protected = Path(summary['path']).resolve()
        if parent == protected or parent.is_relative_to(protected):
            raise ValueError('Output parent overlaps protected input')
    adapter = select_rules(*summaries)
    if adapter is None:
        return {'status': 'analysis-required', 'base': summaries[0], 'donor': summaries[1],
                'output_parent': str(parent), 'installation_policy': selected.get('installation_policy'),
                'reason': 'Core must analyze this pair before building; no ROM/device command was run.',
                'device_validated': False}
    config = local_config()
    request = dict(selected, output=str(parent / ('preview-' + uuid.uuid4().hex[:12])),
                   adapter=str(adapter), work_root=config.get('desktop_fallback_root') or str(parent / '.colornt-work'))
    request.pop('output_parent')
    plan = engine.inspect_request(request)
    plan['output_parent'] = str(parent)
    plan['source_rules'] = read_json(adapter)['id']
    return plan


def create(selected, emit=lambda event: None, cancelled=lambda: False):
    return BuildJob(selected, emit, cancelled).run()


def check_output_parent(parent):
    parent = Path(parent).resolve()
    if any((p / 'BUILD-MANIFEST.json').is_file() or (p / 'ColorNT-Flash/build-report.json').is_file()
           or (p / 'firmware_info.json').is_file() for p in [parent, *parent.parents]):
        raise ValueError('Choose an output parent outside an existing ROM/candidate; create a new sibling folder')
