"""Device-bound PJF110 AVB experiment. Default is offline plan; never wipe.

Only the three original-A metadata images with flags3 may be used. This is a
diagnostic of the already-flashed GUI payload, not a general ROM flash policy.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import sys

KIND = 'pjf110-observed-avb-triplet-trial-v1'
SOURCE_SHA = {
    'vbmeta': '5d105b99a2e544e40ac0373636b6ca2cfe87494d6ec885445ffc5263afd73dba',
    'vbmeta_system': 'e29892d27e492187d5b3d4e8ae21e222ec0dc9ac0ab4bbdc0fecc56c294279a2',
    'vbmeta_vendor': 'ce14ca6797265e654efae8da00728fee43c65b0b440c205cb9c373a0ac493f70',
}
BASE = 'PJF110_16.0.5.1001(CN01)'
DONOR = 'CPH2745_16.0.10.601(EX01)'


def driver():
    spec = importlib.util.spec_from_file_location('trial_transport', Path(__file__).with_name('flash_from_bootloader.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_trial(folder):
    folder = Path(folder).resolve(strict=True)
    trial = json.loads((folder / 'ColorNT-AVB/trial.json').read_text(encoding='utf-8-sig'))
    if (trial.get('kind') != KIND or trial.get('base_version') != BASE or trial.get('donor_version') != DONOR or
            not re.fullmatch('[0-9a-f]{64}', trial.get('device_serial_sha256', '')) or
            set(trial.get('images', {})) != set(SOURCE_SHA)):
        raise ValueError('Unrecognized or incomplete device-bound AVB trial')
    images = {}
    for name, digest in SOURCE_SHA.items():
        relative = trial['images'][name]['path']
        if relative != 'BOOTLOADER/' + name + '.img':
            raise ValueError('Unexpected metadata path: ' + name)
        path = folder / relative
        if any(p.is_symlink() for p in (path, path.parent)) or not path.resolve().is_relative_to(folder):
            raise ValueError('Linked/escaping metadata is refused')
        size = 12288 if name == 'vbmeta' else 4096
        if path.stat().st_size != size:
            raise ValueError('Wrong metadata size: ' + name)
        data = bytearray(path.read_bytes())
        if data[:4] != b'AVB0' or struct.unpack_from('>I', data, 120)[0] != 3:
            raise ValueError('Wrong metadata flags/header: ' + name)
        data[120:124] = bytes(4)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Metadata differs from the pinned original A except flags: ' + name)
        images[name] = path
    return folder, images, trial


def execute_trial(fb, images):
    transport = driver()
    transport.check_device(fb, 'pineapple', False)
    # Admit all three writes before the first one; no automatic retry or mode change.
    for name, path in images.items():
        value = fb.getvar('partition-size:' + name + '_a')
        if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]+', value) or int(value, 16) < path.stat().st_size:
            raise RuntimeError('Metadata partition capacity unconfirmed: ' + name)
    for name, path in images.items():
        fb.run(['flash', name + '_a', str(path)])
    fb.run(['set_active', 'a'])  # Same slot only: restore retry budget for this experiment.
    if (fb.getvar('current-slot').lstrip('_') != 'a' or fb.getvar('slot-unbootable:a') != 'no' or
            not re.fullmatch(r'[1-9][0-9]*', fb.getvar('slot-retry-count:a'))):
        raise RuntimeError('Slot A retry state unconfirmed; no reboot')
    fb.run(['reboot'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rom', type=Path, required=True)
    parser.add_argument('--fastboot', default='C:/Apps/platform-tools/fastboot.exe')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--menu', action='store_true')
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--check-device', action='store_true')
    args = parser.parse_args(argv)
    if args.menu:
        print('COLORNT - PJF110 AVB DIAGNOSTIC; GUI PAYLOAD ALREADY FLASHED')
        print('1. Xem ke hoach offline\n2. Thu 3 vbmeta SLOT A + reset retry A + reboot (KHONG WIPE)\n3. Chi doc ket noi\n0. Thoat')
        choice = input('Lua chon [0-3]: ').strip()
        if choice == '0':
            return 0
        if choice not in ('1', '2', '3'):
            raise ValueError('Invalid choice')
        args.execute, args.check_device = choice == '2', choice == '3'
    folder, images, trial = load_trial(args.rom)
    print('Diagnostic hypothesis only; modified flags invalidate OEM signatures; unlocked A only.')
    print('3 metadata writes + set_active(a) + reboot; no OS/firmware/data/FRP/B-slot writes.')
    if not args.execute and not args.check_device:
        for name in images:
            print('flash ' + name + '_a ' + str(images[name]))
        print('set_active a\nreboot')
        return 0
    executable = shutil.which(args.fastboot) if Path(args.fastboot).name == args.fastboot else args.fastboot
    if not executable or not Path(executable).is_file():
        raise ValueError('Local fastboot tool is missing')
    transport = driver()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    with Path(str(folder) + '.avb-trial-' + stamp + '.log').open('x', encoding='utf-8') as log:
        fb = transport.Fastboot(str(Path(executable).resolve()), None, log)
        devices = re.findall(r'(?m)^([^\s]+)\s+fastboot\s*$', fb.run(['devices'], timeout=10))
        if len(devices) != 1 or hashlib.sha256(devices[0].encode()).hexdigest() != trial['device_serial_sha256']:
            raise RuntimeError('Exactly the previously tested bound device is required; stopped')
        fb.serial = devices[0]
        if args.execute:
            execute_trial(fb, images)
            print('AVB trial transferred; Android boot NOT verified. Log: ' + log.name)
        else:
            transport.check_device(fb, 'pineapple', False)
            print('Read-only preflight PASS; no flash/reset/reboot.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('STOPPED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
