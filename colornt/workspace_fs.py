"""Create case-sensitive Android staging trees without changing Windows settings."""
import os
from pathlib import Path


def enable_case_sensitive(path: Path, diagnostic=None) -> None:
    """Require NTFS case sensitivity on an app-owned Android directory."""
    detailed = diagnostic is not None
    diagnostic = diagnostic if detailed else {}
    diagnostic.update(path=str(path), stage='platform')
    if detailed:
        from .workspace_prepare import environment_info
        diagnostic['environment'] = environment_info(path)
    if os.name != 'nt':
        diagnostic.update(stage='complete', status='posix')
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    diagnostic['stage'] = 'OpenReadAttributes'
    handle = kernel.CreateFileW(str(path.resolve()), 0x80, 7, None, 3, 0x02000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), 'Cannot open Android directory attributes', str(path))
    try:
        diagnostic['stage'] = 'QueryCaseSensitiveInfo'
        flags = wintypes.DWORD()
        if not kernel.GetFileInformationByHandleEx(handle, 23, ctypes.byref(flags), 4):
            raise OSError(ctypes.get_last_error(), 'Cannot query NTFS case sensitivity', str(path))
        if flags.value & 1:
            diagnostic.update(stage='complete', status='already-enabled')
            return
        kernel.CloseHandle(handle)
        diagnostic['stage'] = 'OpenWriteAttributes'
        handle = kernel.CreateFileW(str(path.resolve()), 0x180, 7, None, 3, 0x02000000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise PermissionError(ctypes.get_last_error(), 'Windows cannot enable case-sensitive Android workspace; prepare it as Administrator', str(path))
        flags.value = 1
        diagnostic['stage'] = 'SetCaseSensitiveInfo'
        if not kernel.SetFileInformationByHandle(handle, 23, ctypes.byref(flags), 4):
            raise PermissionError(ctypes.get_last_error(),
                                  'Windows cannot enable case-sensitive Android workspace; prepare it as Administrator', str(path))
        diagnostic['stage'] = 'QueryAfterSet'
        if not kernel.GetFileInformationByHandleEx(handle, 23, ctypes.byref(flags), 4) or not flags.value & 1:
            raise OSError(ctypes.get_last_error(), 'Case-sensitive flag did not persist', str(path))
        diagnostic.update(stage='complete', status='enabled')
    finally:
        if handle not in (None, ctypes.c_void_p(-1).value):
            kernel.CloseHandle(handle)


def android_directory(destination: Path) -> None:
    if destination.exists():
        raise ValueError("Android directory destination must be new")
    if os.name != "nt":
        destination.mkdir(parents=True)
        return
    destination.mkdir(parents=True)
    enable_case_sensitive(destination)
