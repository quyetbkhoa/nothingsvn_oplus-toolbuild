from pathlib import Path
import struct
import tempfile
import unittest

from colornt.elf import inspect_elf


def fixture(bits=64):
    data = bytearray(512)
    data[:7] = b'\x7fELF' + bytes((2 if bits == 64 else 1, 1, 1))
    struct.pack_into('<H', data, 18, 183 if bits == 64 else 40)
    if bits == 64:
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HH', data, 54, 56, 2)
        struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0x1000, 0, 512, 512, 4096)
        struct.pack_into('<IIQQQQQQ', data, 120, 2, 6, 192, 0x10c0, 0, 80, 80, 8)
        fmt, width = '<qQ', 16
    else:
        struct.pack_into('<I', data, 28, 52)
        struct.pack_into('<HH', data, 42, 32, 2)
        struct.pack_into('<IIIIIIII', data, 52, 1, 0, 0x1000, 0, 512, 512, 5, 4096)
        struct.pack_into('<IIIIIIII', data, 84, 2, 192, 0x10c0, 0, 40, 40, 6, 4)
        fmt, width = '<iI', 8
    strings = b'\0libexample.so\0libfixture.so\0'
    data[320:320 + len(strings)] = strings
    for i, (tag, value) in enumerate([(5, 0x1140), (10, len(strings)), (1, 1), (14, 15), (0, 0)]):
        struct.pack_into(fmt, data, 192 + i * width, tag, value)
    return data


class ELFTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'file'
            p.write_bytes(data)
            return inspect_elf(p)

    def test_32_and_64_dynamic_dependencies(self):
        for bits in (32, 64):
            with self.subTest(bits=bits):
                result = self.inspect(fixture(bits))
                self.assertEqual(result['bits'], bits)
                self.assertEqual(result['needed'], ['libexample.so'])
                self.assertEqual(result['soname'], 'libfixture.so')
                self.assertEqual(result['load_alignments'], [4096])

    def test_non_elf_including_cygwin_link(self):
        self.assertIsNone(self.inspect(b'!<symlink>\xff\xfe'))

    def test_truncated_and_out_of_bounds_rejected(self):
        for data in [b'\x7fELF', fixture()[:100]]:
            with self.assertRaises(ValueError):
                self.inspect(data)

    def test_string_table_outside_load_rejected(self):
        data = fixture()
        struct.pack_into('<Q', data, 200, 0xffff)
        with self.assertRaisesRegex(ValueError, 'string table'):
            self.inspect(data)

    def test_dependency_string_cannot_escape_table(self):
        data = fixture()
        struct.pack_into('<Q', data, 232, 400)
        with self.assertRaisesRegex(ValueError, 'string index'):
            self.inspect(data)
