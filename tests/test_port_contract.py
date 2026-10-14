from pathlib import Path
import tempfile
import unittest
import zipfile

from colornt.port_contract import check_staged_contract
from test_elf import fixture


class PortContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ['init', 'apexd', 'servicemanager', 'vold', 'keystore2', 'surfaceflinger', 'app_process64']:
            self.put('system/system/bin/' + name, fixture())
        self.put('vendor/build.prop', b'ro.vndk.version=34\n')
        self.put('my_manifest/build.prop', b'ro.zygote=zygote64\n')
        self.put('system/system/etc/init/hw/init.zygote64.rc', b'service zygote /system/bin/app_process64\n')
        self.apex = self.root / 'system_ext/apex/com.android.vndk.v34.apex'
        self.apex.parent.mkdir(parents=True)
        with zipfile.ZipFile(self.apex, 'w') as archive:
            for name in ['apex_manifest.pb', 'apex_payload.img', 'apex_pubkey']:
                archive.writestr(name, b'fixture')

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def tearDown(self):
        self.temp.cleanup()

    def test_bounded_evidence_never_means_runtime_verified(self):
        result = check_staged_contract(self.root)
        self.assertFalse(result['device_verified'])
        self.assertIn('full VINTF compatibility', result['not_checked'])

    def test_missing_vendor_apex_is_rejected(self):
        self.apex.unlink()
        with self.assertRaisesRegex(ValueError, 'VNDK APEX'):
            check_staged_contract(self.root)

    def test_32_bit_core_executable_is_rejected(self):
        self.put('system/system/bin/init', fixture(32))
        with self.assertRaisesRegex(ValueError, 'ARM64 boot executable'):
            check_staged_contract(self.root)

    def test_dual_zygote_without_32bit_process_is_rejected(self):
        self.put('my_manifest/build.prop', b'ro.zygote=zygote64_32\n')
        self.put('system/system/etc/init/hw/init.zygote64_32.rc', b'fixture')
        with self.assertRaisesRegex(ValueError, 'app_process32'):
            check_staged_contract(self.root)
