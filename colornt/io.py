"""Streaming hashes, atomic reports, and confined workspace paths."""
import hashlib
import json
import os
import zipfile
import contextvars
import uuid
from pathlib import Path

WRITE_GUARD = contextvars.ContextVar('colornt_write_guard', default=None)


def before_write(path, size):
    """Cancel and admit bytes before a product write (including sparse holes)."""
    from .tools import JOB_GUARD
    guard = JOB_GUARD.get()
    if guard:
        guard()
    reserve = WRITE_GUARD.get()
    if reserve:
        reserve(Path(path), size)


def copy_stream(source, destination):
    while data := source.read(1024 * 1024):
        # A quota rescan must see previous admitted bytes, including short buffers.
        destination.flush()
        before_write(destination.name, len(data))
        destination.write(data)


def copy_file(source, destination):
    """Chunked replacement; a failed copy never leaves a reusable partial file."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('Copy source and destination must differ')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name('.colornt-copy-' + uuid.uuid4().hex)
    owned = False
    try:
        with source.open('rb') as src:
            dst = temp.open('xb')
            owned = True
            with dst:
                copy_stream(src, dst)
        os.replace(temp, destination)
    except BaseException:
        if owned:
            temp.unlink(missing_ok=True)
        raise


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def confined(root: Path, relative: str) -> Path:
    """Reject absolute names, traversal and links escaping a chosen root."""
    from pathlib import PureWindowsPath
    name = Path(relative.replace("\\", "/"))
    if not relative or name.is_absolute() or PureWindowsPath(relative).drive or ".." in name.parts:
        raise ValueError(f"Invalid relative path: {relative}")
    candidate = root
    for part in name.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"Symlink in confined path: {relative}")
    result = candidate.resolve()
    if result == root.resolve() or not result.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root: {relative}")
    return result


def is_zip_container(path: Path) -> bool:
    # Images may embed otacerts.zip; zipfile.is_zipfile alone accepts those.
    with path.open("rb") as stream:
        signature = stream.read(4)
    return signature in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08") and zipfile.is_zipfile(path)
