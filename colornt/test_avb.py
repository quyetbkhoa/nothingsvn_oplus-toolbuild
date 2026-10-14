"""Fresh, unsigned AVB metadata for an explicitly selected unlocked lab build.

Uses the existing unlocked-root gate. Never patches source/OEM signatures, clears
failed-boot evidence, or declares locked-device compatibility.
"""
from pathlib import Path
import sys
import os
import tempfile

from .avb import inspect_avb
from .images import inspect_image
from .unlocked_avb import generator_tool, prepare_top_level
from .tools import _run_native
from .io import before_write

INSTALLATION_POLICY = 'unlocked-rebuilt-metadata-v1'


def footer_size(payload_size):
    if payload_size <= 0 or payload_size % 4096:
        raise ValueError('Expected a nonempty, 4KB-aligned EROFS payload')
    # Conservative SHA256 tree (1/128 fanout), 64KB vbmeta and footer allowance.
    return ((payload_size + (payload_size + 99) // 100 + 262144 + 4095) // 4096) * 4096


def native(tool, args, log):
    with Path(log).open('ab') as stream:
        _run_native([sys.executable, str(tool), *map(str, args)], stream)


def add_footer(image, partition, log):
    image = Path(image)
    info = inspect_image(image)
    if info['format'] != 'erofs' or info.get('avb_footer'):
        raise ValueError('Fresh footer requires a newly repacked EROFS image')
    tool, lock = generator_tool()
    size = footer_size(info['size'])
    before_write(image, size - info['size'])
    native(tool, ['add_hashtree_footer', '--image', image, '--partition_name', partition,
                  '--partition_size', size, '--algorithm', 'NONE', '--hash_algorithm', 'sha256',
                  '--salt', '', '--block_size', '4096', '--do_not_generate_fec'], log)
    native(tool, ['verify_image', '--image', image], log)
    avb = inspect_avb(image)
    descriptors = avb.get('descriptors', [])
    if (avb.get('algorithm') != 0 or avb.get('flags') != 0 or len(descriptors) != 1 or
            descriptors[0].get('kind') != 'hashtree' or descriptors[0].get('partition') != partition or
            avb.get('footer', {}).get('original_image_size') != info['size']):
        raise ValueError('Fresh footer metadata differs from requested payload/partition')
    return {'partition': partition, 'payload_size': info['size'], 'partition_size': size,
            'algorithm': 'NONE', 'fec': False, 'avbtool_verify_image': 'PASS-UNSIGNED',
            'generator_sha256': lock['sha256'], 'scope': 'Fresh payload tree/root digest, no OEM authentication'}


def prepare_children(images, log):
    """Regenerate child descriptor sets, then use the existing canonical root."""
    tool, lock = generator_tool()
    result = []
    for child, targets in [('vbmeta_system', ['system', 'system_ext', 'product']),
                           ('vbmeta_vendor', ['vendor'])]:
        old = inspect_avb(images[child])
        if old['status'] != 'metadata-inspected' or old.get('footer'):
            raise ValueError('Child must be raw vbmeta')
        args = ['make_vbmeta_image', '--output', images[child], '--algorithm', 'NONE', '--flags', '0',
                '--padding_size', Path(images[child]).stat().st_size,
                '--rollback_index', old['rollback_index'], '--rollback_index_location', old['rollback_location']]
        for target in targets:
            if not inspect_avb(images[target]).get('footer'):
                raise ValueError('Missing fresh child descriptor source: ' + target)
            args += ['--include_descriptors_from_image', images[target]]
        native(tool, args, log)
        # FFF keeps vbmeta in BOOTLOADER and payloads in SYSTEM. avbtool resolves
        # targets beside the metadata: link only this new output, never inputs.
        directory = Path(tempfile.mkdtemp(prefix='avb-verify-', dir=Path(log).parent))
        for name in [child, *targets]:
            os.link(images[name], directory / (name + '.img'))
        native(tool, ['verify_image', '--image', directory / (child + '.img')], log)
        actual = inspect_avb(images[child])
        if actual['flags'] or actual['algorithm'] or {d.get('partition') for d in actual['descriptors']} != set(targets):
            raise ValueError('Child descriptor set differs')
        result.append({'partition': child, 'targets': targets, 'algorithm': 'NONE',
                       'metadata_sha256': actual['metadata_sha256'], 'avbtool_verify_image': 'PASS-UNSIGNED',
                       'verification_directory': str(directory)})
    root = prepare_top_level(images['vbmeta'], log)
    root['fresh_child_metadata'] = result
    root['installation_policy'] = INSTALLATION_POLICY
    return root
