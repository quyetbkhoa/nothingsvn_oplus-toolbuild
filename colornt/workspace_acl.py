"""Temporary DELETE_CHILD grant on a current-user-owned app directory only."""
from contextlib import contextmanager
import base64
import hashlib
import uuid
from pathlib import Path


@contextmanager
def owner_delete_child(path, diagnostic):
    import ctypes
    from ctypes import wintypes
    from .io import write_json
    from .tools import ROOT
    from .workspace_prepare import environment_info
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.exists() and (component.is_symlink() or getattr(component.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Scoped permission preparation refuses linked paths')
    if path != path.resolve(strict=True):
        raise ValueError('Scoped permission path is not canonical')
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    void = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(void), ctypes.POINTER(void), ctypes.POINTER(void), ctypes.POINTER(void), ctypes.POINTER(void)]
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, void, void, void, void]
    advapi.EqualSid.argtypes = [void, void]
    kernel.LocalFree.argtypes = [void]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, void, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    def security():
        owner, dacl, sd = void(), void(), void()
        error = advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
        if error:
            raise OSError(error, 'Cannot read owned-root security descriptor')
        return owner, dacl, sd

    def acl_bytes(dacl):
        if not dacl:
            raise ValueError('Refusing to modify a null DACL')
        size = int.from_bytes(ctypes.string_at(dacl, 4)[2:4], 'little')
        return ctypes.string_at(dacl, size)

    owner, original_dacl, original_sd = security()
    new_dacl, token = void(), wintypes.HANDLE()
    changed = False
    try:
        size = wintypes.DWORD()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), 'Cannot read current owner token')
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        user_buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user_buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), 'Cannot read token user SID')
        user_sid = void.from_buffer(user_buffer).value
        if not advapi.EqualSid(owner, user_sid):
            raise PermissionError('Workspace directory is not owned by current user')
        before = environment_info(path)
        if not before['access_probes']['0x40000']['opened']:
            raise PermissionError('Current directory owner cannot open WRITE_DAC')
        original = acl_bytes(original_dacl)
        backup = ROOT / 'work/acl-backups' / (uuid.uuid4().hex + '.json')
        write_json(backup, {'path': str(path), 'dacl_base64': base64.b64encode(original).decode(),
                            'scope': 'Original DACL of this current-user-owned directory only'})
        diagnostic['scoped_permission'] = {'owner_matches_current_user': True, 'write_dac_opened': True,
            'grant': 'current user DELETE_CHILD only, no inheritance', 'backup': str(backup),
            'original_dacl_sha256': hashlib.sha256(original).hexdigest()}
        class Trustee(ctypes.Structure):
            _fields_ = [('multiple', void), ('operation', ctypes.c_int), ('form', ctypes.c_int),
                        ('type', ctypes.c_int), ('name', void)]
        class ExplicitAccess(ctypes.Structure):
            _fields_ = [('permissions', wintypes.DWORD), ('mode', ctypes.c_int),
                        ('inheritance', wintypes.DWORD), ('trustee', Trustee)]
        access = ExplicitAccess(0x40, 1, 0, Trustee(None, 0, 0, 1, user_sid))
        advapi.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.POINTER(ExplicitAccess), void, ctypes.POINTER(void)]
        error = advapi.SetEntriesInAclW(1, ctypes.byref(access), original_dacl, ctypes.byref(new_dacl))
        if error:
            raise OSError(error, 'Cannot construct scoped DELETE_CHILD grant')
        error = advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, new_dacl, None)
        if error:
            raise OSError(error, 'Cannot apply scoped DELETE_CHILD grant')
        changed = True
        yield
    finally:
        try:
            if changed:
                error = advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, original_dacl, None)
                if error:
                    raise OSError(error, 'Cannot restore original directory DACL; inspect backup')
                _, restored_dacl, restored_sd = security()
                try:
                    matched = acl_bytes(restored_dacl) == original
                finally:
                    kernel.LocalFree(restored_sd)
                after = environment_info(path)
                diagnostic['scoped_permission'].update(restored_ace_bytes=matched,
                    delete_child_before=before['access_probes']['0x1c0']['opened'],
                    delete_child_after=after['access_probes']['0x1c0']['opened'],
                    note='Windows may normalize auto-inherited SD flag; original DACL ACE bytes are compared')
                if not matched:
                    raise OSError('Original DACL ACE bytes did not restore; inspect backup')
        finally:
            if token:
                kernel.CloseHandle(token)
            if new_dacl:
                kernel.LocalFree(new_dacl)
            kernel.LocalFree(original_sd)
