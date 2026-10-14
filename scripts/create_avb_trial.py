"""Create a separate full FFF candidate with an isolated three-vbmeta experiment.

Copies the existing native GUI payload; it does not rebuild or import old JARs.
The ordinary full flasher stays blocked. A separate device-bound BAT tests only
the three metadata images against the already-installed payload, without wipe.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt.desktop_build import image_map
from colornt.io import confined, read_json, write_json, copy_file
from colornt.images import inspect_image
from colornt.package_validation import inspect_for_gate, validate_records
from colornt.flash_bundle import bundle_output
from colornt.tools import ROOT
_spec=importlib.util.spec_from_file_location('avb_trial_contract',ROOT/'scripts/test_avb_triplet.py')
_contract=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_contract)
KIND,BASE,DONOR,SOURCE_SHA=_contract.KIND,_contract.BASE,_contract.DONOR,_contract.SOURCE_SHA


def create(source, base, output, device_hash, project_bytes, progress=print):
    source, base = Path(source).resolve(strict=True), Path(base).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or any(output == p or output.is_relative_to(p) or p.is_relative_to(output) for p in (source, base)):
        raise ValueError('Trial output must be new and outside protected inputs/parent')
    if not output.is_relative_to(ROOT / 'out'):
        raise ValueError('Trial creation writes only a new repo out folder')
    parent = read_json(Path(str(source) + '.build.json'))
    if (parent['base']['identity']['version_name'] != BASE or parent['donor']['identity']['version_name'] != DONOR or
            parent.get('device_test', {}).get('boot') != 'failed' or
            parent['package_validation'].get('filesystem_verified') is not True):
        raise ValueError('Exact known failed native GUI payload with filesystem QA is required')
    import re
    if not re.fullmatch('[0-9a-f]{64}', device_hash):
        raise ValueError('Known-device binding from diagnostics/authorized flash is required')
    sources, amap = image_map(source), image_map(base)
    rows = {r['partition']: r for r in parent['images']}
    if set(rows) != set(sources):
        raise ValueError('Parent inventory differs')
    stats = {}
    for name, path in sources.items():
        if path != confined(source, rows[name]['path']) or path.stat().st_size != rows[name]['image']['size']:
            raise ValueError('Parent image path/size differs: ' + name)
        stats[name] = (path.stat().st_size, path.stat().st_mtime_ns)
    payloads = {}
    for name, sha in SOURCE_SHA.items():
        data = bytearray(amap[name].read_bytes())
        if hashlib.sha256(data).hexdigest() != sha or rows[name]['path'] != 'BOOTLOADER/' + name + '.img':
            raise ValueError('Original A metadata/layout not pinned: ' + name)
        data[120:124] = (3).to_bytes(4, 'big')
        payloads[name] = bytes(data)
    size = sum(s[0] for s in stats.values()) + 1024 * 1024
    if project_bytes + size > 200_000_000_000 or shutil.disk_usage(output.parent).free < size + 1024**3:
        raise ValueError('W project quota/free space cannot admit separate candidate')
    infos = list(source.rglob('firmware_info.json'))
    if len(infos) != 1:
        raise ValueError('Expected exactly one parent FFF metadata file')
    report = copy.deepcopy(parent)
    report.update(output=str(output), status='creating-diagnostic-candidate', parent_output=str(source),
                  parent_device_test=copy.deepcopy(parent['device_test']),
                  device_test={'boot':'not-tested','hardware':'not-tested'}, flash_ready=False)
    output.mkdir()
    report_path = Path(str(output) + '.build.json')
    write_json(report_path, report)
    try:
        for name, path in sorted(sources.items()):
            progress('Copy candidate: ' + name)
            target = confined(output, rows[name]['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in payloads:
                target.write_bytes(payloads[name])
            else:
                copy_file(path, target)
            if stats[name] != (path.stat().st_size, path.stat().st_mtime_ns):
                raise ValueError('Parent changed during copying: ' + name)
        copy_file(infos[0], output / infos[0].relative_to(source))
        for row in report['images']:
            if row['partition'] in payloads:
                p=confined(output,row['path'])
                row.update(operation='separate observed-A-triplet flags3 diagnostic', image=inspect_image(p), avb=inspect_for_gate(p))
        report['avb_policy']={'kind':KIND,'status':'diagnostic-only-not-a-production-policy',
                              'source':'Original A bytes with flags3, exactly the historical r2 metadata',
                              'current_RSA_signature_valid':False,'requires_unlocked':True}
        rebuilt=[r['partition'] for r in parent['images'] if 'EROFS rebuilt' in r['operation']]
        report['package_validation']=validate_records(report['images'],set(sources),True,parent['roundtrip'],rebuilt)
        report['package_validation']['blockers'].append({'code':'diagnostic-avb-policy-not-production-validated','partition':None,'target':None})
        report['status']='diagnostic-candidate-unverified'
        report['trial']={'changed_images':sorted(payloads),'parent_payload_rebuilt':False,
                         'filesystem_QA':'Inherited only for unchanged parent payload; no filesystem changes',
                         'parent_boot_failed':True,'device_trial_executed':False}
        helper = output / 'ColorNT-AVB';helper.mkdir()
        for script in ('test_avb_triplet.py','flash_from_bootloader.py'):
            copy_file(ROOT/'scripts'/script,helper/script)
        trial={'kind':KIND,'base_version':BASE,'donor_version':DONOR,'device_serial_sha256':device_hash,
               'parent_output':str(source),'parent_boot':'failed','candidate_boot':'not-tested',
               'images':{name:{'path':rows[name]['path'],'source_sha256':SOURCE_SHA[name],
                               'variant_sha256':hashlib.sha256(data).hexdigest()} for name,data in payloads.items()}}
        write_json(helper/'trial.json',trial)
        bat='''@echo off
setlocal DisableDelayedExpansion
title ColorNT - AVB diagnostic only
python "%~dp0ColorNT-AVB\\test_avb_triplet.py" --rom "%~dp0." --menu
set "trialExit=%errorlevel%"
if not "%trialExit%"=="0" echo STOPPED. Khong tu retry.
pause
exit /b %trialExit%
'''
        (output/'Test-AVB-Triplet.bat').write_bytes(bat.replace('\n','\r\n').encode('ascii'))
        (output/'AVB-TRIAL-README.txt').write_text(
            'DIAGNOSTIC CANDIDATE, NOT A VERIFIED ROM FIX.\n'
            'Payload copied from the already-flashed GUI ROM; only 3 vbmeta images changed.\n'
            'Run Test-AVB-Triplet.bat: option1 offline plan, option2 writes only vbmeta_a/vbmeta_system_a/vbmeta_vendor_a, '
            'resets the SAME slot A retry state and reboots. No wipe/OS/firmware/B-slot writes.\n'
            'Requires the bound PJF110 phone still containing that GUI payload and local Python/platform-tools.\n'
            'Flags3 invalidate OEM signatures; this is an unlocked-device experiment, not signing.\n'
            'Flash-ROM.bat full flash remains blocked until a supported complete package policy is established.\n',encoding='utf-8')
        write_json(report_path,report)
        bundle_output(output,report)
        return report
    except Exception as exc:
        report.update(status='failed',error=str(exc));write_json(report_path,report)
        raise


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();config=read_json(ROOT/'config/project.local.json')
    binding=read_json(ROOT/'work/avb-trial-device-binding.json')
    names=subprocess.run(['rg','--files','--hidden','--no-ignore',config['project_data_root']],capture_output=True,text=True,check=True).stdout.splitlines()
    used=sum(Path(p).stat().st_size for p in names if Path(p).is_file())
    result=create(args.source,config['base'],args.output,binding['device_serial_sha256'],used)
    print(result['status']+': '+result['output'])
