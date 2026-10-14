"""Generate an experimental, input-specific adapter within the evidenced A16 IN scope.

Uses input values and metadata, never Ace 3V assets for a different model.
Discovery is not VINTF/HAL/kernel or runtime compatibility certification.
"""
import copy
import fnmatch
from pathlib import Path
import uuid

from .desktop_build import android_walk, check_identity, image_map, prepare_source, source_summary
from .extract import extract
from .io import read_json, write_json
from .properties import Properties
from .oplus import analyze_oplus_pair
from .tools import ROOT

# These categories come from observed A hardware transplants. Selection on a new
# device remains experimental and is recorded as such in the generated adapter.
HARDWARE_PATTERNS = (
    'my_product/etc/audio_policy*.xml', 'my_product/etc/default_volume_tables.xml',
    'my_product/vendor/etc/*', 'my_product/etc/vibrator/*',
    'my_product/etc/vibration/*', 'my_product/etc/waveform/*',
    'my_product/overlay/android_framework_res_overlay.display.product.*.apk',
    'my_product/overlay/com.android.SystemUIResOverlay.*.apk',
    'my_product/overlay/framework_res_overlay.network.*.apk',
    'system_ext/apex/com.android.vndk.v*.apex',
)


def derive_policy(base, donor, template):
    policy = copy.deepcopy(template)
    missing = []
    # Conditions remain exactly the observed manufacturer/Android/region scope.
    for condition in policy['conditions']:
        ref = condition['source']
        root = base if ref['role'] == 'base' else donor
        if Properties((root / ref['path']).read_bytes()).one(ref['key']) != condition['equals']:
            raise ValueError('Automatic adapter currently supports OnePlus Android 16, donor IN only')
    for file in policy['files']:
        baseline = Properties(((base if file['baseline'] == 'base' else donor) / file['path']).read_bytes())
        operations = []
        for operation in file['operations']:
            count = len(baseline.values(operation['key']))
            if operation['kind'] == 'identity' and not count:
                missing.append(file['path'] + ':' + operation['key'])
                continue
            if operation['kind'] == 'copy':
                ref = operation['source']
                source = Properties(((base if ref['role'] == 'base' else donor) / ref['path']).read_bytes())
                if len(source.values(ref['key'])) != 1:
                    missing.append(ref['path'] + ':' + ref['key'])
                    continue
            operation['occurrences'] = count
            operations.append(operation)
        file['operations'] = operations
    if missing:
        # Incomplete hardware fields must be explained, not silently skipped.
        raise ValueError('Automatic adapter lacks required property contract: ' + ', '.join(missing))
    policy['id'] = 'discovered-' + uuid.uuid4().hex[:12]
    policy['scope'] = 'Experimental input-derived OnePlus A16 IN property contract; no device validation'
    return policy


def discover(request, progress=lambda message: None, cancelled=lambda: False):
    """Return a generated adapter path, with isolated extraction workspaces."""
    summaries = [source_summary(request[key]) for key in ('base', 'donor')]
    known_path = ROOT / 'profiles/desktop-pjf110-cph2745.json'
    if all(s['identity'] for s in summaries):
        try:
            check_identity(read_json(known_path), summaries[0]['identity'], summaries[1]['identity'])
            progress('Nhận diện đúng cặp đã phân tích; dùng adapter đã lưu')
            return known_path
        except ValueError:
            pass
    work_root = Path(request.get('work_root') or ROOT / 'work').resolve()
    for key in ('base', 'donor', 'output'):
        protected = Path(request[key]).resolve()
        if work_root == protected or work_root.is_relative_to(protected):
            raise ValueError('Discovery workspace overlaps input/output')
    work = work_root / ('discovery-' + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    roots, identities = {}, {}
    groups = None
    for role, key in (('base', 'base'), ('donor', 'donor')):
        if cancelled():
            raise ValueError('Cancelled during ROM analysis')
        progress('Discovery: giải nén ' + role)
        prepared = request.get('prepared_inputs', {}).get(role)
        if prepared:
            folder, identity = Path(prepared['folder']), prepared['identity']
        else:
            folder, identity = prepare_source(Path(request[key]), work / ('input-' + role))
        partitions = ['system', 'my_manifest', 'my_product', 'odm', 'system_ext']
        if 'my_stock' in image_map(folder):
            partitions.append('my_stock')
        destination = Path(prepared['extracted']) if prepared else work / role
        state = read_json(destination / 'extraction.json') if prepared else extract(folder, destination, partitions, hashes=False)
        if (state['status'] != 'complete' or Path(state['source']).resolve() != folder.resolve()
                or not set(partitions) <= {p['name'] for p in state['partitions']}
                or any(i['status'] != 'extracted' for i in state['partitions'] if i['name'] in partitions)):
            raise ValueError('Automatic adapter requires EROFS with full Android metadata')
        roots[role] = destination / 'fs'
        identities[role] = {k: identity[k] for k in ('product_name', 'version_name', 'android_version')}
        if role == 'base':
            groups = identity.get('dynamic_groups') or request.get('dynamic_groups')
    analysis = analyze_oplus_pair(roots['base'], roots['donor'])
    write_json(work / 'oplus-analysis.json', analysis)
    if analysis['blockers']:
        raise ValueError('Oplus inputs recognized, but automatic port rules are incomplete: '
                         + ' '.join(analysis['blockers']) + ' Analysis: ' + str(work / 'oplus-analysis.json'))
    policy = derive_policy(roots['base'], roots['donor'], read_json(ROOT / 'recipes/oplus-a16-in-core-properties.json'))
    write_json(work / 'properties.json', policy)
    paths = sorted(p.relative_to(roots['base']).as_posix() for part in ('my_product', 'system_ext')
                   for p in android_walk(roots['base'] / part) if p.is_file() and
                   any(fnmatch.fnmatchcase(p.relative_to(roots['base']).as_posix(), pattern) for pattern in HARDWARE_PATTERNS))
    if not paths:
        raise ValueError('No evidenced hardware configuration categories found in A')
    # No inherited sample group size: FFF folder needs explicit known A capacity.
    capacity = sum(g['maximum_size'] for g in groups) if groups else request.get('dynamic_capacity')
    if type(capacity) is not int or capacity <= 0:
        write_json(work / 'discovery.json', {'status': 'capacity-required', 'identities': identities,
                   'hardware_files': paths, 'property_policy': str(work / 'properties.json'),
                   'reason': 'FFF firmware_info does not contain super group capacity. Need A OTA manifest or explicit capacity in saved project.'})
        raise ValueError('Discovery extracted this pair, but A dynamic group capacity is missing. '
                         'Set dynamic_capacity (bytes) from A manifest in saved project; evidence: ' + str(work / 'discovery.json'))
    from .desktop_build import OS_PARTITIONS
    adapter = {'schema': 1, 'kind': 'desktop-port-adapter', 'id': 'experimental-' + uuid.uuid4().hex[:12],
               'identities': identities, 'property_policy': str(work / 'properties.json'),
               'hardware_files': paths, 'app_partition': 'my_product',
               'boot_animation_target': 'my_product/media/bootanimation/bootanimation.zip',
               'reboot_animation_target': 'my_product/media/bootanimation/rbootanimation.zip',
               'dynamic_capacity': capacity,
               'dynamic_partitions': sorted(OS_PARTITIONS | {'vendor', 'odm', 'system_dlkm', 'vendor_dlkm'}),
               'evidence': ['reports/OPLUS-PORT-ANALYSIS.md; input-derived experimental generalization'],
               'limitations': ['Experimental discovery; VINTF/HAL/kernel/SELinux/AVB/runtime are not certified.',
                               'Only OnePlus Android16 donor IN property scope currently implemented.',
                               'Hardware file categories are inferred from the sample, not verified on this device.']}
    if groups:
        adapter['dynamic_groups'] = groups
        adapter['dynamic_partitions'] = sorted({p for group in groups for p in group['partitions']})
    path = work / 'adapter.json'
    write_json(path, adapter)
    write_json(work / 'discovery.json', {'status': 'experimental-adapter-generated', 'adapter': str(path),
                                      'identities': identities, 'hardware_files': paths, 'device_validated': False})
    progress('Đã tạo adapter thử nghiệm từ input; chưa kiểm chứng tương thích phần cứng')
    return path
