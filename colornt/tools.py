"""Pinned local dependencies; no package manager or system configuration changes."""
import os
import base64
from pathlib import Path
import shutil
import subprocess
import contextvars
import time
import tarfile
import urllib.request
import zipfile

from .io import confined, read_json, sha256

ROOT = Path(__file__).resolve().parent.parent
JOB_GUARD = contextvars.ContextVar('colornt_job_guard', default=None)


def executable(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [ROOT / "tools" / "local" / "erofs-tools" / (name + suffix),
                  ROOT / "tools" / "local" / "payload" / (name + suffix)]
    if name == "7z" and os.name == "nt":
        candidates.append(Path(os.getenv("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe")
    candidates.extend((ROOT / "tools" / "local").glob(f"**/{name}{suffix}"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which(name)
    if found:
        return Path(found)
    raise ValueError(f"Missing tool {name}; see tools/README.md")


def native_path(path: Path, tool: Path) -> str:
    absolute = path.resolve()
    if os.name == "nt" and (tool.parent / "cygwin1.dll").exists():
        return f"/cygdrive/{absolute.drive[0].lower()}/{absolute.as_posix()[3:]}"
    return str(absolute)


def run_tool(name: str, args: list, log: Path | None = None) -> None:
    tool = executable(name)
    command = [str(tool), *(native_path(arg, tool) if isinstance(arg, Path) else str(arg) for arg in args)]
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            try:
                _run_native(command, stream)
            except subprocess.CalledProcessError as exc:
                raise ValueError(f'{name} failed ({exc.returncode}); inspect {log}') from exc
            except ValueError as exc:
                if hasattr(exc, 'code'):
                    raise
                raise ValueError(f'{exc}; inspect {log}') from exc
    else:
        _run_native(command)


def _run_native(command, stream=None):
    guard = JOB_GUARD.get()
    if guard is None:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT if stream else None, check=True)
        return
    guard()
    process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT if stream else None)
    try:
        while process.poll() is None:
            guard()
            time.sleep(0.4)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise


def install_tool(tool_id: str) -> dict:
    lock = read_json(ROOT / "tools" / "lock.json")
    tool = lock[tool_id]
    if tool["platform"] not in ("python3", "windows-x86_64" if os.name == "nt" else "linux-x86_64"):
        raise ValueError(f"Lock {tool_id} is for {tool['platform']}")
    archive = ROOT / "tools" / "local" / (tool_id + tool.get("archive_suffix", ".zip"))
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists() or sha256(archive) != tool["sha256"]:
        temp = archive.with_suffix(".partial")
        request = urllib.request.Request(tool["url"], headers={"User-Agent": "ColorNT-Toolbuild/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as src, temp.open("wb") as dst:
                if tool.get('encoding') == 'gitiles-base64':
                    dst.write(base64.b64decode(src.read(2 * 1024 * 1024)))
                else:
                    shutil.copyfileobj(src, dst)
            if sha256(temp) != tool["sha256"]:
                raise ValueError("Downloaded tool checksum mismatch")
            temp.replace(archive)
        finally:
            temp.unlink(missing_ok=True)
    target = ROOT / "tools" / "local" / tool["directory"]
    target.mkdir(parents=True, exist_ok=True)
    if tool.get('archive_suffix') == '.py':
        shutil.copyfile(archive, confined(target, tool['filename']))
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if member.is_dir():
                    continue
                path = confined(target, member.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as src, path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("Tool archive contains links/special files")
                path = confined(target, member.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as src, path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    if os.name != "nt":
        names = {"extract.erofs", "dump.erofs", "mkfs.erofs", "fsck.erofs", "payload-dumper-go"}
        for path in target.rglob("*"):
            if path.is_file() and path.name in names:
                path.chmod(0o755)
    return {"tool": tool_id, "directory": str(target), "sha256": tool["sha256"]}
