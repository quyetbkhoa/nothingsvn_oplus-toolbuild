"""Explicit unsigned top-level policy for user testing on unlocked devices only.

It disables verification; it does not recreate OEM signing or fix inactive trees.
Generation/metadata validation uses a checksum-pinned official AOSP avbtool.
"""
import hashlib
from pathlib import Path
import sys
import uuid
from .avb import inspect_avb
from .io import before_write, read_json
from .tools import ROOT, _run_native

POLICY = 'unlocked-top-level-disabled-v1'


def generator_tool():
    lock = read_json(ROOT / 'tools/lock.json')['avbtool-python']
    tool = ROOT / 'tools/local/avb/avbtool.py'
    if not tool.is_file():
        raise ValueError('Missing tool avbtool-python; install the pinned dependency')
    if tool.stat().st_size > 1024 * 1024 or hashlib.sha256(tool.read_bytes()).hexdigest() != lock['sha256']:
        raise ValueError('Pinned avbtool checksum mismatch')
    return tool, lock


def prepare_top_level(path, log=None):
    path = Path(path)
    size = path.stat().st_size
    if not 256 <= size <= 65536:
        raise ValueError('Unexpected top-level vbmeta partition image size')
    source = inspect_avb(path)
    if source['status'] != 'metadata-inspected' or source.get('footer') or source['required_version'] != [1, 0]:
        raise ValueError('Policy requires raw top-level vbmeta compatible with libavb 1.0')
    tool, lock = generator_tool()
    temp = path.with_name('.colornt-vbmeta-' + uuid.uuid4().hex)
    before_write(temp, size)
    try:
        command = [sys.executable, str(tool), 'make_vbmeta_image', '--algorithm', 'NONE', '--flags', '3',
                   '--padding_size', str(size), '--rollback_index', str(source['rollback_index']),
                   '--rollback_index_location', str(source['rollback_location']), '--output', str(temp)]
        with Path(log).open('ab') if log else open_temp_log() as stream:
            _run_native(command, stream)
            _run_native([sys.executable, str(tool), 'verify_image', '--image', str(temp)], stream)
        info = inspect_avb(temp)
        if temp.stat().st_size != size or not info['canonical_unsigned_root'] or info['flags'] != 3 or info['descriptors']:
            raise ValueError('Generated top-level metadata is not the canonical disabled policy')
        policy = {'kind': POLICY, 'explicitly_requested': True,
                  'top_metadata_sha256': info['metadata_sha256'], 'generator_revision': lock['version'],
                  'generator_sha256': lock['sha256'], 'avbtool_metadata_verification': 'pass-unsigned-NONE',
                  'rollback_index': source['rollback_index'], 'rollback_location': source['rollback_location'],
                  'requires_unlocked': True, 'requires_allow_verification_error': True,
                  'oem_bootloader_acceptance': 'requires-user-device-test', 'locked_device_supported': False,
                  'scope': 'Verification/hashtrees disabled globally; no OEM signature or partition digest verification'}
        temp.replace(path)
        return policy
    finally:
        temp.unlink(missing_ok=True)


def open_temp_log():
    import tempfile
    return tempfile.TemporaryFile()


def matches_policy(images, policy):
    if not isinstance(policy, dict) or policy.get('kind') != POLICY or policy.get('explicitly_requested') is not True:
        return False
    root = images.get('vbmeta', {}).get('avb', {})
    return (root.get('canonical_unsigned_root') is True and root.get('flags') == 3 and
            not root.get('descriptors') and root.get('metadata_sha256') == policy.get('top_metadata_sha256') and
            root.get('rollback_index') == policy.get('rollback_index') and
            root.get('rollback_location') == policy.get('rollback_location') and
            policy.get('requires_unlocked') is True and policy.get('requires_allow_verification_error') is True and
            policy.get('avbtool_metadata_verification') == 'pass-unsigned-NONE' and
            all(images.get(p, {}).get('avb', {}).get('flags') == 0 for p in ('vbmeta_system', 'vbmeta_vendor')))
