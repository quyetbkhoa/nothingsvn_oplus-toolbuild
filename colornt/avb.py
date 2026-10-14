"""Bounded AVB metadata inspection; never authenticate a ROM or change its images.

Layouts follow AOSP libavb's vbmeta/footer/hash/hashtree/chain descriptors.
Only header/auxiliary authentication hashes are checked, not RSA signatures,
partition digests, hashtrees, device trust keys or rollback state.
"""
import hashlib
import struct
from pathlib import Path

MAX_METADATA = 16 * 1024 * 1024


def _span(offset, length, limit, label):
    if offset < 0 or length < 0 or offset + length > limit:
        raise ValueError('AVB ' + label + ' is outside its container')


def _descriptors(data):
    result, position = [], 0
    while position < len(data):
        _span(position, 16, len(data), 'descriptor header')
        tag, length = struct.unpack_from('>QQ', data, position)
        if length % 8:
            raise ValueError('AVB descriptor size is not aligned')
        _span(position + 16, length, len(data), 'descriptor payload')
        blob = data[position:position + 16 + length]
        item = {'tag': tag, 'bytes': len(blob)}
        if tag in (1, 2, 4):
            fixed = {1: 180, 2: 132, 4: 92}[tag]
            _span(0, fixed, len(blob), 'typed descriptor')
            if tag == 4:
                rollback, name_len, key_len, flags = struct.unpack_from('>4I', blob, 16)
                variable = name_len + key_len
                item.update(kind='chain', rollback_location=rollback, flags=flags)
            else:
                fields = 104 if tag == 1 else 56
                name_len, salt_len, digest_len, flags = struct.unpack_from('>4I', blob, fields)
                variable = name_len + salt_len + digest_len
                algorithm_offset = 72 if tag == 1 else 24
                item.update(kind='hashtree' if tag == 1 else 'hash', flags=flags,
                            image_size=struct.unpack_from('>Q', blob, 20 if tag == 1 else 16)[0],
                            hash_algorithm=blob[algorithm_offset:algorithm_offset + 32].split(b'\0')[0].decode('ascii'))
                if tag == 1:
                    item.update(tree_offset=struct.unpack_from('>Q', blob, 28)[0],
                                tree_size=struct.unpack_from('>Q', blob, 36)[0],
                                data_block_size=struct.unpack_from('>I', blob, 44)[0],
                                hash_block_size=struct.unpack_from('>I', blob, 48)[0],
                                fec_offset=struct.unpack_from('>Q', blob, 56)[0],
                                fec_size=struct.unpack_from('>Q', blob, 64)[0])
            _span(fixed, variable, len(blob), 'descriptor variable fields')
            item['partition'] = blob[fixed:fixed + name_len].decode('utf-8')
            if tag == 4:
                item['public_key_sha256'] = hashlib.sha256(blob[fixed + name_len:fixed + variable]).hexdigest()
        result.append(item)
        position += len(blob)
    return result


def inspect_avb(path: Path) -> dict:
    """Read raw AVB metadata with bounds checks; sparse images stay explicitly unknown."""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        first = stream.read(4)
        if first == struct.pack('<I', 0xED26FF3A):
            return {'status': 'sparse-not-inspected', 'signature_verified': False}
        offset, limit, footer = 0, size, None
        if first != b'AVB0':
            if size < 64:
                return {'status': 'no-avb-metadata', 'signature_verified': False}
            stream.seek(size - 64)
            tail = stream.read(64)
            if not tail.startswith(b'AVBf'):
                return {'status': 'no-avb-metadata', 'signature_verified': False}
            major, minor, original, offset, limit = struct.unpack_from('>IIQQQ', tail, 4)
            if major != 1 or original > offset or limit < 256:
                raise ValueError('Invalid AVB footer')
            _span(offset, limit, size - 64, 'footer vbmeta')
            footer = {'major': major, 'minor': minor, 'original_image_size': original,
                      'vbmeta_offset': offset, 'vbmeta_size': limit}
        stream.seek(offset)
        header = stream.read(256)
        if len(header) != 256 or not header.startswith(b'AVB0'):
            raise ValueError('Missing or truncated AVB header')
        auth_size, aux_size = struct.unpack_from('>QQ', header, 12)
        if auth_size % 64 or aux_size % 64:
            raise ValueError('AVB authentication/auxiliary size is not aligned')
        total = 256 + auth_size + aux_size
        _span(0, total, limit, 'vbmeta blocks')
        if total > MAX_METADATA:
            raise ValueError('AVB metadata exceeds inspection bound')
        auth, aux = stream.read(auth_size), stream.read(aux_size)
        if len(auth) != auth_size or len(aux) != aux_size:
            raise ValueError('Truncated AVB blocks')
        hash_offset, hash_size, signature_offset, signature_size = struct.unpack_from('>4Q', header, 32)
        key_offset, key_size, key_meta_offset, key_meta_size, desc_offset, desc_size = struct.unpack_from('>6Q', header, 64)
        for start, length, label in ((hash_offset, hash_size, 'hash'), (signature_offset, signature_size, 'signature')):
            _span(start, length, auth_size, label)
        for start, length, label in ((key_offset, key_size, 'key'), (key_meta_offset, key_meta_size, 'key metadata'), (desc_offset, desc_size, 'descriptors')):
            _span(start, length, aux_size, label)
        algorithm = struct.unpack_from('>I', header, 28)[0]
        flags = struct.unpack_from('>I', header, 120)[0]
        result = {'status': 'metadata-inspected', 'algorithm': algorithm, 'flags': flags,
                  'required_version': list(struct.unpack_from('>II', header, 4)),
                  'authentication_bytes': auth_size, 'auxiliary_bytes': aux_size,
                  'metadata_sha256': hashlib.sha256(header + auth + aux).hexdigest(),
                  'canonical_unsigned_root': algorithm == 0 and auth_size == aux_size == 0 and
                      not any(header[32:112]) and not any(header[176:]) and
                      b'\0' in header[128:176] and struct.unpack_from('>II', header, 4) == (1, 0),
                  'hashtree_disabled': bool(flags & 1), 'verification_disabled': bool(flags & 2),
                  'rollback_index': struct.unpack_from('>Q', header, 112)[0],
                  'rollback_location': struct.unpack_from('>I', header, 124)[0],
                  'public_key_sha256': hashlib.sha256(aux[key_offset:key_offset + key_size]).hexdigest() if key_size else None,
                  'descriptors': _descriptors(aux[desc_offset:desc_offset + desc_size]),
                  'signature_verified': False, 'partition_contents_verified': False}
        if algorithm in range(1, 7):
            digest = hashlib.sha256(header + aux).digest() if algorithm <= 3 else hashlib.sha512(header + aux).digest()
            result['header_aux_hash_matches'] = digest == auth[hash_offset:hash_offset + hash_size]
        else:
            result['header_aux_hash_matches'] = None
        if footer:
            result['footer'] = footer
        return result
