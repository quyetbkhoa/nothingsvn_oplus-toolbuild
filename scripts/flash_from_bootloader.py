"""Offline-first reconstruction of the supplied flash log, not a generic flasher.

No fastboot process is started without explicit --execute or --check-device.
Blocked packages cannot flash; read-only diagnostics remain available.
See docs/FLASH-FROM-BOOTLOADER.md.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import os
import tempfile

FLASHER_VERSION = '2026.10.14.1'

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROM = ROOT / 'out/PJF110_CPH2745IN-20261012-120139-971a'
BOOTSTRAP = ('vbmeta', 'vbmeta_system', 'vbmeta_vendor', 'init_boot', 'boot')
BOTH_SLOTS = ('abl', 'aop', 'aop_config', 'bluetooth', 'boot', 'cpucp',
              'cpucp_dtb', 'devcfg', 'dsp', 'dtbo', 'featenabler', 'hyp',
              'imagefv', 'init_boot', 'keymaster', 'oplus_sec', 'oplusstanvbk',
              'qupfw', 'shrm', 'splash', 'tz', 'uefi', 'uefisecapp', 'vbmeta',
              'vbmeta_system', 'vbmeta_vendor', 'vendor_boot', 'xbl',
              'xbl_config', 'xbl_ramdump')
LOGICAL_A = ('my_bigball', 'my_carrier', 'my_company', 'my_engineering',
             'my_heytap', 'my_manifest', 'my_preload', 'my_product', 'my_region',
             'my_stock', 'odm', 'product', 'system', 'system_dlkm', 'system_ext',
             'vendor', 'vendor_dlkm')
LOG_ONLY_IMAGES = {'my_company', 'my_preload'}
UNOBSERVED_IMAGES = {'engineering_cdt', 'recovery'}
ALLOWED_IMAGES = set(BOOTSTRAP) | set(BOTH_SLOTS) | set(LOGICAL_A) | {'modem'} | UNOBSERVED_IMAGES


def discover_images(folder):
    """Directory names do not select partitions; only explicit IMG basenames do."""
    images, ignored = {}, []
    pending = [folder]
    while pending:
        for path in pending.pop().iterdir():
            # Reject junctions/symlinks before descending into a directory.
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked package entry is not accepted: ' + str(path))
            if path.is_dir():
                pending.append(path)
                continue
            if not path.is_file() or path.suffix.lower() != '.img':
                continue
            name = path.stem.lower()
            if name not in ALLOWED_IMAGES:
                ignored.append(path.relative_to(folder).as_posix())
                continue
            if name in images:
                raise ValueError('Duplicate partition IMG: ' + name)
            if path.stat().st_size == 0:
                raise ValueError('Empty IMG: ' + name)
            images[name] = path
    return images, ignored


class Console:
    """Keep raw commands/output in the log; render only bounded terminal text."""
    COLORS = {'green': '\033[32m', 'red': '\033[31m', 'cyan': '\033[36m', 'yellow': '\033[33m'}

    def __init__(self, color=None):
        self.color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ if color is None else color
        if self.color and os.name == 'nt' and color is None:
            try:
                import ctypes
                handle = ctypes.windll.kernel32.GetStdHandle(-11)
                mode = ctypes.c_ulong()
                self.color = bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)) and
                                  ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4))
            except (OSError, AttributeError):
                self.color = False

    def text(self, message, color='cyan'):
        print(self.paint(message, color), flush=True)

    def paint(self, message, color):
        return self.COLORS[color] + message + '\033[0m' if self.color else message

    def status(self, label, okay, detail=''):
        status = 'OKAY' if okay else 'FAILED'
        self.text(f'  {label:<28} ' + self.paint(status, 'green' if okay else 'red') +
                  (': ' + safe_text(detail) if detail else ''), 'cyan')

    def progress(self, done, total):
        percent = done * 100 // total if total else 100
        filled = percent * 24 // 100
        self.text(f'[{"#" * filled}{"-" * (24 - filled)}] {percent:3d}%  ({done}/{total})')


def safe_text(value):
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value)[:500]


class SessionLog:
    """Spool raw output then publish a full log with the failure summary first."""
    def __init__(self, path):
        self.path, self.failures, self.fatal = Path(path), [], None
        self.stream = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
        try:
            self.target = self.path.open('x', encoding='utf-8')
        except BaseException:
            self.stream.close()
            raise

    def write(self, text):
        self.stream.write(text)
        # Keep a durable raw copy even if the PC/process exits before finalization.
        self.target.write(text)

    def flush(self):
        self.stream.flush()
        self.target.flush()

    def close(self):
        try:
            with self.target as target:
                target.seek(0)
                target.truncate()
                target.write('COLORNT FLASH ' + FLASHER_VERSION + ' - FAILURE SUMMARY\n')
                target.write('Unresolved IMG failures: ' + str(len(self.failures)) + '\n')
                for row in self.failures:
                    target.write(row['partition'] + ' | ' + row['image'] + '\nReason: ' + row['reason'] + '\n')
                if self.fatal:
                    target.write('STOPPED: ' + self.fatal + '\n')
                target.write('\nFULL COMMAND / OUTPUT / RETRY LOG\n')
                self.stream.seek(0)
                shutil.copyfileobj(self.stream, target)
        finally:
            self.stream.close()


def choose_failure(partition, reason):
    print('Flash failed: ' + safe_text(partition) + '. [S]top / [R]etry / s[K]ip failed (all remaining errors).')
    while True:
        try:
            choice = input('Lua chon [S/R/K, default S]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 'stop'
        choices = {'': 'stop', 's': 'stop', 'stop': 'stop', '1': 'stop',
                   'r': 'retry', 'retry': 'retry', '2': 'retry',
                   'k': 'skip', 'skip': 'skip', 'skip failed': 'skip', '3': 'skip'}
        if choice in choices:
            return choices[choice]
        print('Hay chon S, R hoac K.')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load_package(folder):
    folder = Path(folder).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('ROM folder is not a directory')
    images, ignored = discover_images(folder)
    required = set(BOOTSTRAP) | set(BOTH_SLOTS) | (set(LOGICAL_A) - LOG_ONLY_IMAGES) | {'modem'}
    if required - images.keys():
        raise ValueError('Missing required IMG: ' + ', '.join(sorted(required - images.keys())))
    infos = list(folder.rglob('firmware_info.json'))
    if len(infos) != 1 or read_json(infos[0]).get('product_name') != 'PJF110':
        raise ValueError('This log-based script is scoped to the PJF110 package, not arbitrary devices')
    sidecar = Path(str(folder) + '.build.json')
    if sidecar.is_file():
        report = read_json(sidecar)
        described_folder = Path(report['output']).resolve()
    else:
        report = read_json(folder / 'ColorNT-Flash/build-report.json')
        if (report.get('portable_bundle') != {'schema': 1, 'kind': 'colornt-relative-output-report'}
                or report.get('output') != '.'):
            raise ValueError('Invalid relative-output flash report')
        described_folder = folder
    if described_folder != folder or report['base']['identity']['product_name'] != 'PJF110':
        raise ValueError('Build report does not describe this PJF110 output')
    recorded = {}
    for row in report['images']:
        name = row['partition'].lower()
        if name not in ALLOWED_IMAGES:
            continue
        if name in recorded:
            raise ValueError('Duplicate partition in build report')
        recorded[name] = row
    if images.keys() != recorded.keys():
        raise ValueError('IMG inventory differs from build report')
    manifest_rows = {}
    if any(folder / recorded[name]['path'] != path for name, path in images.items()):
        manifest_path, build_path = folder / 'BUILD-MANIFEST.json', folder / 'BUILD-REPORT.json'
        if manifest_path.is_file() and build_path.is_file():
            manifest = read_json(manifest_path)
            if hashlib.sha256(build_path.read_bytes()).hexdigest() != manifest.get('build_report_sha256'):
                raise ValueError('Repackaging manifest does not match BUILD-REPORT.json')
            for row in manifest['images']:
                if row['partition'] in manifest_rows:
                    raise ValueError('Duplicate partition in build manifest')
                manifest_rows[row['partition']] = row
        print('Checking SHA256 of images moved to new packaging directories ...', flush=True)
    for name, path in images.items():
        row = recorded[name]
        # Packaging directories may be renamed/rearranged; the partition set and
        # image bytes must still match the trusted report. Check hashes on moves.
        recorded_path = folder / row['path']
        if path.stat().st_size != row['image']['size']:
            raise ValueError('IMG path/size differs from report: ' + name)
        digest = row['image'].get('sha256')
        mrow = manifest_rows.get(name, {})
        if not digest and mrow.get('path') == row['path'] and mrow.get('size') == row['image']['size']:
            digest = mrow.get('sha256')
        if recorded_path != path and not digest:
            raise ValueError('Repackaged IMG requires report SHA256: ' + name)
        if digest and recorded_path != path:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                raise ValueError('IMG SHA256 differs from report: ' + name)
    report = dict(report, ignored_images=ignored)
    return folder, images, report


def make_plan(images, wipe=False, reboot=False):
    steps = []
    for name in BOOTSTRAP:
        target = 'boot_a' if name == 'boot' else name
        steps.append(('bootloader', ['flash', target, str(images[name])]))
    steps.append(('transition', ['reboot', 'fastboot']))
    for name in BOTH_SLOTS:
        for slot in ('a', 'b'):
            steps.append(('fastbootd', ['flash', name + '_' + slot, str(images[name])]))
    for name in LOGICAL_A:
        if name in images:
            steps.append(('fastbootd', ['flash', name + '_a', str(images[name])]))
    steps.append(('transition', ['reboot', 'bootloader']))
    for slot in ('a', 'b'):
        steps.append(('bootloader', ['flash', 'modem_' + slot, str(images['modem'])]))
    if wipe:
        steps.append(('bootloader', ['-w']))
    if reboot:
        steps.append(('bootloader', ['reboot']))
    return steps


def execution_gate(report, expected_product, images=None):
    gate = report.get('package_validation', {})
    if (gate.get('device_boot_failed') is True or report.get('device_boot_failed') is True or
            report.get('device_test', {}).get('boot') == 'failed'):
        raise ValueError('PACKAGE BLOCKED: recorded device boot failure. '
                         'Offline AVB checks cannot clear it. No fastboot command was run.')
    if (report.get('status') != 'experimental-package-ready' or
            gate.get('experimental_package_ready') is not True or
            gate.get('assembled') is not True or gate.get('filesystem_verified') is not True or
            gate.get('structural_checks_passed') is not True or gate.get('structural_issues')):
        raise ValueError('PACKAGE BLOCKED: fix and validate AVB/package policy in core first. '
                         'No fastboot command was run. This script has no gate override.')
    policy = report.get('avb_policy', {})
    if policy.get('kind') != 'unlocked-top-level-disabled-v1' or policy.get('requires_unlocked') is not True:
        raise ValueError('No supported explicit unlocked policy in this package')
    if not images or 'vbmeta' not in images or images['vbmeta'].stat().st_size > 65536:
        raise ValueError('Top-level vbmeta is not bounded/present')
    data = images['vbmeta'].read_bytes()
    h = data[:256]
    if (len(h) != 256 or h[:4] != b'AVB0' or struct.unpack_from('>II', h, 4) != (1, 0) or
            any(h[12:112]) or struct.unpack_from('>I', h, 120)[0] != 3 or any(h[176:]) or
            any(data[256:]) or hashlib.sha256(h).hexdigest() != policy.get('top_metadata_sha256')):
        raise ValueError('Current vbmeta bytes do not match the prepared unlocked policy')
    if not policy.get('requires_allow_verification_error') or policy.get('avbtool_metadata_verification') != 'pass-unsigned-NONE':
        raise ValueError('Unsigned metadata policy has no offline verification evidence')
    if not expected_product or not re.fullmatch(r'[A-Za-z0-9_.-]+', expected_product):
        raise ValueError('--expected-product must be the independently confirmed bootloader product of target A; '
                         'pineapple in the old log is not enough to prove the identity of this target')


class Fastboot:
    def __init__(self, executable, serial, log, timeout=900, console=None):
        self.executable, self.serial, self.log, self.timeout = executable, serial, log, timeout
        self.mutating_attempts = 0
        self.console = console or Console()

    def run(self, args, timeout=None):
        command = [self.executable]
        if self.serial:
            command += ['-s', self.serial]
        command += list(args)
        label = subprocess.list2cmdline(command)
        self.log.write('\n> ' + label + '\n'); self.log.flush()
        flashing = args and args[0] == 'flash'
        if flashing:
            self.console.text('\nFlash ' + args[1] + ' ...')
        elif args and args[0] in ('reboot', '-w'):
            self.console.text('\n' + ('Wipe data ...' if args[0] == '-w' else 'Reboot ' + ' '.join(args[1:]) + ' ...'))
        if args and (args[0] in ('flash', '-w', 'reboot', 'erase', 'boot', 'set_active') or
                     args[0].startswith('format')):
            self.mutating_attempts += 1
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors='replace',
                                    timeout=timeout or self.timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            partial = '\n'.join((v.decode(errors='replace') if isinstance(v, bytes) else v or '')
                                for v in (exc.stdout, exc.stderr))
            self.log.write(partial + '\nTIMEOUT\n'); self.log.flush()
            self.console.status('Flash' if flashing else 'Command', False, 'Timed out')
            raise RuntimeError('Timed out; command completion unknown') from exc
        output = result.stdout + '\n' + result.stderr
        self.log.write(output + '\nExit code: ' + str(result.returncode) + '\n'); self.log.flush()
        failed = bool(result.returncode or re.search(r'\bFAILED\b|fastboot: error:', output))
        error_lines = [line.strip() for line in output.splitlines() if
                       re.search(r'FAILED|error:|failed', line, re.I)]
        detail = '\n'.join(error_lines) or output.strip() or ('exit code ' + str(result.returncode))
        if flashing:
            sending = [line for line in output.splitlines() if re.search(r'\bSending\b', line, re.I)]
            if sending:
                self.console.status('Sending', all('OKAY' in line for line in sending),
                                    detail if any('FAILED' in line for line in sending) else '')
            self.console.status('Flash', not failed, detail if failed else '')
        elif failed:
            self.console.status('Command', False, detail)
        elif args and args[0] in ('reboot', '-w'):
            self.console.status('Command', True)
        if failed:
            reason = 'Command failed: ' + detail
            if re.search(r'AdbWriteEndpointSync failed|AdbReadEndpointSync failed|usb_read failed|usb_write failed|'
                         r'(?:Status read|Write to device|Read from device) failed', output, re.I):
                reason += ('\nUSB TRANSPORT ERROR. Device enumeration is not proof that USB reads/writes work. '
                           'Reconnect directly to another USB port; avoid hubs/adapters. '
                           'No automatic retry of flash/wipe/reboot will occur.')
            raise RuntimeError(reason)
        return output

    def getvar(self, name):
        output = self.run(['getvar', name], timeout=15)
        values = re.findall(r'(?m)^\s*(?:\(bootloader\)\s*)?' + re.escape(name) + r':\s*([^\r\n]+)', output)
        if len(set(v.strip() for v in values)) != 1:
            raise RuntimeError('Missing or ambiguous getvar: ' + name)
        return values[0].strip()


def check_device(fb, product, userspace):
    if fb.getvar('product') != product:
        raise RuntimeError('Device product mismatch; stopped')
    if fb.getvar('unlocked').lower() not in ('yes', 'true'):
        raise RuntimeError('Bootloader is not confirmed unlocked; stopped')
    if fb.getvar('current-slot').lstrip('_') != 'a' or fb.getvar('slot-count') != '2':
        raise RuntimeError('This sequence requires current slot A and exactly two slots; stopped')
    if fb.getvar('is-userspace').lower() != ('yes' if userspace else 'no'):
        raise RuntimeError('Incorrect bootloader/fastbootd mode; stopped')


def image_size(path):
    with Path(path).open('rb') as stream:
        header = stream.read(28)
    if len(header) >= 28 and struct.unpack_from('<I', header)[0] == 0xed26ff3a:
        return struct.unpack_from('<I', header, 12)[0] * struct.unpack_from('<I', header, 16)[0]
    return Path(path).stat().st_size


def check_physical_sizes(fb, images):
    for name in (*BOTH_SLOTS, 'modem'):
        for slot in ('a', 'b'):
            value = fb.getvar('partition-size:' + name + '_' + slot)
            if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]+', value) or int(value, 16) < image_size(images[name]):
                raise RuntimeError('Physical partition capacity unconfirmed/too small: ' + name + '_' + slot)


def check_fastbootd(fb, images):
    variables = fb.run(['getvar', 'all'], timeout=20)
    if re.search(r'(?:partition-size|is-logical):[^:\s]+-cow\s*:', variables):
        raise RuntimeError('COW partitions exist; original log provides no deletion behavior. Stopped.')
    for name in LOGICAL_A:
        if name in images and fb.getvar('is-logical:' + name + '_a').lower() != 'yes':
            raise RuntimeError('Expected existing logical partition is not confirmed: ' + name + '_a')


def execute_plan(fb, images, product, plan, fastbootd_product, on_failure=None,
                 console=None, failures=None, log=None):
    console = console or Console()
    failures = failures if failures is not None else []
    console.text('Checking device identity, mode, slot and partition capacity ...')
    check_device(fb, product, False)
    check_physical_sizes(fb, images)
    skip_errors = False
    console.status('Preflight', True)
    console.progress(0, len(plan))
    for index, (mode, args) in enumerate(plan, 1):
        if failures and args in (['-w'], ['reboot']):
            console.text('Omitted ' + ('wipe data' if args == ['-w'] else 'Android reboot') +
                         ': unresolved IMG failures.', 'yellow')
            if log:
                log.write('OMITTED ' + str(args) + ': unresolved IMG failures\n')
            console.progress(index, len(plan))
            continue
        failed_row = None
        while True:
            try:
                fb.run(args)
                if failed_row is not None:
                    failures.remove(failed_row)
                    if log:
                        log.write('RECOVERED: ' + args[1] + '\n')
                break
            except RuntimeError as exc:
                if args[0] != 'flash':
                    raise
                row = {'partition': args[1], 'image': str(args[2]), 'reason': str(exc)}
                if failed_row is not None:
                    failures.remove(failed_row)
                failures.append(row)
                failed_row = row
                action = 'skip' if skip_errors else (on_failure(args[1], str(exc)) if on_failure else 'stop')
                if log:
                    log.write('FAILURE ' + json.dumps(row) + '\nACTION: ' + action + '\n')
                    log.flush()
                if action == 'retry':
                    console.text('Retry ' + args[1] + ' ...', 'yellow')
                    # A reconnect may change mode/identity: validate again before
                    # retrying this write. Never replay earlier successful steps.
                    check_device(fb, fastbootd_product if mode == 'fastbootd' else product,
                                 mode == 'fastbootd')
                    continue
                if action != 'skip':
                    raise
                skip_errors = True
                console.text('Skipped ' + args[1] + '; continuing remaining IMG.', 'yellow')
                check_device(fb, fastbootd_product if mode == 'fastbootd' else product,
                             mode == 'fastbootd')
                break
        if args == ['reboot', 'fastboot']:
            time.sleep(10)  # Observed settling interval, then require live state rather than assuming it.
            check_device(fb, fastbootd_product, True)
            check_fastbootd(fb, images)
        elif args == ['reboot', 'bootloader']:
            time.sleep(5)
            check_device(fb, product, False)
        console.progress(index, len(plan))
    return failures


def select_menu(boot_product=None, fastbootd_product=None):
    console = Console()
    console.text('\nCOLORNT FLASH ' + FLASHER_VERSION + ' - PJF110 (UNLOCKED, SLOT A)')
    console.text('Bootloader: ' + (boot_product or 'chua cau hinh') + ' | Fastbootd: ' +
          (fastbootd_product or 'chua cau hinh') + ' (configured target identities).')
    console.text('WIPE DATA? Chon 1 = KHONG, 2 = CO (XOA du lieu).', 'yellow')
    print('1. Flash - GIU du lieu (khong format userdata/metadata)')
    print('2. Flash - WIPE du lieu (XOA/FORMAT userdata va metadata)')
    print('3. Chi xem ke hoach (khong ket noi dien thoai)')
    print('4. Kiem tra ket noi USB/getvar (CHI DOC, khong flash/wipe/reboot)')
    print('0. Thoat')
    while True:
        choice = input('Lua chon [0-4]: ').strip()
        if choice in ('0', '1', '2', '3', '4'):
            return choice
        print('Hay chon 0, 1, 2, 3 hoac 4.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rom', type=Path, default=DEFAULT_ROM)
    parser.add_argument('--execute', action='store_true', help='Explicitly run the plan; requires package gate to pass')
    parser.add_argument('--menu', action='store_true', help='BAT menu: keep data, wipe, plan, read-only USB check or exit')
    parser.add_argument('--check-device', action='store_true', help='Read-only USB/getvar diagnostics, no flash/wipe/reboot')
    parser.add_argument('--fastboot', help='Path to official platform-tools fastboot.exe')
    parser.add_argument('--serial', help='Select a single device, never hardcoded from the historical log')
    parser.add_argument('--expected-product', help='Independently confirmed bootloader product of A')
    parser.add_argument('--expected-fastbootd-product', help='Confirmed userspace product of A; may differ from bootloader product')
    parser.add_argument('--wipe', action='store_true', help='Erase/format user data and metadata with -w')
    parser.add_argument('--reboot', action='store_true', help='Reboot to Android only after all selected operations succeed')
    args = parser.parse_args(argv)
    if args.menu:
        if args.execute or args.wipe or args.reboot or args.check_device:
            raise ValueError('Menu chooses execute/wipe/reboot; do not combine with those command-line flags')
        choice = select_menu(args.expected_product, args.expected_fastbootd_product)
        if choice == '0':
            return 0
        args.execute = choice in ('1', '2')
        args.wipe = choice == '2'
        args.reboot = args.execute
        args.check_device = choice == '4'
    if args.check_device and (args.execute or args.wipe or args.reboot):
        raise ValueError('Read-only connection check cannot be combined with execute/wipe/reboot')
    folder, images, report = load_package(args.rom)
    plan = make_plan(images, args.wipe, args.reboot)
    console = Console()
    console.text('COLORNT FLASH ' + FLASHER_VERSION + ' | ROM: ' + str(folder))
    if report.get('ignored_images'):
        console.text('Ignored IMG outside allowlist: ' + ', '.join(report['ignored_images']), 'yellow')
    print('Unobserved IMG left unchanged: ' + ', '.join(sorted(images.keys() & UNOBSERVED_IMAGES)))
    print('Log-only IMG absent from this output: ' + ', '.join(sorted(LOG_ONLY_IMAGES - images.keys())))
    print('No FRP erase, COW deletion, slot change, AVB flag rewriting or partition creation is inferred.')
    print('Package: ' + report.get('status', 'unknown'))
    if not args.execute and not args.check_device:
        for i, (mode, command) in enumerate(plan, 1):
            print(f'[PLAN] {i:02d} [{mode}] ' + ' '.join(command[:2]))
        print('PLAN ONLY. No device/tool commands were run.')
        return 0
    if args.execute:
        print('PRE-FLIGHT: checking device before any flash/wipe/reboot. Planned commands: ' + str(len(plan)))
        execution_gate(report, args.expected_product, images)
        if not args.expected_fastbootd_product or not re.fullmatch(r'[A-Za-z0-9_.-]+', args.expected_fastbootd_product):
            raise ValueError('--expected-fastbootd-product is also required; bootloader and userspace product names may differ')
    else:
        print('READ-ONLY USB CHECK. No flash/wipe/reboot or partition changes.')
    executable = args.fastboot or shutil.which('fastboot.exe')
    if executable and Path(executable).name == executable:
        executable = shutil.which(executable)
    if not executable or not Path(executable).is_file() or Path(executable).suffix.lower() != '.exe':
        raise ValueError('Specify official platform-tools fastboot.exe with --fastboot')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    log_path = Path(str(folder) + '.flash-' + stamp + '.log')
    log = SessionLog(log_path)
    try:
        fb = Fastboot(str(Path(executable).resolve()), args.serial, log)
        try:
            devices = re.findall(r'(?m)^([^\s]+)\s+fastboot\s*$', fb.run(['devices'], timeout=10))
            if args.serial:
                if devices != [args.serial] and args.serial not in devices:
                    raise RuntimeError('Selected device not found')
            elif len(devices) != 1:
                raise RuntimeError('Exactly one device is required, or select --serial')
            fb.serial = args.serial or devices[0]
            if args.check_device:
                observed = {name: fb.getvar(name) for name in ('product', 'unlocked', 'current-slot', 'slot-count', 'is-userspace')}
                print('USB/getvar responses received: ' + json.dumps(observed))
                log.write('\nRead-only connection check completed; no flash/wipe/reboot.\n')
            else:
                execute_plan(fb, images, args.expected_product, plan, args.expected_fastbootd_product,
                             on_failure=choose_failure if args.menu else None,
                             console=console, failures=log.failures, log=log)
                log.write('\nPlan processed; unresolved failures: ' + str(len(log.failures)) +
                          '; boot/hardware not verified.\n')
        except Exception as exc:
            if getattr(fb, 'mutating_attempts', None) == 0:
                print('NO FLASH/WIPE/REBOOT COMMAND WAS STARTED.', flush=True)
                log.write('\nNo flash/wipe/reboot command was started.\n')
            log.write('\nSTOPPED: ' + str(exc) + '\n')
            log.fatal = str(exc)
            raise
    finally:
        log.close()
        console.text('Full log: ' + str(log_path))
    if log.failures:
        console.text('FAILED: ' + str(len(log.failures)) + ' IMG operations unresolved. Wipe/reboot omitted.', 'red')
        return 1
    console.text('Read-only USB check completed.' if args.check_device else
                 'Selected operations completed. Android boot/hardware remain unverified.', 'green')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print('STOPPED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
