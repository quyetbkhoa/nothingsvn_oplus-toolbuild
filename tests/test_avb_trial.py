"""Offline tests only: all phone transport is mocked."""
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
def load(name):
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
trial=load('test_avb_triplet');creator=load('create_avb_trial')


class FB:
    def __init__(self, slot='a', capacity='0x10000', fail=None):
        self.slot,self.capacity,self.fail,self.calls=slot,capacity,fail,[]
    def getvar(self,name):
        self.calls.append(['getvar',name])
        if name.startswith('partition-size:'):
            return self.capacity if name=='partition-size:vbmeta_vendor_a' else '0x10000'
        return {'product':'pineapple','unlocked':'yes','current-slot':self.slot,'slot-count':'2',
                'is-userspace':'no','slot-unbootable:a':'no','slot-retry-count:a':'7'}[name]
    def run(self,args,timeout=None):
        self.calls.append(args)
        if args==self.fail:raise RuntimeError('Injected transport failure')
        return 'other-phone\tfastboot\n' if args==['devices'] else 'OKAY\n'


class AvbTrialTests(unittest.TestCase):
    def fixture(self,root):
        folder=root/'ROM';(folder/'BOOTLOADER').mkdir(parents=True);(folder/'ColorNT-AVB').mkdir()
        hashes={};images={}
        for name in trial.SOURCE_SHA:
            data=bytearray(12288 if name=='vbmeta' else 4096);data[:4]=b'AVB0'
            hashes[name]=hashlib.sha256(data).hexdigest();data[123]=3
            p=folder/'BOOTLOADER'/(name+'.img');p.write_bytes(data);images[name]=p
        record={'kind':trial.KIND,'base_version':trial.BASE,'donor_version':trial.DONOR,
                'device_serial_sha256':hashlib.sha256(b'expected-phone').hexdigest(),
                'images':{n:{'path':'BOOTLOADER/'+n+'.img'} for n in hashes}}
        (folder/'ColorNT-AVB/trial.json').write_text(json.dumps(record))
        return folder,hashes,images

    def test_real_bytes_must_match_original_pin_except_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder,hashes,images=self.fixture(Path(tmp))
            with patch.object(trial,'SOURCE_SHA',hashes):
                self.assertEqual(set(trial.load_trial(folder)[1]),set(hashes))
                p=images['vbmeta_vendor'];data=bytearray(p.read_bytes());data[-1]=1;p.write_bytes(data)
                with self.assertRaisesRegex(ValueError,'except flags'):trial.load_trial(folder)
                data[-1]=0;data[123]=0;p.write_bytes(data)
                with self.assertRaisesRegex(ValueError,'flags/header'):trial.load_trial(folder)

    def test_whole_preflight_runs_before_three_writes_and_same_slot_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,images=self.fixture(Path(tmp));fb=FB();trial.execute_trial(fb,images)
            mutations=[a for a in fb.calls if a[0]!='getvar']
            self.assertEqual(mutations,[*(['flash',n+'_a',str(p)] for n,p in images.items()),['set_active','a'],['reboot']])
            first=next(i for i,a in enumerate(fb.calls) if a[0]=='flash')
            self.assertEqual(sum(a[1].startswith('partition-size:') for a in fb.calls[:first] if a[0]=='getvar'),3)

    def test_wrong_slot_or_last_partition_capacity_blocks_all_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,images=self.fixture(Path(tmp))
            for fb in (FB(slot='b'),FB(capacity='0x0')):
                with self.assertRaises(RuntimeError):trial.execute_trial(fb,images)
                self.assertFalse(any(a[0]!='getvar' for a in fb.calls))

    def test_partial_failure_never_continues_reset_or_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,images=self.fixture(Path(tmp));fb=FB(fail=['flash','vbmeta_system_a',str(images['vbmeta_system'])])
            with self.assertRaises(RuntimeError):trial.execute_trial(fb,images)
            self.assertFalse(any(a[0] in ['set_active','reboot'] for a in fb.calls))
            self.assertFalse(any(a[:2]==['flash','vbmeta_vendor_a'] for a in fb.calls))

    def test_default_plan_never_loads_transport_and_other_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder,hashes,_=self.fixture(Path(tmp))
            with patch.object(trial,'SOURCE_SHA',hashes),redirect_stdout(io.StringIO()):
                with patch.object(trial,'driver',side_effect=AssertionError('No transport in plan')):
                    self.assertEqual(trial.main(['--rom',str(folder)]),0)
                fb=FB()
                with patch.object(trial,'driver',return_value=SimpleNamespace(Fastboot=lambda *a:fb)),patch.object(trial.Path,'is_file',return_value=True):
                    with self.assertRaisesRegex(RuntimeError,'bound device'):
                        trial.main(['--rom',str(folder),'--execute','--fastboot','C:/mock/fastboot.exe'])
                self.assertEqual(fb.calls,[['devices']])

    def test_creator_copies_separate_payload_keeps_parent_failed_and_full_flash_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,hashes,images=self.fixture(root)
            base=root/'base';(base/'BOOTLOADER').mkdir(parents=True)
            for n,p in images.items():
                b=bytearray(p.read_bytes());b[123]=0;(base/'BOOTLOADER'/p.name).write_bytes(b)
            (source/'system.img').write_bytes(b'unchanged-native-payload')
            (source/'firmware_info.json').write_text('{"product_name":"PJF110"}')
            rows=[{'partition':n,'path':p.relative_to(source).as_posix(),'image':{'size':p.stat().st_size},
                   'operation':'base unchanged','avb':{'status':'no-avb-metadata'}} for n,p in images.items()]
            rows.append({'partition':'system','path':'system.img','image':{'size':(source/'system.img').stat().st_size},'operation':'donor unchanged','avb':{'status':'no-avb-metadata'}})
            parent={'output':str(source),'status':'package-blocked','images':rows,'roundtrip':[],
                'base':{'identity':{'version_name':trial.BASE,'product_name':'PJF110'}},
                'donor':{'identity':{'version_name':trial.DONOR}},'device_test':{'boot':'failed'},
                'package_validation':{'filesystem_verified':True,'device_boot_failed':True}}
            sidecar=Path(str(source)+'.build.json');sidecar.write_text(json.dumps(parent));before=sidecar.read_bytes()
            install=root/'app';(install/'out').mkdir(parents=True);(install/'scripts').mkdir()
            import shutil
            for name in ('test_avb_triplet.py','flash_from_bootloader.py'):shutil.copyfile(SCRIPTS/name,install/'scripts'/name)
            output=install/'out/trial'
            with patch.object(creator,'ROOT',install),patch.object(creator,'SOURCE_SHA',hashes),redirect_stdout(io.StringIO()):
                result=creator.create(source,base,output,'0'*64,0)
            self.assertEqual(sidecar.read_bytes(),before)
            self.assertEqual(result['parent_device_test']['boot'],'failed')
            self.assertEqual(result['device_test']['boot'],'not-tested')
            self.assertFalse(result['package_validation']['experimental_package_ready'])
            self.assertEqual((source/'system.img').read_bytes(),(output/'system.img').read_bytes())
            (output/'system.img').write_bytes(b'edited-separate-copy')
            self.assertEqual((source/'system.img').read_bytes(),b'unchanged-native-payload')
            with patch.object(trial,'SOURCE_SHA',hashes):trial.load_trial(output)


if __name__=='__main__':unittest.main()
