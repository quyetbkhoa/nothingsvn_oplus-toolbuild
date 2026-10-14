"""Create a fresh portable release from a reviewed source allowlist."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt import __version__
from colornt.io import sha256
from colornt.tools import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--zip', required=True, type=Path)
    args = parser.parse_args()
    target, archive = args.root.resolve(), args.zip.resolve()
    if target.exists() or archive.exists() or target.is_relative_to(ROOT):
        raise ValueError('Release folder/ZIP must be new and outside the working repository')
    source_files = [p for p in (ROOT / 'colornt').glob('*.py')]
    source_files += list((ROOT / 'tests').glob('*.py'))
    for name in ('profiles', 'recipes'):
        source_files += list((ROOT / name).rglob('*.json'))
    source_files += [ROOT / name for name in (
        'toolbuild.py', 'colornt_gui.py', 'pyproject.toml', '.gitignore',
        'install-desktop.ps1', 'prepare-workspace.ps1', 'setup-portable.ps1',
        'Setup.cmd', 'Setup-Policy.cmd', 'ColorNT.cmd',
        'scripts/desktop_build.py', 'scripts/flash_from_bootloader.py',
        'scripts/prepare_windows_workspace.py', 'scripts/bootstrap_portable.py',
        'scripts/package_toolbuild.py', 'scripts/test_avb_triplet.py',
        'scripts/create_avb_trial.py', 'scripts/prepare_unlocked_output.py',
        'reports/PATCH-DETAILS.json', 'tools/lock.json', 'tools/README.md', 'docs/PORTABLE.md')]
    target.mkdir(parents=True)
    for path in source_files:
        dest = target / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    # Portable docs are authoritative for a downloaded tool; local ops/history
    # and machine-specific configuration/caches/ROMs are intentionally excluded.
    shutil.copyfile(ROOT / 'docs/PORTABLE.md', target / 'README.md')
    shutil.copyfile(ROOT / 'docs/PORTABLE.md', target / 'docs/DESKTOP.md')
    for name in ('PORT-BUILD-METHOD.md', 'PORT-TOOL-REQUIREMENTS.md'):
        shutil.copyfile(ROOT / 'docs' / name, target / 'docs' / name)
    for directory in ('erofs-tools', 'payload', 'avb', 'policy-runtime'):
        source = ROOT / 'tools/local' / directory
        if not source.is_dir():
            raise ValueError('Pinned tool directory missing: ' + directory)
        for path in source.iterdir():
            if path.is_file() and (path.suffix.lower() in ('.exe', '.dll', '.py', '.c', '.o', '.md', '.txt') or path.name == 'secilc-static'):
                dest = target / path.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
    records = {p.relative_to(target).as_posix(): {'size': p.stat().st_size, 'sha256': sha256(p)}
               for p in sorted(target.rglob('*')) if p.is_file()}
    manifest = target / 'TOOLBUILD-FILES.json'
    manifest.write_text(json.dumps({'version': __version__, 'files': records,
                                   'device_validated': False}, indent=2) + '\n', encoding='utf-8')
    checker = '''import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
if digest(root/'TOOLBUILD-FILES.json')!=EXPECTED:sys.exit('FAIL: release manifest changed')
m=json.loads((root/'TOOLBUILD-FILES.json').read_text())
for name,row in m['files'].items():
    p=root/name
    if not p.is_file() or p.stat().st_size!=row['size'] or digest(p)!=row['sha256']:sys.exit('FAIL: '+name)
print('PASS tool release identity: '+m['version']+'; ROM/device validation is separate')
'''
    (target / 'Check-Toolbuild.py').write_text("EXPECTED=" + repr(sha256(manifest)) + '\n' + checker, encoding='utf-8')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(target.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(target).as_posix())
    print(json.dumps({'version': __version__, 'folder': str(target), 'zip': str(archive),
                      'size': archive.stat().st_size, 'sha256': sha256(archive),
                      'source_files': len(source_files)}, indent=2))


if __name__ == '__main__':
    main()
