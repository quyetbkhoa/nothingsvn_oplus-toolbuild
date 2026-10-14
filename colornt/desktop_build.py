"""Local GUI build engine with explicit device and optional mod contracts.

An adapter is a compatibility contract, not a claim that arbitrary ROMs boot.
All optional modifications default off. Pinned v1.4 reconstruction may add
explicitly budgeted sample partitions; ordinary ports follow A's image layout.
"""
from pathlib import Path
import json
import os
import re
import shutil
import struct
import uuid
import zipfile

from .android import apk_info
from .package_validation import inspect_for_gate
from .android_links import copy_entry, entry_digest, entry_path, link_target
from .extract import extract
from .images import inspect_image
from .io import confined, read_json, write_json, copy_stream, copy_file, sha256
from .properties import port_properties
from .repack import repack_erofs
from .tools import ROOT, JOB_GUARD, executable, run_tool
from .workspace_fs import android_directory, enable_case_sensitive

OS_PARTITIONS = frozenset(('system', 'system_ext', 'product', 'my_product', 'my_manifest',
                          'my_region', 'my_carrier', 'my_heytap', 'my_bigball', 'my_stock',
                          'my_engineering', 'my_company', 'my_preload'))
PRIVATE_PARTITIONS = frozenset(('userdata', 'metadata', 'persist', 'modemst1', 'modemst2',
                               'fsg', 'fsc', 'frp'))
MODS = {
    'apps': ('Thêm APK từ PC (system app thường)', True),
    'boot_animation': ('Boot animation ZIP từ PC', True),
    'reboot_animation': ('Reboot animation ZIP từ PC', True),
    'debloat': ('Debloat theo danh sách đường dẫn', True),
    'vbmeta_flags': ('AVB: đặt vbmeta flags=3 (không ký lại IMG)', True),
    'unlocked_avb': ('Chuẩn bị AVB để thử trên máy unlocked (tắt verification)', True),
    'v14_bundle': ('Bộ mod v1.4: NTManager, NexAlloy, InstallerX, Toolbox (cặp ROM hiện tại)', True),
    'ntmanager': ('NTManager + tích hợp framework — chưa có engine tổng quát', False),
    'nexalloy': ('NexAlloy / Toolbox — chưa có engine tổng quát', False),
    'installerx': ('InstallerX + quyền đặc biệt — chưa có engine tổng quát', False),
    'framework': ('Framework / services patch — chưa có engine tổng quát', False),
    'settings': ('Settings / launcher / SystemUI patch — chưa có engine tổng quát', False),
    'ota_block': ('Chặn OTA / thay trust — chưa có engine tổng quát', False),
    'selinux': ('SELinux bridge — chưa có engine tổng quát', False),
    'splash': ('Sinh splash theo panel A — chưa có engine tổng quát', False),
    'features': ('XML features / clone app / region — chưa có engine tổng quát', False),
}


def validate_options(options):
    if options.get('unlocked_avb') and options.get('vbmeta_flags'):
        raise ValueError('Unlocked AVB policy conflicts with legacy vbmeta flag edits')
    if set(options) - set(MODS):
        raise ValueError('Unknown modification option')
    for name, value in options.items():
        if type(value) is not bool:
            raise ValueError('Modification switches must be booleans')
        if value and not MODS[name][1]:
            raise ValueError(f'{name}: reusable transformation is not implemented')


def normalize_installation_request(request):
    """UI and CLI share one explicit lab installation policy, separate from mods."""
    validate_options(request.get('mods', {}))
    if request.get('mods', {}).get('unlocked_avb') and not request.get('installation_policy'):
        from .test_avb import INSTALLATION_POLICY
        return dict(request, installation_policy=INSTALLATION_POLICY,
                    mods=dict(request['mods'], unlocked_avb=False))
    return dict(request)


def image_map(folder):
    result = {}
    for path in sorted(android_walk(folder), key=lambda p: p.as_posix()):
        if path.is_symlink():
            raise ValueError(f'Linked input entry is unsupported: {path}')
        if path.is_file() and path.suffix.lower() == '.img':
            if not re.fullmatch(r'[a-zA-Z0-9_]+', path.stem) or path.stem in result:
                raise ValueError(f'Invalid or duplicate partition basename: {path.name}')
            result[path.stem] = path
    if not result:
        raise ValueError(f'No IMG files in {folder}')
    if 'super' in result:
        raise ValueError('super.img input requires logical IMG extraction through FFF first')
    return result


def source_summary(source):
    source = Path(source).resolve(strict=True)
    if source.is_dir():
        images = image_map(source)
        infos = [p for p in android_walk(source) if p.name == 'firmware_info.json']
        if len(infos) != 1:
            raise ValueError('FFF input needs exactly one firmware_info.json')
        return {'path': str(source), 'kind': 'fff-folder', 'identity': read_json(infos[0]),
                'images': {name: {'path': p.relative_to(source).as_posix(), 'size': p.stat().st_size,
                                  'header': inspect_image(p)} for name, p in images.items()}}
    with zipfile.ZipFile(source) as archive:
        directories = [i.filename.rstrip('/') for i in archive.infolist() if i.is_dir()]
        for name in directories:
            safe_zip_name(name)
        entries = [i for i in archive.infolist() if not i.is_dir()]
        # Validate every name before extracting anything. Never execute bundled scripts.
        seen = set()
        for info in entries:
            safe_zip_name(info.filename)
            key = info.filename.replace('\\', '/').casefold()
            if key in seen or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('ZIP has duplicate names or symlinks')
            seen.add(key)
        imgs = [i for i in entries if i.filename.lower().endswith('.img')]
        payloads = [i for i in entries if i.filename.rsplit('/', 1)[-1] == 'payload.bin']
        infos = [i for i in entries if i.filename.rsplit('/', 1)[-1] == 'firmware_info.json']
        if imgs and not payloads and len(infos) == 1:
            names = [Path(i.filename).stem for i in imgs]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate ZIP partition basenames')
            if archive.getinfo(infos[0].filename).file_size > 1024 * 1024:
                raise ValueError('Oversized firmware metadata')
            identity = json.loads(archive.read(infos[0]).decode('utf-8-sig'))
            image_entries = {}
            for item in imgs:
                record = {'path': item.filename, 'size': item.file_size}
                with archive.open(item) as stream:
                    header = stream.read(28)
                if header[:4] == b'\x3a\xff\x26\xed':
                    if len(header) != 28:
                        raise ValueError('Truncated sparse IMG header in ZIP')
                    fields = struct.unpack('<I4H4I', header)
                    record['expanded_size'] = fields[5] * fields[6]
                image_entries[Path(item.filename).stem] = record
            return {'path': str(source), 'kind': 'fff-zip', 'identity': identity,
                    'images': image_entries}
        if len(payloads) == 1 and not imgs:
            return {'path': str(source), 'kind': 'ota-zip', 'identity': None, 'images': {},
                    'note': 'Full payload is extracted in work; delta payloads are rejected by dumper.'}
        raise ValueError('ZIP must contain FFF IMG + firmware_info.json, or a single full OTA payload.bin')


def safe_zip_name(name):
    parts = name.replace('\\', '/').split('/')
    if not name or name.startswith(('/', '\\')) or any(
        p in ('.', '..', '') or ':' in p or p.endswith(('.', ' ')) or
        re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p)
        for p in parts):
        raise ValueError(f'Unsafe ZIP path: {name}')


def prepare_source(source, destination):
    summary = source_summary(source)
    if summary['kind'] == 'fff-folder':
        return Path(summary['path']), summary['identity']
    destination.mkdir(parents=True)
    if summary['kind'] == 'fff-zip':
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                if item.is_dir() or not (item.filename.lower().endswith('.img') or
                                         item.filename.rsplit('/', 1)[-1] == 'firmware_info.json'):
                    continue
                target = confined(destination, item.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open('xb') as dst:
                    copy_stream(src, dst)
        return destination, summary['identity']
    executable('payload-dumper-go')
    from .ota import payload_info
    manifest = payload_info(Path(source))
    if manifest['minor_version'] != 0:
        raise ValueError('Delta OTA is unsupported; restore full logical IMG through FFF first')
    with zipfile.ZipFile(source) as archive:
        name = next(n for n in archive.namelist() if n.rsplit('/', 1)[-1] == 'payload.bin')
        with archive.open(name) as src, (destination / 'payload.bin').open('xb') as dst:
            copy_stream(src, dst)
    images = destination / 'images'
    images.mkdir()
    run_tool('payload-dumper-go', ['-o', images, destination / 'payload.bin'], destination / 'payload.log')
    # Inspect only identity partitions; do not synthesize identities from the sample.
    extract(images, destination / 'identity', ['my_manifest', 'system'], hashes=False)
    identity = identity_from_fs(destination / 'identity/fs')
    identity['dynamic_groups'] = manifest['groups']
    write_json(images / 'firmware_info.json', identity)
    return images, identity


def identity_from_fs(fs):
    from .properties import Properties
    props = Properties((fs / 'my_manifest/build.prop').read_bytes())
    system = Properties((fs / 'system/system/build.prop').read_bytes())
    return {'product_name': props.one('ro.product.name'),
                'version_name': props.one('ro.build.display.id'),
                'android_version': system.one('ro.build.version.release'),
                'security_patch': system.one('ro.build.version.security_patch'),
                'arb_index': None, 'identity_source': 'extracted OTA properties; rollback unknown'}


def inspect_request(request):
    if any(not request.get(key) for key in ('base', 'donor', 'output')):
        raise ValueError('Select A, B and a new output folder')
    request = normalize_installation_request(request)
    from .test_avb import INSTALLATION_POLICY
    install_policy = request.get('installation_policy')
    if install_policy not in (None, INSTALLATION_POLICY):
        raise ValueError('Unknown installation policy')
    if install_policy and any(request.get('mods', {}).get(k) for k in ('vbmeta_flags', 'unlocked_avb')):
        raise ValueError('Installation policy conflicts with AVB modification switches')
    if install_policy:
        from .unlocked_avb import generator_tool
        generator_tool()
    a, b = [source_summary(request[role]) for role in ('base', 'donor')]
    adapter_path = Path(request['adapter']) if request.get('adapter') else ROOT / 'profiles/desktop-pjf110-cph2745.json'
    adapter = read_json(adapter_path)
    if adapter.get('schema') != 1 or adapter.get('kind') != 'desktop-port-adapter':
        raise ValueError('Unsupported device adapter')
    if a['identity'] and b['identity']:
        try:
            check_identity(adapter, a['identity'], b['identity'])
        except ValueError:
            if request.get('adapter'):
                raise
            raise ValueError('Automatic port rules are not yet implemented for this ROM pair. '
                             'Run ROM analysis to identify missing transformations; a manual adapter is optional.') from None
    elif not request.get('adapter'):
        raise ValueError('Run ROM analysis to extract OTA identities before automatic rule selection.')
    output = Path(request['output']).resolve()
    if output.exists():
        raise ValueError('Output folder must be new')
    work_root = Path(request.get('work_root') or ROOT / 'work').resolve()
    if work_root == output or work_root.is_relative_to(output):
        raise ValueError('Workspace cannot be inside output')
    roots = [Path(request[key]).resolve(strict=True) for key in ('base', 'donor')]
    roots += [p.resolve(strict=True) for p in replacement_paths(request.get('replacements', []))]
    roots += [Path(p).resolve(strict=True) for p in request.get('apks', [])]
    roots += [Path(request[key]).resolve(strict=True) for key in ('boot_animation', 'reboot_animation', 'debloat_list')
              if request.get(key)]
    roots += [Path(request[key]).resolve(strict=True) for key in ('base_cache', 'donor_cache') if request.get(key)]
    for root in roots:
        if output == root or output.is_relative_to(root) or root.is_relative_to(output):
            raise ValueError('Output overlaps protected input')
        if work_root == root or work_root.is_relative_to(root):
            raise ValueError('Workspace root overlaps protected input')
    replacements = replacement_map(request.get('replacements', []))
    if request.get('mods', {}).get('unlocked_avb'):
        from .unlocked_avb import generator_tool
        generator_tool()
        if set(replacements) & {'vbmeta', 'vbmeta_system', 'vbmeta_vendor'}:
            raise ValueError('IMG C vbmeta conflicts with selected unlocked AVB policy')
    if a['images'] and set(replacements) - set(a['images']):
        raise ValueError('C can replace only partitions already present in A layout')
    enabled = request.get('mods', {})
    if request.get('apks') and not enabled.get('apps'):
        raise ValueError('APKs selected while APK switch is off')
    if enabled.get('apps') and not request.get('apks'):
        raise ValueError('Select at least one standalone APK')
    app_infos = inspect_apks(request.get('apks', []), request.get('target_sdk', 36))
    for name in ('boot_animation', 'reboot_animation'):
        if enabled.get(name):
            validate_animation(Path(request.get(name, '')))
    if enabled.get('debloat'):
        read_debloat(Path(request.get('debloat_list', '')))
    changed = {p.split('/')[0] for p in adapter['hardware_files']} | {'my_manifest', 'my_product', 'odm'}
    v14_plan = None
    if enabled.get('v14_bundle'):
        from . import v14
        if not install_policy:
            raise ValueError('v1.4 candidate requires fresh unlocked installation metadata')
        v14_plan = v14.inspect(a, b)
        changed |= set(v14_plan['repacked_partitions'])
    if install_policy:
        changed |= (set(a['images']) & (OS_PARTITIONS | {'vendor', 'odm'}))
        if set(replacements) & (changed | {'vbmeta', 'vbmeta_system', 'vbmeta_vendor'}):
            raise ValueError('Fresh AVB build cannot discard core repacks through IMG C')
    if enabled.get('apps'):
        changed.add(adapter['app_partition'])
    for name in ('boot_animation', 'reboot_animation'):
        if enabled.get(name):
            changed.add(adapter[name + '_target'].split('/')[0])
    if enabled.get('debloat'):
        changed |= {p.split('/')[0] for p in read_debloat(Path(request['debloat_list']))}
    mod_targets = set()
    if enabled.get('apps'):
        mod_targets.add(adapter['app_partition'])
    for name in ('boot_animation', 'reboot_animation'):
        if enabled.get(name):
            mod_targets.add(adapter[name + '_target'].split('/')[0])
    if enabled.get('debloat'):
        mod_targets |= {p.split('/')[0] for p in read_debloat(Path(request['debloat_list']))}
    if mod_targets & set(replacements):
        raise ValueError('IMG C would discard selected mod/APK changes: ' + ', '.join(sorted(mod_targets & set(replacements))))
    for name in ('extract.erofs', 'mkfs.erofs'):
        executable(name)
    floor = 0
    if a['images'] and b['images']:
        for name, info in a['images'].items():
            if name in PRIVATE_PARTITIONS or name in changed and name not in replacements:
                continue
            if name in replacements:
                floor += replacements[name].stat().st_size
            else:
                source = b['images'] if name in OS_PARTITIONS else a['images']
                if name not in source:
                    raise ValueError('B lacks required OS partition: ' + name)
                floor += source[name]['size']
    parent = output.parent
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    return {'schema': 1, 'status': 'planned', 'base': a, 'donor': b, 'adapter': adapter['id'],
            'installation_policy': install_policy,
            'adapter_path': str(adapter_path.resolve()),
            'mods': {name: bool(enabled.get(name)) for name in MODS}, 'apks': app_infos,
            'replacements': {name: str(path) for name, path in replacements.items()},
            'output': str(output), 'work_root': str(work_root), 'changed_partitions': sorted(changed),
            'space': {'minimum_copy_bytes': floor, 'output_free_bytes': free,
                      'enough_for_copy_floor': free >= floor,
                      'note': 'Minimum excludes ZIP extraction, EROFS stage/repack/verification; actual required space is higher.'},
            'warnings': ['Hardware/runtime compatibility requires user device tests.',
                         ('Fresh unsigned footers/child metadata will be rebuilt for unlocked testing; no OEM authentication.' if install_policy
                          else 'Repacked IMG are unsigned; source metadata may block installation.'),
                         ('v1.4 adds my_company/my_preload from the pinned recipe; the flasher requires existing logical partitions.' if v14_plan
                          else 'Output preserves A layout; donor-only partitions are not silently created.'),
                         'Explicit C overrides core changes in: ' + ', '.join(sorted(changed & set(replacements)))],
            'v14_bundle': v14_plan,
            'device_validated': False, 'flash_ready': False, 'avb_preparation_required': True}


def check_identity(adapter, a, b):
    for role, actual in (('base', a), ('donor', b)):
        for key, value in adapter['identities'][role].items():
            if actual.get(key) != value:
                raise ValueError(f'{role}: adapter requires {key}={value}, got {actual.get(key)}. '
                                 'A new pair requires an evidence-backed adapter.')


def replacement_paths(paths):
    return [Path(value['path'] if isinstance(value, dict) else value) for value in paths]


def replacement_map(paths):
    result = {}
    for value in paths:
        if isinstance(value, dict) and set(value) != {'path', 'partition'}:
            raise ValueError('IMG selection needs a path and partition')
        p = Path(value['path'] if isinstance(value, dict) else value).resolve(strict=True)
        name = value['partition'] if isinstance(value, dict) else p.stem
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_]+', name):
            raise ValueError('Invalid replacement partition')
        if p.suffix.lower() != '.img' or not p.is_file() or name in result:
            raise ValueError('C needs unique partition-named IMG files')
        info = inspect_image(p)
        if info['format'] == 'unknown':
            raise ValueError(f'Unrecognized replacement image: {p.name}')
        result[name] = p
    return result


def check_replacement_types(images, replacements):
    for name, replacement in replacements.items():
        original = inspect_image(images[name])['format']
        selected = inspect_image(replacement)['format']
        if original in ('erofs', 'ext', 'android-boot', 'android-vendor-boot', 'avb-vbmeta') and selected not in (original, 'android-sparse'):
            raise ValueError(f'IMG C filesystem/header type differs from A: {name}: {original} → {selected}')
        if original in ('android-boot', 'android-vendor-boot', 'avb-vbmeta') and selected != original:
            raise ValueError('Boot/vbmeta replacement must preserve raw header type')


def inspect_apks(paths, target_sdk=36):
    result, packages = [], set()
    for value in paths:
        path = Path(value).resolve(strict=True)
        info = apk_info(path)
        package = info.get('package', '')
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package):
            raise ValueError('Invalid APK package name')
        from .android import binary_manifest
        with zipfile.ZipFile(path) as archive:
            tags = binary_manifest(archive.read('AndroidManifest.xml'))['tags']
        manifest = next(x['attributes'] for x in tags if x['tag'] == 'manifest')
        if manifest.get('split') or manifest.get('sharedUserId') or info['application'].get('isSplitRequired'):
            raise ValueError(f'{package}: split/shared UID APK needs a separate supported installer')
        if package in packages:
            raise ValueError(f'Duplicate selected package: {package}')
        if target_sdk is not None and int(info['sdk'].get('minSdkVersion', 1)) > target_sdk:
            raise ValueError(f'{package}: requires SDK {info["sdk"]["minSdkVersion"]}, ROM provides {target_sdk}')
        packages.add(package)
        result.append(info)
    return result


def validate_animation(path):
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo('desc.txt').file_size > 65536:
            raise ValueError('Oversized animation descriptor')
        lines = [line.strip() for line in archive.read('desc.txt').decode('utf-8-sig').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if not lines:
            raise ValueError('Empty animation descriptor')
        header = lines[0].split()
        offsets = None
        if len(header) == 6 and header[0] == 'g':
            offsets = [int(v) for v in header[3:5]]
            header = [header[1], header[2], header[5]]
        if len(header) != 3 or not all(v.isdecimal() and 0 < int(v) <= 10000 for v in header):
            raise ValueError('Animation needs width height fps descriptor')
        parts = []
        for line in lines[1:]:
            if not line.strip() or line.startswith(('#', 'g ')):
                continue
            fields = line.split()
            if len(fields) < 4 or fields[0] not in ('p', 'c') or not all(v.isdecimal() for v in fields[1:3]):
                raise ValueError('Unsupported animation descriptor')
            parts.append(fields[3])
        if not parts or any(not any(n.startswith(part + '/') and n.endswith('.png') for n in archive.namelist()) for part in parts):
            raise ValueError('Missing animation PNG frames')
        for item in archive.infolist():
            if not item.is_dir():
                safe_zip_name(item.filename)
        return {'dimensions': list(map(int, header)), 'parts': parts, 'offsets': offsets}


def read_debloat(path):
    paths = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Debloat list must contain unique app directory paths')
    for p in paths:
        if not re.fullmatch(r'(?:system/system|system_ext|product|my_product|my_region|my_stock)/(?:app|priv-app)/[A-Za-z0-9_.-]+', p):
            raise ValueError(f'Debloat accepts explicit app directory paths only: {p}')
    return paths


def copy_tree(source, target, progress=None):
    android_directory(target)
    paths = list(android_walk(source))
    directories = [source] + [p for p in paths if p.is_dir() and not p.is_symlink()]
    for p in directories[1:]:
        directory = target / p.relative_to(source)
        directory.mkdir(parents=True, exist_ok=True)
        enable_case_sensitive(directory)
    for index, p in enumerate(paths):
        if not p.is_dir() or p.is_symlink():
            copy_entry(p, target / p.relative_to(source), p.lstat().st_mtime_ns)
        if progress and index % 500 == 0:
            progress(f'{source.name}: {index}/{len(paths)} entries')
    for p in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        os.utime(target / p.relative_to(source), ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns))


def android_walk(root):
    """Windows Path.rglob deduplicates case-distinct Paths; Android must not."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            yield Path(directory) / name


def metadata_records(config, partition, suffix):
    return (config / f'{partition}_{suffix}').read_text(encoding='utf-8').splitlines()


def metadata_key(line, suffix):
    key = line.split()[0]
    return re.sub(r'\\(.)', r'\1', key) if suffix == 'file_contexts' else key


def transfer_metadata(base_config, config, relative):
    part = relative.split('/')[0]
    for suffix in ('fs_config', 'file_contexts'):
        prefix = relative if suffix == 'fs_config' else '/' + relative
        source = [line for line in metadata_records(base_config, part, suffix) if metadata_key(line, suffix) == prefix]
        if len(source) != 1:
            raise ValueError(f'Missing/ambiguous source Android metadata: {relative}:{suffix}')
        target = config / f'{part}_{suffix}'
        lines = [line for line in metadata_records(config, part, suffix) if metadata_key(line, suffix) != prefix]
        target.write_text('\n'.join(lines + source) + '\n', encoding='utf-8')


def add_metadata(config, relative, directory=False):
    part = relative.split('/')[0]
    for suffix, line in (('fs_config', f'{relative} 0 0 {"0755" if directory else "0644"}'),
                         ('file_contexts', f'/{re.escape(relative)} u:object_r:system_file:s0')):
        path = config / f'{part}_{suffix}'
        # New app names contain dots; file_contexts needs literal matching.
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')


def verify_roundtrip(stage, unpacked, partition, extracted_name=None):
    extracted_name = extracted_name or partition
    def entries(root, name):
        result = {partition: ('dir', None)}
        if not (root / name).is_dir():
            raise ValueError(f'Missing extracted partition: {name}')
        for p in android_walk(root / name):
            key = partition + '/' + p.relative_to(root / name).as_posix()
            result[key] = ('dir', None) if p.is_dir() and not p.is_symlink() else ('file', entry_digest(p))
        return result
    expected, actual = entries(stage, partition), entries(unpacked, extracted_name)
    if expected != actual:
        raise ValueError(f'{partition}: content/link/empty-directory round-trip failed')
    for suffix in ('fs_config', 'file_contexts'):
        def normalized(root, name):
            # lstat context names in extractor use escaped regex strings.
            result = set()
            for line in metadata_records(root / 'config', name, suffix):
                if not line.strip():
                    continue
                key = metadata_key(line, suffix)
                # Extractor names the mount from the IMG filename, not EROFS volume name.
                key = re.sub(r'^(/?)' + re.escape(name) + r'(?=/|$)', lambda m: m[1] + partition, key)
                result.add((key, *line.split()[1:]))
            return result
        if normalized(stage, partition) != normalized(unpacked, extracted_name):
            raise ValueError(f'{partition}: Android {suffix} round-trip failed')
    return {'partition': partition, 'extracted_name': extracted_name, 'status': 'pass', 'entries': len(expected),
            'checks': ['content', 'links', 'empty directories', 'uid/gid/mode/capabilities', 'SELinux']}


def add_apps(stage, infos, app_partition, existing_trees):
    existing = set()
    for tree in existing_trees:
        for apk in (p for p in android_walk(tree) if p.suffix.lower() == '.apk' and p.is_file()):
            if link_target(apk) is None:
                existing.add(apk_info(apk).get('package'))
    conflicts = sorted(app['package'] for app in infos if app['package'] in existing)
    if conflicts:
        raise ValueError('APK package already present in ROM: ' + ', '.join(conflicts))
    root = stage / app_partition / 'app'
    if not root.is_dir():
        raise ValueError('APK target app directory absent')
    for app in infos:
        relative = f'{app_partition}/app/ColorNT_{app["package"]}'
        dest = confined(stage, relative)
        if dest.exists():
            raise ValueError('APK directory collision')
        dest.mkdir()
        copy_file(app['path'], dest / 'base.apk')
        add_metadata(stage / 'config', relative, directory=True)
        add_metadata(stage / 'config', relative + '/base.apk')


def apply_file_mods(stage, request, adapter):
    mods = request.get('mods', {})
    animations = []
    for option in ('boot_animation', 'reboot_animation'):
        if mods.get(option):
            validate_animation(Path(request[option]))
            target = confined(stage, adapter[option + '_target'])
            if not target.is_file() or link_target(target) is not None:
                raise ValueError('Animation target is not an existing regular file')
            animations.append((Path(request[option]), target))
    removed = []
    if mods.get('debloat'):
        for relative in read_debloat(Path(request['debloat_list'])):
            target = confined(stage, relative)
            if not target.is_dir():
                raise ValueError('Debloat target absent: ' + relative)
            removed.append((relative, target))
    # Preflight the entire batch before copying or deleting any staged entries.
    for source, target in animations:
        copy_file(source, target)
    for relative, target in removed:
        shutil.rmtree(target)
        part = relative.split('/')[0]
        for suffix in ('fs_config', 'file_contexts'):
            prefix = relative if suffix == 'fs_config' else '/' + relative
            path = stage / 'config' / f'{part}_{suffix}'
            lines = [line for line in metadata_records(stage / 'config', part, suffix)
                     if metadata_key(line, suffix) != prefix and not metadata_key(line, suffix).startswith(prefix + '/')]
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'animations': [str(target.relative_to(stage)) for _, target in animations],
            'removed': [relative for relative, _ in removed]}


def build(request, progress=lambda msg: None, cancelled=lambda: False):
    request = normalize_installation_request(request)
    plan = inspect_request(request)
    if not plan['space']['enough_for_copy_floor']:
        raise ValueError(f'Insufficient output disk space: {plan["space"]["output_free_bytes"]} free bytes; '
                         f'at least {plan["space"]["minimum_copy_bytes"]} required just for unchanged/C IMG copies, '
                         'plus extraction/staging/repack/verification workspace.')
    request = dict(request, adapter=plan['adapter_path'])
    adapter = read_json(Path(request['adapter']))
    output = Path(plan['output'])
    work = Path(plan['work_root']) / ('desktop-' + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    if request.get('workspace_observer'):
        request['workspace_observer'](work)
    report = dict(plan, status='building', workspace=str(work), images=[], roundtrip=[])
    from .delivery import input_state, source_index, rule_state
    inputs_before, sources_before = input_state(request), source_index()
    report['rule_state'] = rule_state(request['adapter'])
    write_json(work / 'request.json', {k: v for k, v in request.items() if not callable(v)})
    def step(message):
        write_json(work / 'build.json', report)
        if cancelled():
            raise ValueError('Cancelled between stages; partial output is not complete')
        progress(message)
    try:
        step('Chuẩn bị input A/B (giữ nguyên input)')
        a, ai = prepare_source(Path(request['base']), work / 'input-a')
        b, bi = prepare_source(Path(request['donor']), work / 'input-b')
        check_identity(adapter, ai, bi)
        report['base'] = dict(report['base'], identity=ai, prepared_path=str(a))
        report['donor'] = dict(report['donor'], identity=bi, prepared_path=str(b))
        amap, bmap = image_map(a), image_map(b)
        choices = {n: ('donor' if n in OS_PARTITIONS else 'base') for n in amap if n not in PRIVATE_PARTITIONS}
        if set(n for n, role in choices.items() if role == 'donor') - set(bmap):
            raise ValueError('B lacks required A OS partitions')
        replacements = replacement_map(request.get('replacements', []))
        if set(replacements) - set(choices):
            raise ValueError('C is outside exported A partition layout')
        check_replacement_types(amap, replacements)
        changed = set(plan['changed_partitions'])
        v14_images = {}
        if plan.get('v14_bundle'):
            from . import v14
            v14_images = v14.image_entries()
            choices.update({name: 'v14-created' for name in changed | {'my_preload'} if name not in choices})
        if not changed <= choices.keys():
            raise ValueError('Adapter/mod needs partitions absent from A layout')
        base_parts = sorted({p.split('/')[0] for p in adapter['hardware_files']} | {'system', 'my_manifest', 'my_product', 'odm'} |
                            {n for n in changed if choices[n] == 'base'})
        donor_parts = sorted({n for n in changed if choices[n] == 'donor'} | {'system'})
        if v14_images:
            base_parts = sorted(set(base_parts) | v14.parts('base'))
            donor_parts = sorted(set(donor_parts) | v14.parts('donor'))
        for role, folder, names in (('base', a, base_parts), ('donor', b, donor_parts)):
            step('Giải nén Android metadata: ' + role)
            # Explicit cache is permitted only when source/partition set matches exactly.
            cache = request.get(role + '_cache')
            dest = Path(cache).resolve(strict=True) if cache else work / role
            if cache:
                state = read_json(dest / 'extraction.json')
                if state['status'] != 'complete' or Path(state['source']).resolve() != folder.resolve() or not set(names) <= {i['name'] for i in state['partitions']}:
                    raise ValueError('Extraction cache belongs to different input/partitions')
            else:
                state = extract(folder, dest, names, hashes=False)
            if any(i['status'] != 'extracted' for i in state['partitions'] if i['name'] in names):
                raise ValueError('Modified partitions require complete native EROFS metadata; EXT edits are unsupported')
            report[role + '_extracted'] = str(dest)
        afs = Path(report['base_extracted']) / 'fs'
        bfs = Path(report['donor_extracted']) / 'fs'
        stage = work / 'stage'
        if v14_images:
            step('Tái tạo full bundle v1.4 từ nguồn OOS đúng version (không chạy payload)')
            report['v14_stage'] = v14.stage_bundle(afs, bfs, stage, progress=step)
        else:
            android_directory(stage)
            (stage / 'config').mkdir()
            step('Đang ghép hệ điều hành B với cấu hình phần cứng A…')
            for name in sorted(changed):
                origin = afs if choices[name] == 'base' else bfs
                copy_tree(origin / name, stage / name, progress=step)
                for suffix in ('fs_config', 'file_contexts', 'fs_options'):
                    src = origin / 'config' / f'{name}_{suffix}'
                    if src.is_file():
                        shutil.copyfile(src, stage / 'config' / src.name)
            for relative in adapter['hardware_files']:
                source, dest = entry_path(afs, relative), entry_path(stage, relative)
                if not source.is_file() or link_target(source) is not None or dest.is_dir() or link_target(dest) is not None:
                    raise ValueError(f'Hardware transplant requires regular files: {relative}')
                parents = []
                parent = dest.parent
                while not parent.exists():
                    parents.append(parent)
                    parent = parent.parent
                for parent in reversed(parents):
                    rel = parent.relative_to(stage).as_posix()
                    parent.mkdir()
                    transfer_metadata(afs / 'config', stage / 'config', rel)
                copy_entry(source, dest, source.stat().st_mtime_ns)
                transfer_metadata(afs / 'config', stage / 'config', relative)
        report['properties'] = port_properties(stage, afs, bfs, read_json(ROOT / adapter['property_policy']),
                                               allow_applied=bool(v14_images))
        mods = request.get('mods', {})
        if mods.get('apps'):
            step('Thêm APK thường vào ROM (không cấp quyền privileged/platform)')
            existing_trees = []
            for name in choices:
                if name not in OS_PARTITIONS | {'vendor', 'odm'}:
                    continue
                if name in changed:
                    existing_trees.append(stage / name)
                    continue
                origin = afs if choices[name] == 'base' else bfs
                if not (origin / name).is_dir():
                    scan = work / ('apk-scan-' + name)
                    state = extract((amap if choices[name] == 'base' else bmap)[name], scan, hashes=False)
                    if state['partitions'][0]['status'] not in ('extracted', 'extracted-content-only'):
                        raise ValueError('Cannot check APK package collisions in ' + name)
                    origin = scan / 'fs'
                existing_trees.append(origin / name)
            add_apps(stage, plan['apks'], adapter['app_partition'], existing_trees)
        report['file_mods'] = apply_file_mods(stage, request, adapter)
        if request.get('installation_policy'):
            from .port_contract import check_staged_contract
            report['port_contract'] = check_staged_contract(stage)
        if v14_images:
            report['v14_modules'] = v14.module_evidence(stage)
            step('Compile CIL v1.4 đúng stage tại policy33; không inject policy vào ROM')
            report['v14_selinux'] = v14.compile_policy(stage, work)
        output.mkdir(parents=True)
        output_paths = {}
        for name, role in sorted(choices.items()):
            step('Đóng IMG: ' + name)
            dest = output / (amap[name].relative_to(a) if name in amap else v14_images[name]['output'])
            output_paths[name] = dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            if v14_images.get(name, {}).get('kind') == 'asset':
                source = replacements.get(name) or v14.assets() / v14_images[name]['asset']
                if name not in replacements and sha256(source) != v14_images[name]['reference_sha256']:
                    raise ValueError('v1.4 opaque IMG checksum mismatch: ' + name)
            else:
                source = replacements.get(name) or (amap if role == 'base' else bmap).get(name)
            operation = 'C replacement' if name in replacements else role + ' unchanged'
            if name in changed and name not in replacements:
                header = (inspect_image(source) if source is not None else
                          {'format': 'erofs', 'build_time': v14_images[name]['timestamp'], 'uuid': v14_images[name]['uuid']})
                if header['format'] == 'android-sparse':
                    # The extractor records the expanded header without rehashing input.
                    state = read_json(Path(report[role + '_extracted']) / 'extraction.json')
                    header = next(i['expanded_image'] for i in state['partitions'] if i['name'] == name)
                if header['format'] != 'erofs':
                    raise ValueError('Only EROFS rebuilding is supported')
                settings = (stage / 'config' / f'{name}_fs_options').read_text(encoding='utf-8')
                cluster = re.search(r'(?:^|\s)-C\s*(\d+)', settings)
                packed = work / 'packed' / (name + '.img')
                packed.parent.mkdir(parents=True, exist_ok=True)
                repack_erofs(stage / name, stage / 'config', packed, header['build_time'],
                             str(uuid.UUID(header['uuid'])), preserve_mtime=True,
                             cluster_size=int(cluster.group(1)) if cluster else 4096,
                             log_path=work / (name + '.repack.log'))
                if request.get('installation_policy'):
                    from .test_avb import add_footer
                    step('Dựng hashtree/footer mới, unsigned: ' + name)
                    report.setdefault('fresh_avb_footers', []).append(add_footer(packed, name, work / 'fresh-avb.log'))
                step('Kiểm chứng extract/repack/extract: ' + name)
                unpack = work / ('verify-' + name)
                extract(packed, unpack, hashes=False)
                report['roundtrip'].append(verify_roundtrip(stage, unpack / 'fs', name))
                if request.get('cleanup_verification'):
                    cleanup_verified_tree(work, unpack)
                copy_file(packed, dest)
                packed.unlink()
                operation = role + ' EROFS rebuilt with core/selected mods'
            else:
                copy_file(source, dest)
            # Windows may merge a recipe's "system" directory into A's
            # existing "SYSTEM". Freeze the on-disk spelling, also for added
            # partitions, so delivery compares the same canonical inventory.
            dest = dest.resolve(strict=True)
            output_paths[name] = dest
            if mods.get('vbmeta_flags') and name.startswith('vbmeta'):
                with dest.open('r+b') as stream:
                    if stream.read(4) != b'AVB0':
                        raise ValueError('vbmeta flags require raw AVB0 image')
                    stream.seek(120)
                    stream.write(struct.pack('>I', 3))
                operation += '; flags=3'
            info = inspect_image(dest)
            report['images'].append({'partition': name, 'path': dest.relative_to(output).as_posix(),
                                     'operation': operation, 'image': info,
                                     'avb': inspect_for_gate(dest),
                                     'source_has_avb_footer': bool(source and inspect_image(source).get('avb_footer'))})
        # Only explicitly budgeted dynamic partitions are counted; no size heuristic for boot/firmware.
        dynamic_parts = set(adapter['dynamic_partitions']) | ({'my_company', 'my_preload'} if v14_images else set())
        used = sum(i['image'].get('expanded_size', i['image']['size']) for i in report['images']
                   if i['partition'] in dynamic_parts)
        if used > adapter['dynamic_capacity']:
            raise ValueError(f'Dynamic partition budget exceeded: {used}/{adapter["dynamic_capacity"]}')
        report['budget'] = {'used': used, 'capacity': adapter['dynamic_capacity']}
        if v14_images:
            from .ota import check_groups
            budget = v14.recipe()['dynamic_budget']
            sizes = {i['partition']: i['image'].get('expanded_size', i['image']['size']) for i in report['images']
                     if i['partition'] in dynamic_parts}
            report['dynamic_groups'] = check_groups(budget['groups'], sizes, budget['extra_assignments'])
            report['dynamic_layout_note'] = budget['assumption']
        elif adapter.get('dynamic_groups'):
            from .ota import check_groups
            sizes = {i['partition']: i['image'].get('expanded_size', i['image']['size']) for i in report['images']
                     if i['partition'] in adapter['dynamic_partitions']}
            report['dynamic_groups'] = check_groups(adapter['dynamic_groups'], sizes)
        metadata = dict(ai)
        metadata.update(version_name=ai['product_name'] + '_' + bi['version_name'].split('_', 1)[-1],
                        colornt_donor=bi, colornt_unsigned=True, colornt_device_validated=False)
        # Use A's firmware_info location; never import or execute scripts from inputs.
        info_path = next(a.rglob('firmware_info.json'))
        write_json(output / info_path.relative_to(a), metadata)
        if request.get('installation_policy'):
            from .test_avb import prepare_children
            exported = output_paths
            report['avb_policy'] = prepare_children(exported, work / 'fresh-avb.log')
            for row in report['images']:
                if row['partition'].startswith('vbmeta'):
                    dest = exported[row['partition']]
                    row.update(image=inspect_image(dest), avb=inspect_for_gate(dest),
                               operation='fresh unsigned AVB metadata for explicit unlocked lab installation')
        elif mods.get('unlocked_avb'):
            from .unlocked_avb import prepare_top_level
            top = output / amap['vbmeta'].relative_to(a)
            report['avb_policy'] = prepare_top_level(top, work / 'avb-policy.log')
            row = next(i for i in report['images'] if i['partition'] == 'vbmeta')
            row.update(image=inspect_image(top), avb=inspect_for_gate(top),
                       operation='explicit unsigned top-level AVB policy for unlocked testing')
        from .package_validation import validate_records
        report['package_validation'] = validate_records(report['images'], set(amap) - PRIVATE_PARTITIONS, True,
                                                        report['roundtrip'], changed - set(replacements), report.get('avb_policy'))
        report['status'] = report['package_validation']['status']
        report['avb_preparation_required'] = not report['package_validation']['experimental_package_ready']
        report['excluded_device_data'] = sorted(set(amap) & PRIVATE_PARTITIONS)
        from .flash_bundle import bundle_info, bundle_output
        report['flash_bundle'] = bundle_info(report)
        write_json(output.parent / (output.name + '.build.json'), report)
        write_json(work / 'build.json', report)
        if report['status'] == 'experimental-package-ready':
            from .delivery import export_candidate
            report['delivery'] = export_candidate(report, request, stage, inputs_before, sources_before, step, base_folder=a,
                                                  cancelled=cancelled)
        progress('Package sẵn sàng thử trên máy unlocked; chưa kiểm chứng thiết bị.' if report['status'] == 'experimental-package-ready'
                 else 'Filesystem đã kiểm chứng; package bị chặn bởi điều kiện AVB/chính sách chưa đạt. Xem Chi tiết.')
        bundle_output(output, report)
        return report
    except Exception as exc:
        report.update(status='cancelled' if cancelled() else 'failed', error=str(exc))
        write_json(work / 'build.json', report)
        if output.exists():
            write_json(output.parent / (output.name + '.build.json'), report)
        raise


def cleanup_verified_tree(work, unpack):
    """Remove only this build's successful verification copy; retain metadata."""
    work, unpack = Path(work).resolve(strict=True), Path(unpack).resolve(strict=True)
    if unpack.parent != work or not unpack.name.startswith('verify-'):
        raise ValueError('Verification cleanup target is outside its build')
    if not (work / 'build.json').is_file():
        raise ValueError('Verification cleanup needs an owned build report')
    for current, dirs, files in os.walk(unpack, followlinks=False):
        for name in dirs:
            p = Path(current) / name
            if p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked verification directory cannot be cleaned')
    archive = work / 'verification-records' / unpack.name
    archive.mkdir(parents=True, exist_ok=False)
    for p in unpack.iterdir():
        if p.is_file() and p.suffix in ('.json', '.log'):
            shutil.copyfile(p, archive / p.name)
    config = unpack / 'fs/config'
    if config.is_dir():
        shutil.copytree(config, archive / 'config')
    shutil.rmtree(unpack)
