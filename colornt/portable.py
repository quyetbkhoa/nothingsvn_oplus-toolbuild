"""Portable, byte-qualified mod data; no ROM input or device operations."""
import os
from pathlib import Path
import shutil
import uuid
import zipfile

from .android_links import entry_path
from .io import sha256
from .tools import ROOT


def asset_names(data):
    return ({r['source_path'] for r in data['files'] if r['source'] == 'assets'} |
            {r['asset'] for r in data['metadata']} |
            {r['asset'] for r in data['images'] if r['kind'] == 'asset'})


def export_v14_pack(output):
    from .v14 import assets, recipe, RECIPE, RECIPE_SHA256
    from .reconstruct import verify_assets
    source, data = assets(), recipe()
    output = Path(output).absolute()
    if output.exists() or output.is_relative_to(source):
        raise ValueError('Choose a new pack path outside the mod asset store')
    checked = verify_assets(ROOT / RECIPE, source)
    if checked['status'] != 'pass':
        raise ValueError('Mod asset verification failed: ' + str(checked['issues'][:3]))
    output.parent.mkdir(parents=True, exist_ok=True)
    owned = False
    try:
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            owned = True
            for name in sorted(asset_names(data)):
                # Asset links are transported as Cygwin placeholders; never follow
                # a real symlink into host data while producing a portable pack.
                path = entry_path(source, name)
                if path.is_symlink():
                    from .android_links import MAGIC, link_target
                    archive.writestr(name, MAGIC + (link_target(path) + '\0').encode('utf-16-le'))
                else:
                    archive.write(path, name)
    except BaseException:
        if owned:
            output.unlink(missing_ok=True)
        raise
    return {'pack': str(output), 'size': output.stat().st_size, 'sha256': sha256(output),
            'recipe_sha256': RECIPE_SHA256,
            'scope': 'Pinned mod payload, metadata and splash/my_preload data; no full ROM or A/B input packages'}


def import_v14_pack(pack, progress=lambda msg: None):
    from .v14 import recipe, RECIPE
    from .reconstruct import verify_assets
    from .workspace_prepare import prepare_case_flag
    pack = Path(pack).resolve(strict=True)
    data = recipe()
    target = ROOT / 'assets/local/pjf110-cph2745'
    if target.exists():
        checked = verify_assets(ROOT / RECIPE, target)
        if checked['status'] != 'pass':
            raise ValueError('Existing mod store differs; preserve it and install in a new tool folder')
        return dict(checked, status='already-installed')
    wanted = asset_names(data)
    with zipfile.ZipFile(pack) as archive:
        infos = archive.infolist()
        if len(infos) != len(wanted) or {r.filename for r in infos} != wanted:
            raise ValueError('Mod pack has missing, duplicate, or unexpected files')
        if sum(r.file_size for r in infos) != data['asset_bytes']:
            raise ValueError('Mod pack expanded byte total differs from recipe')
        sizes = {r['source_path']: r['size'] for r in data['files']
                 if r['source'] == 'assets' and not r.get('link_target')}
        sizes.update({r['asset']: r['reference_size'] for r in data['images'] if r['kind'] == 'asset'})
        for info in infos:
            if (info.flag_bits & 1 or info.is_dir() or
                    (info.filename in sizes and info.file_size != sizes[info.filename])):
                raise ValueError('Mod pack entry size/type differs: ' + info.filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < data['asset_bytes'] + 100 * 1024**2:
            raise ValueError('Not enough space for the mod asset store')
        temp = target.parent / ('.import-' + uuid.uuid4().hex)
        created = False
        try:
            temp.mkdir()
            created = True
            prepare_case_flag(temp, {})
            for i, info in enumerate(infos, 1):
                path = entry_path(temp, info.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if i % 500 == 0:
                    progress('Nhập dữ liệu mod: ' + str(i) + '/' + str(len(infos)))
            checked = verify_assets(ROOT / RECIPE, temp)
            if checked['status'] != 'pass':
                raise ValueError('Mod pack content/link/metadata mismatch: ' + str(checked['issues'][:3]))
            temp.rename(target)
            created = False
            return dict(checked, status='installed', assets=str(target))
        finally:
            if created:
                # Only the fresh directory owned by this invocation is removed.
                if temp.resolve().parent != target.parent.resolve() or temp.is_symlink():
                    raise ValueError('Refusing unsafe mod import cleanup')
                shutil.rmtree(temp)
