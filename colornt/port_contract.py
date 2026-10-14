"""Check concrete staged boot/ABI prerequisites, without certifying a boot."""
from pathlib import Path
import zipfile

from .elf import inspect_elf
from .properties import Properties


def check_staged_contract(stage):
    stage = Path(stage)
    checks = []
    for name in ['init', 'apexd', 'servicemanager', 'vold', 'keystore2', 'surfaceflinger', 'app_process64']:
        relative = 'system/system/bin/' + name
        result = inspect_elf(stage / relative)
        if result is None or result['machine'] != 183 or result['bits'] != 64:
            raise ValueError('Staged ARM64 boot executable missing/incompatible: ' + relative)
        checks.append({'path': relative, 'bits': 64, 'machine': 183, 'load_alignments': result['load_alignments']})
    vendor = Properties((stage / 'vendor/build.prop').read_bytes())
    version = vendor.one('ro.vndk.version')
    apex = stage / ('system_ext/apex/com.android.vndk.v' + version + '.apex')
    if not apex.is_file():
        raise ValueError('Staged vendor VNDK APEX is missing: ' + version)
    with zipfile.ZipFile(apex) as archive:
        if not {'apex_manifest.pb', 'apex_payload.img', 'apex_pubkey'} <= set(archive.namelist()):
            raise ValueError('Vendor VNDK APEX lacks payload/manifest/key')
    manifest = Properties((stage / 'my_manifest/build.prop').read_bytes())
    zygote = manifest.one('ro.zygote')
    if zygote not in ('zygote64', 'zygote64_32'):
        raise ValueError('Unimplemented donor zygote contract: ' + zygote)
    init = stage / ('system/system/etc/init/hw/init.' + zygote + '.rc')
    if not init.is_file():
        raise ValueError('Donor zygote import has no staged init script')
    if zygote == 'zygote64_32' and not (stage / 'system/system/bin/app_process32').is_file():
        raise ValueError('Dual zygote declared without app_process32')
    return {'status': 'PASS-BOUNDED', 'boot_executables': checks, 'vendor_vndk_version': version,
            'vndk_apex': apex.relative_to(stage).as_posix(), 'donor_zygote': zygote,
            'device_verified': False, 'scope': 'Staged ELF architecture, zygote file presence and VNDK APEX structure only',
            'not_checked': ['APEX activation/signatures/payload linkage', 'Android property import precedence',
                            'ELF symbol/namespace resolution', 'kernel syscall support', 'full VINTF compatibility', 'runtime boot']}
