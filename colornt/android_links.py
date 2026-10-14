"""Android links, including Cygwin's on-disk representation on Windows."""
import hashlib
import os
from pathlib import Path
import shutil
from pathlib import PureWindowsPath

MAGIC = b"!<symlink>\xff\xfe"


def entry_path(root: Path, relative: str) -> Path:
    """Confine parents but allow reading a link inode at the final component."""
    from .io import confined
    name = Path(relative.replace("\\", "/"))
    if not relative or name.is_absolute() or PureWindowsPath(relative).drive or ".." in name.parts or not name.name:
        raise ValueError(f"Invalid relative entry: {relative}")
    parent = root if str(name.parent) == "." else confined(root, name.parent.as_posix())
    return parent / name.name


def link_target(path: Path) -> str | None:
    if path.is_symlink():
        return str(path.readlink())
    if not path.is_file() or path.stat().st_size > 65536:
        return None
    with path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            return None
        data = stream.read()
    return data.decode("utf-16-le").rstrip("\0")


def entry_digest(path: Path) -> str:
    from .io import sha256
    target = link_target(path)
    return hashlib.sha256(target.encode("utf-8")).hexdigest() if target is not None else sha256(path)


def copy_entry(source: Path, destination: Path, mtime_ns: int | None = None) -> None:
    """Never follow an Android link into the host filesystem."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = link_target(source)
    if target is None:
        from .io import copy_file
        copy_file(source, destination)
    elif os.name == "nt":
        import ctypes
        from .io import before_write
        data = MAGIC + (target + "\0").encode("utf-16-le")
        before_write(destination, len(data))
        destination.write_bytes(data)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel.GetFileAttributesW.restype = ctypes.c_uint32
        kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel.SetFileAttributesW.restype = ctypes.c_int
        attributes = kernel.GetFileAttributesW(str(destination))
        if attributes == 0xFFFFFFFF or not kernel.SetFileAttributesW(str(destination), attributes | 4):
            raise OSError(ctypes.get_last_error(), "Cannot set Cygwin symlink SYSTEM attribute")
    else:
        from .io import before_write
        before_write(destination, len(target.encode('utf-8')))
        destination.symlink_to(target)
    if mtime_ns is not None:
        os.utime(destination, ns=(mtime_ns, mtime_ns), follow_symlinks=not destination.is_symlink())
