"""Byte-qualified v1.4 reconstruction for the observed exact OOS input pair.

No ColorOS JAR transplant, private binary execution, downloads or device actions.
This is an exact-version backend; generic v1.4 transformers remain separate work.
"""
from pathlib import Path

from .android_links import entry_digest, entry_path, link_target
from .io import read_json, sha256, write_json
from .tools import ROOT

RECIPE = 'recipes/pjf110-cph2745-oos16.json'
RECIPE_SHA256 = 'f35676b29e676103a10a840213bea8dfb5932edf456d9f31f0f1c065723352ee'


def recipe():
    path = ROOT / RECIPE
    if not path.is_file() or sha256(path) != RECIPE_SHA256:
        raise ValueError('v1.4 exact-version recipe is missing/changed')
    result = read_json(path)
    if result['variant'] != {'install_mods': True, 'install_toolbox': True}:
        raise ValueError('Unsupported v1.4 reconstruction variant')
    return result


def assets():
    config = ROOT / 'config/project.local.json'
    value = read_json(config).get('desktop_v14_assets_root') if config.is_file() else None
    path = Path(value or ROOT / 'assets/local/pjf110-cph2745').resolve(strict=True)
    if not path.is_dir():
        raise ValueError('v1.4 pinned asset store is missing')
    return path


def source_overrides():
    config = ROOT / 'config/project.local.json'
    settings = read_json(config) if config.is_file() else {}
    return {role: {part: Path(cache) / 'fs' for part, cache in partitions.items()}
            for role, partitions in settings.get('desktop_v14_partition_caches', {}).items()}


def check_partition_cache(root, part, summary):
    state = read_json(root.parent / 'extraction.json')
    records = {r['name']: r for r in state['partitions']}
    r = records.get(part, {})
    if (state['status'] != 'complete' or Path(state['source']).resolve() != Path(summary['path']).resolve() or
        r.get('status') != 'extracted' or r.get('image') != summary['images'].get(part, {}).get('header')):
        raise ValueError('v1.4 extra partition cache belongs to different/incomplete input: ' + part)


def check_partition_caches(base, donor):
    summaries = {'base': base, 'donor': donor}
    for role, partitions in source_overrides().items():
        if role not in summaries:
            raise ValueError('Unknown v1.4 partition-cache role')
        for part, root in partitions.items():
            check_partition_cache(root, part, summaries[role])


def inspect(base, donor):
    data = recipe()
    for role, summary in [('base', base), ('donor', donor)]:
        actual = summary.get('identity') or {}
        for key, value in data['input_identity'][role].items():
            if actual.get(key) != value:
                raise ValueError('v1.4 full bundle requires exact ' + role + ' ' + key + '=' + str(value))
    check_partition_caches(base, donor)
    root = assets()
    # Bounded existence/size preflight; stage verifies every imported asset hash.
    for row in data['files']:
        if row['source'] == 'assets':
            p = entry_path(root, row['source_path'])
            if link_target(p) != row.get('link_target') or (link_target(p) is None and (not p.is_file() or p.stat().st_size != row['size'])):
                raise ValueError('v1.4 asset missing/size/link mismatch: ' + row['path'])
    for row in data['images']:
        if row['kind'] == 'asset':
            p = root / row['asset']
            if not p.is_file() or p.stat().st_size != row['reference_size']:
                raise ValueError('v1.4 IMG asset missing/size mismatch: ' + row['partition'])
    return {'backend': 'exact-pair-v14-reference-reconstruction', 'recipe_sha256': RECIPE_SHA256,
            'assets': str(root), 'repacked_partitions': sorted(r['partition'] for r in data['images'] if r['kind'] == 'repack-erofs'),
            'source_file_count': len(data['files']), 'asset_file_count': sum(r['source'] == 'assets' for r in data['files']),
            'partition_caches': {role: {part: str(root.parent) for part, root in partitions.items()}
                                 for role, partitions in source_overrides().items()},
            'module_scope': 'Observed v1.4 OOS payload, metadata, apps/framework/toolbox/settings/themes; exact input versions only',
            'generic_transformers': 'NOT_IMPLEMENTED', 'device_validated': False}


def parts(role):
    return {r['source_path'].split('/')[0] for r in recipe()['files'] if r['source'] == role} - source_overrides().get(role, {}).keys()


def image_entries():
    return {r['partition']: r for r in recipe()['images']}


def stage_bundle(base, donor, destination, progress=None):
    from .reconstruct import stage
    result = stage(Path(base), Path(donor), assets(), ROOT / RECIPE, Path(destination), source_overrides(), progress)
    data = recipe()  # Recheck source contract after staging.
    for row in data['images']:
        if row['kind'] == 'repack-erofs':
            (Path(destination) / 'config' / (row['partition'] + '_fs_options')).write_text(
                '-C ' + str(row['compression_cluster']) + '\n', encoding='utf-8')
    return result


def source_map():
    return {r['path']: {'source_role': r['source'], 'source_path': r['source_path'],
                       'transformation': 'v1.4 pinned mod/hardware payload' if r['source'] == 'assets' else 'v1.4 reference-selected unchanged payload'}
            for r in recipe()['files']}


def module_evidence(stage):
    """Bind essential integration files and embedded payload to this new stage."""
    import hashlib
    import zipfile
    from .android import apk_info
    stage = Path(stage)
    rows = {r['path']: r for r in recipe()['files']}
    required = {
        'manager': 'system/system/priv-app/NTManager/NTManager.apk',
        'nexalloy': 'system/system/priv-app/NexAlloyXES/NexAlloyXES.apk',
        'installerx': 'my_product/non_overlay/priv-app/InstallerX/InstallerX.apk',
        'toolbox': 'system_ext/xbin/xeutoolbox',
        'manager_permissions': 'system/system/etc/permissions/com.hma.ntmanager.xml',
        'nexalloy_permissions': 'system/system/etc/permissions/com.hma.nexalloy.xml',
        'installer_permissions': 'my_product/etc/permissions/privapp-permissions-installerx.xml',
        'framework': 'system/system/framework/framework.jar',
        'services': 'system/system/framework/services.jar',
        'oplus_services': 'system/system/framework/oplus-services.jar',
        'settings': 'system_ext/priv-app/Settings/Settings.apk',
        'launcher': 'system_ext/priv-app/OplusLauncher/OplusLauncher.apk',
        'systemui': 'system_ext/priv-app/SystemUI/SystemUI.apk',
        'selinux_bridge': 'system_ext/etc/selinux/system_ext_sepolicy.cil',
        'init_trigger': 'system/system/etc/init/hw/init.rc',
    }
    files = {}
    for name, rel in required.items():
        p = stage / rel
        actual = entry_digest(p)
        if actual != rows[rel]['sha256']:
            raise ValueError('Essential v1.4 integration file changed/missing: ' + rel)
        files[name] = {'path': rel, 'sha256': actual}
        if p.suffix == '.apk':
            app = apk_info(p)
            files[name]['apk'] = {k: app[k] for k in ('package', 'version_name', 'version_code', 'sdk') if k in app}
    with zipfile.ZipFile(stage / required['framework']) as archive:
        members = ['ntmanager/NTNexAlloy.apk'] + [
            'ntmanager/' + abi + '/' + library
            for abi in ('arm64-v8a', 'armeabi-v7a')
            for library in ('libaliuhook.so', 'libc++_shared.so', 'libdexkit.so', 'liblsplant.so')]
        embedded = {name: {'size': archive.getinfo(name).file_size,
                           'sha256': hashlib.sha256(archive.read(name)).hexdigest()} for name in members}
    return {'status': 'PINNED-INTEGRATION-FILES-PASS', 'files': files, 'embedded_framework_payload': embedded,
            'runtime': 'NOT_TESTED',
            'scope': 'Observed exact-version mod/Toolbox files and dependencies reproduced; functional hooks/APK trust need device tests',
            'generic_transformers': 'NOT_IMPLEMENTED'}


def reference_cache(path, summary, role, *, content=True):
    """A content contract qualifies research caches without modifying them.

    Only recipe-selected files may enter the stage. The controller can preflight
    headers/paths/links/sizes and defer digests to the mandatory full stage barrier,
    which checks each copied file before any IMG is generated. No IMG hashing.
    """
    path = Path(path)
    try:
        data = recipe(); state = read_json(path / 'extraction.json')
        if state['status'] != 'complete' or Path(state['source']).resolve() != Path(summary['path']).resolve():
            return False
        records = {r['name']: r for r in state['partitions']}
        overrides = source_overrides().get(role, {})
        for name, root in overrides.items():
            check_partition_cache(root, name, summary)
        for name in parts(role):
            if records[name]['status'] != 'extracted' or records[name]['image'] != summary['images'][name]['header']:
                return False
        for row in data['files']:
            if row['source'] != role:
                continue
            root = overrides.get(row['source_path'].split('/')[0], path / 'fs')
            p = entry_path(root, row['source_path'])
            target = link_target(p)
            if (target != row.get('link_target') or
                (target is None and (not p.is_file() or p.stat().st_size != row['size'])) or
                (content and entry_digest(p) != row['sha256'])):
                return False
        return True
    except (ValueError, KeyError, OSError):
        return False


def compile_policy(stage, work):
    """Compile staged policy at known target-A max33 in the existing host runtime."""
    import subprocess
    from .io import before_write
    from .tools import _run_native
    stage, work = Path(stage), Path(work)
    settings_path = ROOT / 'config/project.local.json'
    settings = read_json(settings_path) if settings_path.is_file() else {}
    from . import policy_runtime
    explicit = settings.get('desktop_policy_compiler')
    compiler = Path(explicit) if explicit else policy_runtime.compiler()
    expected = 'ae83114646bd22993a2f8856062a7bd168853897af02337bde386fb79d938e5b'
    library_expected = '52f9f0665b6e98f7dcd6e52d87506bffa33a1dcc0797f24db994ca2587ee87f7'
    if explicit and (not compiler.is_file() or sha256(compiler) != expected):
        raise ValueError('Pinned v1.4 policy compiler unavailable/changed; cannot certify this mod build')
    if explicit:
        distro = settings.get('desktop_policy_distro') or 'ColorNT-Policy'
        lib = subprocess.run(['wsl.exe', '-d', distro, '--exec', 'sha256sum', '/usr/lib/libsepol.so.2'],
                             capture_output=True, text=True, timeout=30, check=True)
        if lib.stdout.split()[0] != library_expected:
            raise ValueError('Pinned libsepol identity changed')
        prefix = ['wsl.exe', '-d', distro, '--exec', policy_runtime.linux_path(compiler)]
    else:
        expected = policy_runtime.STATIC_SHA256
        prefix = policy_runtime.command(compiler)
        library_expected = None  # libsepol is statically linked and covered by compiler SHA.
    relative_inputs = ['system/system/etc/selinux/plat_sepolicy.cil',
        'system/system/etc/selinux/mapping/34.0.cil', 'system/system/etc/selinux/mapping/34.0.compat.cil',
        'system_ext/etc/selinux/system_ext_sepolicy.cil', 'system_ext/etc/selinux/mapping/34.0.cil',
        'system_ext/etc/selinux/mapping/34.0.compat.cil', 'product/etc/selinux/product_sepolicy.cil',
        'product/etc/selinux/mapping/34.0.cil', 'vendor/etc/selinux/plat_pub_versioned.cil',
        'vendor/etc/selinux/vendor_sepolicy.cil']
    inputs = [{'path': rel, 'sha256': sha256(stage / rel)} for rel in relative_inputs]
    def linux(path):
        return policy_runtime.linux_path(path)
    output = work / 'v14-host-policy-v33.policy'
    before_write(output, 8 * 1024 * 1024)
    args = [*prefix, '-m', '-M', 'true', '-G', '-c', '33',
            '-o', linux(output), '-f', '/dev/null', *[linux(stage / r) for r in relative_inputs]]
    # Report build-time neverallow diagnostics separately. Android split-policy
    # loading invokes secilc with -N; this does not set SELinux permissive.
    strict_log = work / 'v14-host-policy-neverallow.log'
    strict = {'status': 'PASS', 'log': strict_log.name}
    try:
        with strict_log.open('wb') as log:
            _run_native(args, log)
    except subprocess.CalledProcessError as exc:
        if 'neverallow check failed' not in strict_log.read_text(encoding='utf-8', errors='replace'):
            raise
        strict.update(status='FAIL', exit_code=exc.returncode)
    args.insert(args.index('-c'), '-N')
    with (work / 'v14-host-policy.log').open('wb') as log:
        _run_native(args, log)
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError('v1.4 CIL compiler did not produce a policy')
    result = {'status': 'HOST-COMPILE-PASS', 'compile_rerun': True, 'policy_version': 33,
              'inputs': inputs, 'compiled_policy_sha256': sha256(output), 'compiler_sha256': expected,
              'libsepol_sha256': library_expected,
              'compile_flags': ['-m', '-M', 'true', '-G', '-N', '-c', '33'],
              'neverallow_audit': strict,
              'compile_mode_source': 'https://android.googlesource.com/platform/system/core/+/781aa78ee25fca2bb27a9b724ae111f9581e63f7/init/selinux.cpp',
              'scope': 'Staged OOS v1.4 + A vendor/ODM CIL; host compile only, not injected or device load proof'}
    write_json(work / 'v14-selinux-evidence.json', result)
    return result
