"""Pinned static SELinux compiler in an isolated, explicitly prepared WSL root."""
import os
from pathlib import Path
import subprocess
import urllib.request

from .io import read_json, sha256
from .tools import ROOT

STATIC_SHA256 = '86812381be6b6230a7754616c3be3597b178bc4c1a9f38b64f489c3b8814c5bb'
DISTRO = 'ColorNT-Policy-Portable'


def linux_path(path):
    p = Path(path).resolve()
    if os.name != 'nt':
        return str(p)
    if not p.drive or len(p.drive) != 2:
        raise ValueError('Policy work must use a local drive, not a UNC path')
    return '/mnt/' + p.drive[0].lower() + p.as_posix()[2:]


def compiler():
    path = ROOT / 'tools/local/policy-runtime/secilc-static'
    if not path.is_file() or sha256(path) != STATIC_SHA256:
        raise ValueError('Pinned v1.4 policy compiler unavailable/changed; install the portable tool package')
    return path


def setup(progress=lambda msg: None):
    binary = compiler()
    if os.name != 'nt':
        binary.chmod(0o755)
        return {'status': 'ready', 'platform': os.name, 'compiler_sha256': STATIC_SHA256}
    # Explicit setup only. Never enable Windows features, touch another distro,
    # unregister a distro, or replace an installation of unknown ownership.
    result = subprocess.run(['wsl.exe', '--list', '--quiet'], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('WSL2 is required for full v1.4 CIL checks. Install WSL first, then rerun Setup-Policy.cmd')
    names = result.stdout.decode('utf-16-le' if b'\x00' in result.stdout else 'utf-8', errors='replace').split()
    install = ROOT / 'work/policy-runtime'
    marker = install / 'runtime.json'
    if DISTRO in names:
        if not marker.is_file() or read_json(marker).get('install') != str(install.resolve()):
            raise ValueError('Portable policy distro already belongs to another installation; use that tool folder')
    else:
        if install.exists():
            raise ValueError('Incomplete policy workspace exists; preserve it and install in a fresh tool folder')
        lock = read_json(ROOT / 'tools/lock.json')['policy-alpine-rootfs']
        archive = ROOT / 'tools/local/policy-runtime/alpine-rootfs.tar.gz'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.is_file() or sha256(archive) != lock['sha256']:
            temp = archive.with_suffix('.download')
            progress('Tải rootfs SELinux đã ghim checksum…')
            try:
                request = urllib.request.Request(lock['url'], headers={'User-Agent': 'ColorNT-Toolbuild'})
                with urllib.request.urlopen(request, timeout=60) as source, temp.open('xb') as output:
                    import shutil
                    shutil.copyfileobj(source, output)
                if sha256(temp) != lock['sha256']:
                    raise ValueError('Policy rootfs checksum mismatch')
                temp.replace(archive)
            finally:
                temp.unlink(missing_ok=True)
        install.mkdir(parents=True)
        progress('Chuẩn bị WSL riêng cho công cụ SELinux…')
        subprocess.run(['wsl.exe', '--import', DISTRO, str(install / 'distro'), str(archive), '--version', '2'], check=True)
        from .io import write_json
        write_json(marker, {'owner': 'ColorNT', 'install': str(install.resolve()),
                            'rootfs_sha256': lock['sha256'], 'compiler_sha256': STATIC_SHA256})
    subprocess.run(['wsl.exe', '-d', DISTRO, '--exec', '/bin/chmod', '755', linux_path(binary)], check=True)
    return {'status': 'ready', 'platform': os.name, 'distro': DISTRO, 'compiler_sha256': STATIC_SHA256}


def command(binary):
    if os.name == 'nt':
        return ['wsl.exe', '-d', DISTRO, '--exec', linux_path(binary)]
    return [str(binary)]
