import io
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from colornt.ota import check_groups, payload_info, _read_manifest


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint((number << 3) | 2) + varint(len(value)) + value


class PayloadTests(unittest.TestCase):
    def test_manifest_sizes_and_groups_from_zip_without_data_reads(self):
        group = field(1, b"group") + field(2, 32768) + field(3, b"system")
        partition = field(1, b"system") + field(7, field(1, 8192))
        manifest = field(3, 4096) + field(13, partition) + field(15, field(1, group))
        data = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 256) + manifest
        # Neither the signature nor payload data is present: manifest-only read.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ota.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("payload.bin", data)
            result = payload_info(path)
        self.assertEqual(result["groups"][0]["maximum_size"], 32768)
        self.assertEqual(result["partitions"], [{"name": "system", "new_size": 8192}])

    def test_bounded_reader_rejects_huge_and_truncated_manifests(self):
        for data in (b"CrAU" + struct.pack(">QQI", 2, 1 << 40, 0),
                     b"CrAU" + struct.pack(">QQI", 2, 10, 0) + b"a"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                _read_manifest(io.BytesIO(data))

    def test_budget_counts_sparse_expanded_sizes_and_extra_partitions(self):
        group = {"name": "qti", "maximum_size": 16384, "partitions": ["system", "vendor"]}
        result = check_groups([group], {"system": 8192, "vendor": 4096, "my_company": 4096}, {"my_company": "qti"})
        self.assertEqual(result[0]["remaining_bytes"], 0)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            check_groups([group], {"system": 8193, "vendor": 4096, "my_company": 4096}, {"my_company": "qti"})
        with self.assertRaisesRegex(ValueError, "Unassigned"):
            check_groups([group], {"system": 8192, "vendor": 4096, "my_company": 4096})
        with self.assertRaisesRegex(ValueError, "Missing"):
            check_groups([group], {"system": 8192})


if __name__ == "__main__":
    unittest.main()
