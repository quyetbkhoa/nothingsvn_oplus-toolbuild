import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from colornt.avb import inspect_avb


def vbmeta(descriptors=b'', flags=0):
    aux = descriptors + bytes((-len(descriptors)) % 64)
    header = bytearray(256)
    header[:4] = b'AVB0'
    struct.pack_into('>IIQQI', header, 4, 1, 0, 320, len(aux), 1)
    struct.pack_into('>4Q', header, 32, 0, 32, 32, 256)
    struct.pack_into('>QQ', header, 96, 0, len(descriptors))
    struct.pack_into('>I', header, 120, flags)
    return bytes(header) + hashlib.sha256(header + aux).digest() + bytes(288) + aux


class AvbTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.img'
            path.write_bytes(data)
            return inspect_avb(path)

    def test_flag_edit_invalidates_metadata_hash_without_claiming_signature(self):
        original = vbmeta()
        result = self.inspect(original)
        self.assertTrue(result['header_aux_hash_matches'])
        self.assertFalse(result['signature_verified'])
        changed = bytearray(original)
        struct.pack_into('>I', changed, 120, 3)
        result = self.inspect(changed)
        self.assertFalse(result['header_aux_hash_matches'])
        self.assertTrue(result['verification_disabled'])

    def test_chain_partition_and_hash_descriptor(self):
        chain = bytearray(96)
        struct.pack_into('>QQ4I', chain, 0, 4, 80, 2, 4, 0, 0)
        chain[92:] = b'boot'
        hashed = bytearray(144)
        struct.pack_into('>QQQ', hashed, 0, 2, 128, 4096)
        hashed[24:30] = b'sha256'
        struct.pack_into('>4I', hashed, 56, 6, 0, 0, 0)
        hashed[132:138] = b'system'
        result = self.inspect(vbmeta(chain + hashed))
        self.assertEqual([d['partition'] for d in result['descriptors']], ['boot', 'system'])
        self.assertEqual(result['descriptors'][1]['image_size'], 4096)

    def test_footer_bounds_and_embedded_vbmeta(self):
        meta = vbmeta()
        footer = b'AVBf' + struct.pack('>IIQQQ', 1, 0, 4096, 4096, len(meta)) + bytes(28)
        result = self.inspect(bytes(4096) + meta + footer)
        self.assertEqual(result['footer']['original_image_size'], 4096)
        bad = b'AVBf' + struct.pack('>IIQQQ', 1, 0, 4096, 4096, len(meta) + 1) + bytes(28)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.inspect(bytes(4096) + meta + bad)

    def test_rejects_oversized_descriptor_and_header_spans(self):
        with self.assertRaisesRegex(ValueError, 'descriptor payload'):
            self.inspect(vbmeta(struct.pack('>QQ', 2, 1024)))
        bad = bytearray(vbmeta())
        struct.pack_into('>Q', bad, 40, 1000)
        with self.assertRaisesRegex(ValueError, 'hash is outside'):
            self.inspect(bad)
        bad = bytearray(256)
        bad[:4] = b'AVB0'
        struct.pack_into('>Q', bad, 12, 1 << 63)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.inspect(bad)

    def test_sparse_and_unsigned_are_explicitly_unverified(self):
        result = self.inspect(struct.pack('<I', 0xED26FF3A) + bytes(128))
        self.assertEqual(result['status'], 'sparse-not-inspected')
        unsigned = bytearray(256)
        unsigned[:4] = b'AVB0'
        result = self.inspect(unsigned)
        self.assertIsNone(result['header_aux_hash_matches'])
        self.assertFalse(result['signature_verified'])
