"""Freeze locally built candidates; never infer device acceptance or run devices."""
import csv
import datetime
import io
import hashlib
import json
from pathlib import Path

from .android_links import link_target
from .io import before_write, copy_file, read_json, sha256, write_json
from .tools import ROOT


def input_state(request):
    """Size/time preservation check, not input content verification."""
    from .desktop_build import image_map
    state = {}
    for role in ('base', 'donor'):
        source = Path(request[role]).resolve(strict=True)
        paths = [source] if source.is_file() else [*image_map(source).values(), *source.rglob('firmware_info.json')]
        state[role] = {str(p.relative_to(source) if source.is_dir() else p.name):
                       {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
                       for p in paths}
    return state


def source_index():
    paths = [*(ROOT / 'colornt').glob('*.py'), ROOT / 'toolbuild.py', ROOT / 'colornt_gui.py',
             ROOT / 'scripts/desktop_build.py', ROOT / 'scripts/flash_from_bootloader.py',
             ROOT / 'tools/lock.json', ROOT / 'tools/local/avb/avbtool.py']
    paths += list((ROOT / 'tools/local/erofs-tools').glob('*.exe'))
    paths += list((ROOT / 'tools/local/erofs-tools').glob('*.dll'))
    paths += list((ROOT / 'tools/local/payload').glob('*.exe'))
    paths += [ROOT / 'tools/local/policy-runtime/secilc-static', ROOT / 'tools/local/policy-runtime/secilc.c']
    paths += list((ROOT / 'profiles/evidence').glob('*.json'))
    return {p.relative_to(ROOT).as_posix(): {'size': p.stat().st_size, 'sha256': sha256(p)}
            for p in sorted(paths) if p.is_file()}


def rule_state(adapter_path):
    adapter = read_json(Path(adapter_path))
    return {'adapter_sha256': sha256(Path(adapter_path)),
            'property_policy_sha256': sha256(ROOT / adapter['property_policy'])}


def image_digest(path, cancelled):
    from .tools import JOB_GUARD
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            if cancelled():
                raise ValueError('Cancelled during candidate freeze')
            guard = JOB_GUARD.get()
            if guard:
                guard()
            digest.update(chunk)
    return digest.hexdigest()


def _json(path, value):
    before_write(path, len(json.dumps(value, ensure_ascii=False).encode('utf-8')) * 2 + 4096)
    write_json(path, value)


def _text(path, value):
    before_write(path, len(value.encode('utf-8')))
    path.write_text(value, encoding='utf-8')


CHECKER = '''import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
if digest(root/'BUILD-MANIFEST.json')!=EXPECTED:sys.exit('FAIL: manifest changed')
m=json.loads((root/'BUILD-MANIFEST.json').read_text(encoding='utf-8'))
for r in m['images']:
    p=(root/r['path']).resolve(strict=True)
    if not p.is_relative_to(root) or p.stat().st_size!=r['size'] or digest(p)!=r['sha256']:sys.exit('FAIL: '+r['path'])
for name,expected in m['metadata'].items():
    p=(root/name).resolve(strict=True)
    if not p.is_relative_to(root) or digest(p)!=expected:sys.exit('FAIL: '+name)
print('PASS candidate identity/files; boot/hardware NOT_TESTED: '+m['candidate_id'])
'''


def export_candidate(report, request, stage, inputs_before, sources_before, progress=lambda msg: None, base_folder=None,
                     cancelled=lambda: False):
    from .desktop_build import android_walk, image_map
    from .package_validation import validate_folder
    output = Path(report['output']).resolve(strict=True)
    if (output / 'BUILD-MANIFEST.json').exists() or (output / 'DELIVERY.json').exists():
        raise ValueError('Candidate delivery already exists; refusing to overwrite')
    if input_state(request) != inputs_before:
        raise ValueError('Protected A/B input size/mtime changed during build')
    if source_index() != sources_before:
        raise ValueError('Build source/tools changed during build; candidate not frozen')
    if rule_state(report['adapter_path']) != report['rule_state']:
        raise ValueError('Port rules changed during build; candidate not frozen')
    rebuilt = [r['partition'] for r in report['roundtrip']]
    gate = validate_folder(output, base_folder or report['base']['path'], report['roundtrip'], rebuilt, report.get('avb_policy'))
    if report['status'] != 'experimental-package-ready' or gate['status'] != report['status']:
        raise ValueError('Independent delivery package gate did not pass')
    actual = image_map(output)
    recorded = {r['partition']: r for r in report['images']}
    if len(recorded) != len(report['images']) or actual.keys() != recorded.keys():
        raise ValueError('Candidate image inventory differs from build report')
    for name, image in actual.items():
        if image.relative_to(output).as_posix() != recorded[name]['path'] or image.stat().st_size != recorded[name]['image']['size']:
            raise ValueError('Candidate image path/size differs from report: ' + name)
    adapter = read_json(Path(report['adapter_path']))
    stage = Path(stage)
    hardware = set(adapter['hardware_files'])
    properties = {r['path'] for r in report.get('properties', {}).get('files', [])}
    animations = set(report.get('file_mods', {}).get('animations', []))
    reference_map = {}
    if report.get('v14_bundle'):
        from .v14 import source_map
        reference_map = source_map()
    csv_text = io.StringIO(newline='')
    writer = csv.writer(csv_text)
    writer.writerow(['path', 'kind', 'source_role', 'source_path', 'transformation'])
    counts = {'files_links': 0, 'directories': 0}
    for part in sorted(rebuilt):
        origin_role = 'base' if part in ('vendor', 'odm') else 'donor'
        origin = Path(report[origin_role + '_extracted']) / 'fs'
        for path in [stage / part, *android_walk(stage / part)]:
            relative = path.relative_to(stage).as_posix()
            kind = 'symlink' if link_target(path) is not None else 'directory' if path.is_dir() else 'file'
            counts['directories' if kind == 'directory' else 'files_links'] += 1
            role, transformation = origin_role, 'unchanged payload'
            if relative in hardware or (kind == 'directory' and not (origin / relative).exists() and
                                        any(h.startswith(relative + '/') for h in hardware)):
                role, transformation = 'base', 'target hardware transplant'
            if relative in properties:
                transformation = 'core property operations'
            elif relative in animations:
                role, transformation = 'selected-option', 'selected animation'
            elif kind != 'directory' and relative not in hardware and not (origin / relative).exists():
                role, transformation = 'selected-option-or-generated', 'added by selected option/core'
            source_path = relative
            if relative in reference_map and relative not in animations:
                row = reference_map[relative]
                role, source_path, transformation = row['source_role'], row['source_path'], row['transformation']
                if relative in properties:
                    transformation += '; core property operations'
            writer.writerow([relative, kind, role, source_path, transformation])
    _text(output / 'SOURCE-MAP.csv', csv_text.getvalue())
    _json(output / 'BUILD-REQUEST.json', {k: v for k, v in request.items() if not callable(v)})
    _json(output / 'BUILD-REPORT.json', report)
    from .reference_qa import staged_reference_checks
    supplemental = staged_reference_checks(stage, report['base']['identity'], report['donor']['identity'])
    if report.get('v14_selinux'):
        # This result belongs to the current stage, not inherited device success.
        supplemental['host_cil_compile'] = report['v14_selinux']
        supplemental['v14_modules'] = report['v14_modules']
        copy_file(Path(report['workspace']) / 'v14-host-policy-neverallow.log', output / 'V14-POLICY-NEVERALLOW.log')
    _json(output / 'SUPPLEMENTAL-EVIDENCE.json', supplemental)
    snapshot = output / 'BUILD-SOURCE'
    snapshot.mkdir()
    # Copy only code, actual rules and pinned build dependencies, no ROM inputs.
    source_files = {}
    for relative, expected in sources_before.items():
        source = ROOT / relative
        if sha256(source) != expected['sha256']:
            raise ValueError('Source changed during snapshot: ' + relative)
        copy_file(source, snapshot / relative)
        source_files[relative] = expected
    copy_file(Path(report['adapter_path']), snapshot / 'PORT-ADAPTER.json')
    copy_file(ROOT / adapter['property_policy'], snapshot / 'PROPERTY-POLICY.json')
    for name in ('PORT-ADAPTER.json', 'PROPERTY-POLICY.json'):
        path = snapshot / name
        source_files[name] = {'size': path.stat().st_size, 'sha256': sha256(path)}
    if report.get('v14_bundle'):
        from .v14 import recipe as check_v14_recipe, RECIPE
        check_v14_recipe()
        copy_file(ROOT / RECIPE, snapshot / 'V14-RECIPE.json')
        source_files['V14-RECIPE.json'] = {'size': (snapshot / 'V14-RECIPE.json').stat().st_size,
                                         'sha256': sha256(snapshot / 'V14-RECIPE.json')}
    _json(snapshot / 'INDEX.json', {'schema': 1, 'files': source_files})
    images = []
    for name, image in sorted(actual.items()):
        progress('Ghim checksum IMG output: ' + name)
        images.append({'partition': name, 'path': image.relative_to(output).as_posix(),
                       'size': image.stat().st_size, 'sha256': image_digest(image, cancelled)})
    metadata_paths = [output / n for n in ('SOURCE-MAP.csv', 'BUILD-REQUEST.json', 'BUILD-REPORT.json', 'SUPPLEMENTAL-EVIDENCE.json', 'BUILD-SOURCE/INDEX.json')]
    metadata_paths += [output / 'BUILD-SOURCE' / relative for relative in source_files]
    metadata_paths += list(output.rglob('firmware_info.json'))
    if report.get('v14_selinux'):
        metadata_paths.append(output / 'V14-POLICY-NEVERALLOW.log')
    manifest = {'schema': 1, 'candidate_id': output.name, 'state': 'AWAITING_USER',
                'created_at': datetime.datetime.now().astimezone().isoformat(),
                'base': report['base']['identity'], 'donor': report['donor']['identity'],
                'direction': 'B OS to A hardware', 'mods': report['mods'],
                'apks': report['apks'], 'replacements': report['replacements'],
                'installation_policy': report.get('installation_policy'), 'avb_policy': report.get('avb_policy'),
                'v14_bundle': report.get('v14_bundle'),
                'v14_modules': report.get('v14_modules'),
                'fresh_avb_footers': report.get('fresh_avb_footers', []),
                'images': images, 'IMG_count': len(images), 'IMG_bytes': sum(r['size'] for r in images),
                'roundtrip': report['roundtrip'], 'staged_counts': counts, 'budget': report['budget'],
                'port_contract': report.get('port_contract'), 'package_validation': gate,
                'input_preservation': {'method': 'size/mtime only; no input whole-ROM hashes', 'unchanged': True, 'sources': inputs_before},
                'source_snapshot_index_sha256': sha256(snapshot / 'INDEX.json'),
                'build_report_sha256': sha256(output / 'BUILD-REPORT.json'),
                'metadata': {p.relative_to(output).as_posix(): sha256(p) for p in metadata_paths},
                'device_test': {'boot': 'NOT_TESTED', 'hardware': 'NOT_TESTED'}, 'production_signed': False,
                'device_mutations': [], 'supplemental_checks': supplemental,
                'limitations': ['Unsigned unlocked lab package; no OEM/locked-device authentication',
                    'Every new output requires exact-candidate user boot/hardware acceptance',
                    'Successful prior pair does not prove other versions/models or selected mods']}
    if input_state(request) != inputs_before or source_index() != sources_before or rule_state(report['adapter_path']) != report['rule_state']:
        raise ValueError('Source/input identity changed during candidate freeze')
    _json(output / 'BUILD-MANIFEST.json', manifest)
    digest = sha256(output / 'BUILD-MANIFEST.json')
    _text(output / 'CANDIDATE-ID.txt', output.name + '\nBUILD-MANIFEST SHA256 ' + digest + '\n')
    _text(output / 'Check-Candidate.py', 'EXPECTED=' + repr(digest) + '\n' + CHECKER)
    checklist = ('# Candidate test checklist\n\nCandidate: `' + output.name + '`\n\nManifest SHA256: `' + digest + '`\n\n'
        'Boot/hardware NOT_TESTED. This new output does not inherit acceptance of another build.\n\n'
        '1. Run `python Check-Candidate.py` locally; it must PASS. Back up personal data.\n'
        '2. User installs on the correct unlocked device; Flash-ROM.bat menu3 is offline, menu4 read-only.\n'
        '3. Choose keep data/wipe explicitly. Wipe deletes personal data; the builder never runs device tools.\n'
        '4. Check Android boots/setup, call/SMS/mobile data, Wi-Fi/Bluetooth/GPS.\n'
        '5. Check all cameras/audio, biometrics, charging/USB, screen/touch and UI.\n'
        '6. Check reboot, sleep/wake, battery/thermal/stability and any selected APK/mods.\n'
        '7. Report this candidate ID/manifest, failures, last screen and full flash log.\n\n'
        'Offline limits: full VINTF/kernel/APEX/linker solver is NOT_RUN; inspect SUPPLEMENTAL-EVIDENCE.json '
        'for exact-byte reuse or NOT_RUN of host CIL/APK checks. Keep historical boot feedback separate.\n')
    if report.get('v14_bundle'):
        checklist += ('\n## Mod v1.4 checks for this candidate\n\n'
            '- Open NTManager and NexAlloy; check version, bridge status and persistence after reboot.\n'
            '- Test InstallerX install/update/uninstall with an APK you own.\n'
            '- Check Settings integration, launcher, SystemUI, themes, animations and clone-app choices.\n'
            '- Test selected Manager tweaks individually, then disable them and confirm restoration.\n'
            '- Report SELinux denials/crashes and any broken hook with exact steps.\n'
            '- Host runtime-style CIL compile passed with -N; build-time neverallow audit may FAIL.\n'
            '  See V14-POLICY-NEVERALLOW.log. No CTS/Integrity/security or OEM policy-load certification.\n'
            '- my_company/my_preload must already exist as logical partitions; no automatic partition creation.\n'
            '  This backend is for these exact A/B versions; generic mod transformers remain unfinished.\n')
    _text(output / 'USER-TEST-CHECKLIST.md', checklist)
    delivery = {'candidate_id': output.name, 'state': 'AWAITING_USER', 'folder': str(output),
                'manifest_sha256': digest, 'gate': gate['status'], 'boot': 'NOT_TESTED', 'hardware': 'NOT_TESTED',
                'IMG_count': len(images), 'IMG_bytes': manifest['IMG_bytes'], 'device_mutations': []}
    _json(output / 'DELIVERY.json', delivery)
    return delivery
