"""Structural package gate. AVB metadata inspection is not authentication/signing."""
from pathlib import Path
from .avb import inspect_avb
from .images import inspect_image
from .io import read_json


def inspect_for_gate(path):
    try:
        return inspect_avb(path)
    except (OSError, ValueError) as exc:
        return {'status': 'invalid', 'error': str(exc), 'signature_verified': False}


def validate_records(records, required, metadata_present, roundtrip=(), rebuilt=(), policy=None):
    images = {r['partition']: r for r in records}
    from .unlocked_avb import matches_policy
    unlocked = matches_policy(images, policy)
    issues = []
    inactive = []
    def issue(code, owner=None, target=None, **detail):
        destination = inactive if unlocked and code in {
            'required-footer-missing', 'descriptor-size-mismatch', 'hashtree-or-fec-span-invalid',
            'descriptor-target-sparse-uninspected'} else issues
        destination.append(dict(code=code, partition=owner, target=target, **detail))
    if policy and not unlocked:
        issue('unlocked-policy-does-not-match-metadata')
    missing = sorted(set(required) - images.keys())
    for name in missing:
        issue('missing-required-partition', name)
    if len(images) != len(records):
        issue('duplicate-partition')
    if not metadata_present:
        issue('missing-or-invalid-firmware-metadata')
    fs_pass = {r['partition'] for r in roundtrip if r.get('status') == 'pass' and r.get('partition')}
    fs_verified = bool(rebuilt) and set(rebuilt) <= fs_pass
    for name in sorted(set(rebuilt) - fs_pass):
        issue('filesystem-not-verified', name)
    chained = set()
    chain_edges = {}
    locations = {}
    for name, record in images.items():
        avb = record.get('avb', {})
        status = avb.get('status')
        header = record.get('image', {})
        stored = header.get('expanded_size', header.get('size', 0))
        if header.get('format') == 'invalid':
            issue('invalid-image-header', name, error=header.get('error'))
        if status == 'invalid':
            issue('invalid-avb-metadata', name, error=avb.get('error'))
        if name.startswith('vbmeta') and status != 'metadata-inspected':
            issue('required-vbmeta-not-inspected', name, inspection_status=status)
        if avb.get('header_aux_hash_matches') is False:
            issue('vbmeta-header-aux-hash-mismatch', name)
        if avb.get('flags', 0) & ~3:
            issue('unknown-vbmeta-flags', name, flags=avb['flags'])
        if avb.get('algorithm', 0) not in range(7):
            issue('unsupported-avb-algorithm', name, algorithm=avb['algorithm'])
        footer = avb.get('footer') or header.get('avb_footer')
        if record.get('source_has_avb_footer') and not footer:
            issue('required-footer-missing', name)
        for descriptor in avb.get('descriptors', []):
            kind, target = descriptor.get('kind'), descriptor.get('partition')
            if kind not in ('chain', 'hash', 'hashtree'):
                continue
            if target not in images:
                issue('descriptor-target-missing', name, target)
                continue
            selected = images[target]
            target_avb, target_header = selected.get('avb', {}), selected.get('image', {})
            available = target_header.get('expanded_size', target_header.get('size', 0))
            if kind == 'chain':
                chained.add(target)
                chain_edges.setdefault(name, []).append(target)
                location = descriptor.get('rollback_location', 0)
                if not location or location in locations:
                    issue('invalid-or-duplicate-chain-rollback-location', name, target, location=location)
                locations[location] = target
                if target_avb.get('status') != 'metadata-inspected':
                    issue('chain-vbmeta-not-inspected', name, target)
                elif descriptor.get('public_key_sha256') != target_avb.get('public_key_sha256'):
                    issue('chain-public-key-mismatch', name, target)
                continue
            declared = descriptor['image_size']
            target_footer = target_avb.get('footer') or target_header.get('avb_footer')
            original = target_footer.get('original_image_size') if target_footer else None
            if declared > available or original is not None and declared != original:
                issue('descriptor-size-mismatch', name, target, declared_image_size=declared,
                      available_image_bytes=available, footer_original_image_size=original)
            if target_header.get('format') == 'android-sparse' or target_avb.get('status') == 'sparse-not-inspected':
                issue('descriptor-target-sparse-uninspected', name, target)
            if kind == 'hashtree':
                for prefix in ('tree', 'fec'):
                    start, length = descriptor.get(prefix + '_offset', 0), descriptor.get(prefix + '_size', 0)
                    limit = target_footer.get('vbmeta_offset', available) if target_footer else available
                    if length and (start < declared or start + length > limit):
                        issue('hashtree-or-fec-span-invalid', name, target, span=prefix, offset=start, size=length, limit=limit)
    for target in sorted(chained):
        if images[target].get('avb', {}).get('flags', 0):
            issue('nonzero-chained-vbmeta-flags', target)
    visited = set()
    def visit(name, ancestors):
        if name in ancestors:
            issue('avb-chain-cycle', name)
            return
        if name in visited:
            return
        for target in chain_edges.get(name, []):
            visit(target, ancestors | {name})
        visited.add(name)
    for name in chain_edges:
        visit(name, set())
    structural = list(issues)
    # No signing keys or unlocked-device policy were established by this build.
    # Passing size/key/span checks must never bypass authentication or content checks.
    if not unlocked:
        issue('avb-authentication-and-partition-digests-unverified')
        issue('signing-or-unlocked-test-policy-unresolved')
    assembled = not missing and metadata_present and len(images) == len(records)
    ready = unlocked and assembled and fs_verified and not structural
    status = 'experimental-package-ready' if ready else 'package-blocked'
    states = ['assembled'] if assembled else []
    if assembled and fs_verified:
        states.append('filesystem-verified')
    states.append(status)
    return {'schema': 2, 'status': status, 'states': states, 'assembled': assembled,
            'filesystem_verified': fs_verified, 'structural_checks_passed': not structural,
            'structural_issues': structural, 'blockers': issues,
            'inactive_avb_issues': inactive,
            'experimental_package_ready': ready, 'device_verified': False,
            'signature_verified': False, 'partition_contents_verified': False,
            'policy': dict(policy, status='explicit-unlocked-test-only', active_descriptor_graph='disabled-at-root',
                           flags_automatically_changed=False) if unlocked else {'status': 'unresolved', 'flags_automatically_changed': False,
                       'requirements': ['Valid descriptor sizes/footer/chain metadata',
                                        'Authenticated partition digests/hashtrees and vbmeta chain, or an evidence-backed unlocked-test policy',
                                        'Device trust/rollback/unlocked state and explicit device testing']},
            'runtime_conditions': ['Unlocked bootloader allows verification errors', 'OEM must implement the tested AOSP disabled-verification path',
                                   'Confirmed target identity/slots and user-performed boot/hardware test'] if unlocked else [],
            'scope': 'Bounded structural validation; inactive issues retained under explicit unlocked policy; no RSA/content authentication or device checks'}


def validate_folder(folder, base, roundtrip=(), rebuilt=(), policy=None):
    """Require an explicit A layout baseline; never infer completeness from output."""
    from .desktop_build import image_map, PRIVATE_PARTITIONS
    folder = Path(folder)
    mapping = image_map(folder)
    original = image_map(Path(base))
    records = []
    for name, path in sorted(mapping.items()):
        try:
            image = inspect_image(path)
        except ValueError as exc:
            image = {'format': 'invalid', 'error': str(exc), 'size': path.stat().st_size}
        avb = inspect_for_gate(path)
        records.append({'partition': name, 'image': image, 'avb': avb,
                        'source_has_avb_footer': bool(name in original and inspect_image(original[name]).get('avb_footer'))})
    infos = [p for p in folder.rglob('firmware_info.json') if p.is_file()]
    metadata = False
    if len(infos) == 1:
        try:
            info = read_json(infos[0])
            metadata = isinstance(info, dict) and all(info.get(k) for k in ('product_name', 'version_name'))
        except (ValueError, OSError):
            pass
    return validate_records(records, set(original) - PRIVATE_PARTITIONS, metadata, roundtrip, rebuilt, policy)
