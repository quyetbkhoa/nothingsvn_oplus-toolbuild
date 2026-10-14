"""Read-only Oplus family analysis; recognition does not certify a port.

Brand feature tokens follow legacy bin/ddevice/fetchINFO.sh. That legacy
reference is evidence for recognition only, not current private port patches.
"""
from pathlib import Path
import xml.etree.ElementTree as ET

from .android_links import link_target
from .io import confined
from .properties import Properties

MANUFACTURERS = {'oppo': 'Oppo', 'realme': 'Realme', 'oneplus': 'OnePlus'}
BRAND_FEATURES = {
    'oppo': ('Oppo', 'ColorOS'),
    'realme': ('Realme', 'RealmeUI'),
    'oneplus': ('OnePlus', 'ColorOS'),
    'oneplus-exp': ('OnePlus', 'OxygenOS'),
}


class _FeatureTree(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError('Unsupported declarations in Oplus brand XML')


def identify_oplus(fs):
    fs = Path(fs)

    def read(relative, optional=False):
        path = confined(fs, relative)
        if optional and not path.exists():
            return None
        if link_target(path) is not None:
            raise ValueError('Oplus identity must not be read through an Android link: ' + relative)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Oversized Oplus identity document: ' + relative)
        return path.read_bytes()

    manifest = Properties(read('my_manifest/build.prop'))
    system = Properties(read('system/system/build.prop'))
    manufacturer = manifest.one('ro.product.manufacturer')
    family = MANUFACTURERS.get(manufacturer.strip().casefold())
    if family is None:
        raise ValueError('Unrecognized Oplus manufacturer: ' + manufacturer)
    feature_path = 'my_stock/etc/extension/com.oplus.app-features.xml'
    raw = read(feature_path, optional=True)
    os_brand = None
    os_name = None
    if raw is not None:
        # No DTD/entity processing is needed for this small feature document.
        xml = ET.fromstring(raw, parser=ET.XMLParser(target=_FeatureTree()))
        values = {element.get('args') for element in xml.iter('app_feature')
                  if element.get('name') == 'com.oplus.ota.brand'}
        if len(values) != 1:
            raise ValueError('Missing or conflicting Oplus OTA brand feature')
        token = next(iter(values))
        if token not in {'String:' + key for key in BRAND_FEATURES}:
            raise ValueError('Unknown Oplus OTA brand feature: ' + str(token))
        os_brand, os_name = BRAND_FEATURES[token.removeprefix('String:')]
    region_values = manifest.values('ro.oplus.pipeline_key')
    if len(region_values) > 1:
        raise ValueError('Ambiguous Oplus region property')
    return {'manufacturer': manufacturer, 'hardware_brand': family,
            'os_brand': os_brand, 'os_name': os_name,
            'model': manifest.one('ro.product.model'),
            'device': manifest.one('ro.product.device'),
            'android_version': system.one('ro.build.version.release'),
            'region': region_values[0] if region_values else None,
            'brand_evidence': feature_path if raw is not None else 'manufacturer only; OS branding unknown'}


def analyze_oplus_pair(base, donor):
    a, b = identify_oplus(base), identify_oplus(donor)
    blockers = []
    if a['android_version'] != '16' or b['android_version'] != '16':
        blockers.append('Reusable property transformations currently require Android 16 on A and B.')
    if (a['hardware_brand'] != 'OnePlus' or b['hardware_brand'] != 'OnePlus' or
            any(info['os_brand'] not in (None, 'OnePlus') for info in (a, b))):
        blockers.append('Oppo/Realme cross-brand port transformations have not been reconstructed or validated.')
    if b['region'] != 'IN':
        blockers.append('Property transformations for donor regions other than IN are incomplete.')
    return {'schema': 1, 'status': 'rules-required' if blockers else 'property-contract-check-required',
            'direction': 'B OS to A hardware', 'base': a, 'donor': b,
            'oplus_family_recognized': True, 'cross_brand': a['hardware_brand'] != b['hardware_brand'],
            'blockers': blockers, 'device_validated': False, 'flash_ready': False,
            'recognition_evidence': 'legacy b0331c4817c07f431f718bde69c8adf2331e4ef4:bin/ddevice/fetchINFO.sh; not current private port source',
            'limitations': ['Recognition does not validate VINTF, HAL, ABI, kernel, SELinux or AVB.',
                            'A matching property scope still needs all source/property and capacity checks.']}
