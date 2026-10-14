"""Prepare app-local dependencies from pinned upstreams; never run a phone tool."""
from pathlib import Path
import hashlib
import os
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt.tools import ROOT, install_tool
from colornt.io import read_json, sha256


def main():
    lock = read_json(ROOT / 'tools/lock.json')
    for name in (('erofs-windows', 'payload-windows') if os.name == 'nt' else ('erofs-linux', 'payload-linux')):
        print('Prepare ' + name, flush=True)
        install_tool(name)
    install_tool('avbtool-python')
    # A source-only checkout can fetch the static compiler from the exact
    # tool release. Hash is immutable; a moving URL can never replace the pin.
    spec = lock.get('policy-static-portable')
    if spec:
        target = ROOT / 'tools/local/policy-runtime/secilc-static'
        if not target.is_file() or sha256(target) != spec['sha256']:
            if not spec.get('url'):
                raise ValueError('Use the tool release ZIP, which includes the pinned SELinux compiler')
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.download')
            try:
                request = urllib.request.Request(spec['url'], headers={'User-Agent': 'ColorNT-Toolbuild'})
                with urllib.request.urlopen(request, timeout=60) as source, temp.open('xb') as output:
                    import shutil
                    shutil.copyfileobj(source, output)
                if sha256(temp) != spec['sha256']:
                    raise ValueError('Static compiler checksum mismatch')
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        if os.name != 'nt':
            target.chmod(0o755)
    print('App-local dependencies ready; no ROM inputs or device actions.', flush=True)


if __name__ == '__main__':
    main()
