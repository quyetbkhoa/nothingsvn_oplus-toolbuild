"""Owned-root capability diagnostics and the desktop helper result contract."""
import os
import shutil
import uuid
from pathlib import Path
from .io import read_json, write_json


def canonical_owned_path(path):
    original = Path(path).absolute()
    for component in (original, *original.parents):
        if component.exists() and (component.is_symlink() or getattr(component.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Cannot prepare a root through linked path components')
    return original.resolve()


def validate_owned_root(path, project, protected):
    root = canonical_owned_path(path)
    for p in protected:
        p = Path(p).resolve()
        if root == p or root.is_relative_to(p) or p.is_relative_to(root):
            raise ValueError('Preparation overlaps a protected input')
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'colornt-root.json'
    if marker.is_file():
        owner = read_json(marker)
        if owner.get('owner') != 'ColorNT' or Path(owner.get('project', '')).resolve() != Path(project).resolve():
            raise ValueError('Work root ownership mismatch')
    elif any(root.iterdir()):
        raise ValueError('Refusing to prepare a nonempty unowned work root')
    return root


def prepare_case_flag(path, diagnostic):
    from .workspace_fs import enable_case_sensitive
    try:
        enable_case_sensitive(path, diagnostic)
        return
    except PermissionError as exc:
        if os.name != 'nt' or (getattr(exc, 'winerror', None) or exc.errno) != 5 or diagnostic.get('stage') != 'SetCaseSensitiveInfo':
            raise
    diagnostic['initial_attempt'] = dict(diagnostic)
    from .workspace_acl import owner_delete_child
    try:
        with owner_delete_child(path, diagnostic):
            enable_case_sensitive(path, diagnostic)
    except Exception as exc:
        exc.workspace_diagnostic = diagnostic
        raise


def environment_info(path):
    if os.name != 'nt':
        return {'platform': os.name, 'token': {'elevated': None}, 'filesystem': 'posix'}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    token, elevated, size = wintypes.HANDLE(), wintypes.DWORD(), wintypes.DWORD()
    token_info = {'elevated': None}
    if advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        try:
            if advapi.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size)):
                token_info['elevated'] = bool(elevated.value)
            else:
                token_info['error'] = ctypes.get_last_error()
        finally:
            kernel.CloseHandle(token)
    else:
        token_info['error'] = ctypes.get_last_error()
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    volume, filesystem = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(128)
    if (kernel.GetVolumePathNameW(str(Path(path).resolve()), volume, len(volume)) and
            kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem))):
        fs = filesystem.value
    else:
        fs = {'error': ctypes.get_last_error()}
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    rights = {}
    for access in (0x80, 0x180, 0x182, 0x184, 0x1c0, 0x1c6, 0x40000):
        handle = kernel.CreateFileW(str(Path(path).resolve()), access, 7, None, 3, 0x02000000, None)
        ok = handle not in (None, ctypes.c_void_p(-1).value)
        rights[hex(access)] = {'opened': ok, 'error': 0 if ok else ctypes.get_last_error()}
        if ok:
            kernel.CloseHandle(handle)
    return {'platform': 'nt', 'token': token_info, 'filesystem': fs, 'access_probes': rights}


def probe_workspace(root):
    """Only create/delete a fresh owned probe, checking root and child separately."""
    from .workspace_fs import enable_case_sensitive
    probe = Path(root) / ('.colornt-case-probe-' + uuid.uuid4().hex)
    report = {'root': str(root), 'status': 'failed', 'api': {}, 'case_checks': []}
    created = False
    try:
        probe.mkdir()
        created = True
        prepare_case_flag(probe, report['api'])
        report['api']['stage'] = 'CaseDistinctFiles'
        for directory in (probe, probe / 'child'):
            if directory != probe:
                directory.mkdir()
            for name, content in (('Case', b'upper'), ('case', b'lower')):
                with (directory / name).open('xb') as stream:
                    stream.write(content)
            names = sorted(p.name for p in directory.iterdir() if p.is_file())
            passed = names == ['Case', 'case'] and (directory / 'Case').read_bytes() == b'upper' and (directory / 'case').read_bytes() == b'lower'
            report['case_checks'].append({'directory': 'root' if directory == probe else 'child', 'status': 'pass' if passed else 'fail'})
            if not passed:
                raise ValueError('Case-distinct files did not remain distinct')
        report.update(status='success', stage='complete')
    except Exception as exc:
        report.update(error=str(exc), error_code=getattr(exc, 'winerror', None) or getattr(exc, 'errno', None),
                      stage=report['api'].get('stage', 'create-probe'))
    finally:
        if created:
            shutil.rmtree(probe)
    return report


def prepare_owned_roots(roots, project, protected):
    from .workspace_fs import enable_case_sensitive
    result = {'schema': 2, 'status': 'failed', 'roots': [], 'scope': 'isolated ColorNT work roots only'}
    for path in roots:
        item = {'root': str(path), 'status': 'failed', 'stage': 'ownership'}
        result['roots'].append(item)
        try:
            root = validate_owned_root(path, project, protected)
            marker = root / 'colornt-root.json'
            item['probe'] = probe_workspace(root)
            if item['probe']['status'] != 'success':
                item.update(stage=item['probe']['stage'], error=item['probe']['error'], error_code=item['probe'].get('error_code'))
                continue
            item['api'] = {}
            prepare_case_flag(root, item['api'])
            write_json(marker, {'owner': 'ColorNT', 'project': str(Path(project).resolve())})
            item.update(status='success', stage='complete')
        except Exception as exc:
            item.update(error=str(exc), error_code=getattr(exc, 'winerror', None) or getattr(exc, 'errno', None),
                        stage=item.get('api', {}).get('stage', item['stage']))
    result['status'] = 'success' if result['roots'] and all(r['status'] == 'success' for r in result['roots']) else 'failed'
    return result


def launch_preparation(script, result_path, nonce, cwd):
    """Owner-managed preparation runs without UAC or changing system settings."""
    import subprocess
    import sys
    return subprocess.Popen([sys.executable, str(script), '--result', str(result_path), '--nonce', nonce],
                            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def preparation_exited(handle, close=False):
    return handle.poll() is not None
