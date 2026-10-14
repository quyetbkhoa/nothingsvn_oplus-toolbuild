"""Package the trusted local flasher without executing it or copying input scripts."""
import copy
import json
import os
from pathlib import Path
import uuid

from .io import before_write, copy_file
from .tools import ROOT


BUNDLE = 'ColorNT-Flash'


def bundle_info(report):
    supported = report['base']['identity'].get('product_name') == 'PJF110'
    return {'schema': 1, 'kind': 'local-flash-menu', 'launcher': 'Flash-ROM.bat',
            'driver': BUNDLE + '/flash_from_bootloader.py',
            'report': BUNDLE + '/build-report.json', 'target_supported': supported,
            'scope': 'Observed PJF110 log sequence only; other devices are blocked',
            'requirements': 'Python 3.11+ and local platform-tools; nothing runs during build'}


def _write(path, data):
    """Admit bounded companion bytes; publish the launcher only after its helpers."""
    before_write(path, len(data))
    temp = path.with_name('.colornt-flash-' + uuid.uuid4().hex)
    owned = False
    try:
        with temp.open('xb') as stream:
            owned = True
            stream.write(data)
        os.replace(temp, path)
    finally:
        if owned:
            temp.unlink(missing_ok=True)


def bundle_output(folder, report):
    folder = Path(folder).resolve(strict=True)
    helper = folder / BUNDLE
    launcher = folder / 'Flash-ROM.bat'
    if launcher.exists() or helper.exists():
        raise ValueError('Flash companions already exist; refusing to overwrite them')
    if Path(report['output']).resolve() != folder:
        raise ValueError('Flash companion report belongs to another output')
    driver = ROOT / 'scripts/flash_from_bootloader.py'
    if not driver.is_file():
        raise ValueError('Trusted ColorNT flash driver is missing from this installation')
    snapshot = {key: copy.deepcopy(report[key]) for key in
                ('status', 'package_validation', 'images')}
    snapshot['base'] = {'identity': copy.deepcopy(report['base']['identity'])}
    snapshot['images'] = [{'partition': row['partition'], 'path': row['path'],
                           'image': {key: row['image'][key] for key in ('size', 'sha256')
                                     if key in row['image']}}
                          for row in snapshot['images']]
    snapshot['avb_policy'] = copy.deepcopy(report.get('avb_policy', {}))
    for key in ('device_test', 'device_boot_failed'):
        if key in report:
            snapshot[key] = copy.deepcopy(report[key])
    snapshot['output'] = '.'
    snapshot['portable_bundle'] = {'schema': 1, 'kind': 'colornt-relative-output-report'}
    supported = bundle_info(report)['target_supported']
    text = '''@echo off
setlocal DisableDelayedExpansion
title ColorNT - Flash ROM
'''
    if supported:
        text += '''set "fastboot=C:\\Apps\\platform-tools\\fastboot.exe"
if not exist "%fastboot%" set "fastboot=fastboot.exe"
python "%~dp0ColorNT-Flash\\flash_from_bootloader.py" --menu --rom "%~dp0." --fastboot "%fastboot%" --expected-product pineapple --expected-fastbootd-product ossi
set "flashExit=%errorlevel%"
if not "%flashExit%"=="0" echo FAILED - error %flashExit%. Xem failure summary trong log.
echo.
pause
exit /b %flashExit%
'''
    else:
        text += '''echo Chua co flash profile duoc kiem chung cho thiet bi nay.
echo Folder IMG van duoc giu theo ROM A. Khong tu dung lenh flash cua PJF110 cho may khac.
pause
exit /b 2
'''
    helper.mkdir()
    copy_file(driver, helper / driver.name)
    _write(helper / 'build-report.json',
           (json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    _write(helper / 'README.txt', (
        'ColorNT flash companions. Run Flash-ROM.bat manually; building never flashes.\n'
        'Requires local Python 3.11+ and platform-tools (37.0.0 was tested on this PC).\n'
        'Keep ColorNT-Flash beside Flash-ROM.bat. The package report/AVB gates still apply.\n'
        'Only the observed PJF110 flash sequence is implemented; other targets are blocked.\n'
        'Build/transfer completion does not certify Android boot, hardware or optional mods.\n'
    ).encode('utf-8'))
    _write(launcher, text.replace('\n', '\r\n').encode('ascii'))
    return bundle_info(report)
