"""Apply the explicitly requested unlocked policy to an existing native output.

Only small top-level vbmeta and reports change; no device commands or ROM hashes.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt.avb import inspect_avb
from colornt.desktop_build import image_map
from colornt.images import inspect_image
from colornt.io import read_json, write_json
from colornt.package_validation import validate_folder
from colornt.tools import ROOT
from colornt.unlocked_avb import prepare_top_level


def prepare(output):
    output = Path(output).resolve(strict=True)
    report_path = Path(str(output) + '.build.json')
    report = read_json(report_path)
    if (report.get('device_test', {}).get('boot') == 'failed' or
            report.get('package_validation', {}).get('device_boot_failed') is True or
            report.get('device_boot_failed') is True):
        raise ValueError('DEVICE BOOT FAILED: AVB preparation cannot clear recorded boot failure. '
                         'Investigate and build a separate corrected candidate; this output stays blocked.')
    base = Path(report['base']['path'])
    if Path(report['output']).resolve() != output:
        raise ValueError('Output/report ownership does not match')
    for item in (base, Path(report['donor']['path'])):
        protected = item.resolve()
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError('Output overlaps a protected source')
    mapping = image_map(output)
    if len(report['images']) != len(mapping) or len({i['partition'] for i in report['images']}) != len(mapping):
        raise ValueError('Output inventory is incomplete')
    for row in report['images']:
        if mapping[row['partition']] != output / row['path'] or mapping[row['partition']].stat().st_size != row['image']['size']:
            raise ValueError('IMG differs from recorded native output: ' + row['partition'])
    rebuilt = [i['partition'] for i in report['images'] if 'EROFS rebuilt' in i['operation']]
    before = validate_folder(output, base, report['roundtrip'], rebuilt)
    if not before['assembled'] or not before['filesystem_verified']:
        raise ValueError('An assembled native filesystem-verified output is required')
    stats = {n: (p.stat().st_size, p.stat().st_mtime_ns) for n, p in mapping.items()}
    top = mapping['vbmeta']
    if top.stat().st_size > 65536:
        raise ValueError('Top-level vbmeta exceeds the metadata-only bound')
    original = top.read_bytes()
    backup = Path(str(output) + '.vbmeta-before-unlocked.img')
    with backup.open('xb') as stream:
        stream.write(original)
    write_json(Path(str(output) + '.pre-unlocked.build.json'), report)
    log = Path(str(output) + '.avb-policy.log')
    try:
        policy = prepare_top_level(top, log)
        policy['target_product_name'] = report['base']['identity']['product_name']
        operations = [op for f in report['properties']['files'] if f['path'] == 'my_manifest/build.prop'
                      for op in f['operations'] if op['key'] == 'ro.product.device']
        if len(operations) != 1:
            raise ValueError('Target userspace device identity is ambiguous')
        policy['target_device'] = operations[0]['after']
        result = validate_folder(output, base, report['roundtrip'], rebuilt, policy)
        if not result['experimental_package_ready']:
            raise ValueError('Policy did not satisfy the unlocked experimental gate')
        for name, path in mapping.items():
            if name != 'vbmeta' and stats[name] != (path.stat().st_size, path.stat().st_mtime_ns):
                raise ValueError('Unexpected change to another IMG: ' + name)
        row = next(i for i in report['images'] if i['partition'] == 'vbmeta')
        row.update(image=inspect_image(top), avb=inspect_avb(top),
                   operation='explicit unsigned top-level AVB policy for unlocked testing')
        report.update(status=result['status'], avb_policy=policy, package_validation=result,
                      avb_preparation_required=False, flash_ready=False,
                      policy_repair_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        report['mods']['unlocked_avb'] = True
        report['validation_attribution']['policy_repair'] = 'Only vbmeta changed after native run; 47 IMG unchanged by size/mtime'
        write_json(report_path, report)
        write_json(Path(str(output) + '.package.json'), {'validation': result, 'attribution': report['validation_attribution']})
        write_json(Path(report['workspace']) / 'build.json', report)
        evidence = {'schema': 1, 'output': str(output), 'status': result['status'], 'policy': policy,
                    'validation': result, 'backup': str(backup), 'avbtool_log': str(log),
                    'original_vbmeta_sha256': hashlib.sha256(original).hexdigest(),
                    'only_top_level_vbmeta_changed': True, 'other_images_unchanged_size_mtime': len(mapping) - 1,
                    'filesystem_repack_rerun': False, 'whole_rom_hashes': False, 'device_commands': False,
                    'device_verified': False, 'scope': 'Explicit unlocked-test policy, not OEM signing or hardware validation'}
        write_json(ROOT / 'evidence/pc-unlocked-avb-output.json', evidence)
        return report
    except Exception:
        top.write_bytes(original)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = prepare(args.output)
    print(result['status'] + ': ' + result['output'])
