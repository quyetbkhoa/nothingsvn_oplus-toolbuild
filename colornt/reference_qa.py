"""Reuse narrowly scoped offline checks only for the same identity and bytes."""
from pathlib import Path
from .io import read_json, sha256
from .tools import ROOT


def staged_reference_checks(stage, base, donor):
    reference = ROOT / 'profiles/evidence/pjf110-cph2745-booted-20261014.json'
    unchecked = {'status': 'NOT_RUN', 'scope': 'No exact byte-matched prior offline evidence'}
    result = {'host_cil_compile': dict(unchecked), 'apk_cryptographic_signatures': dict(unchecked),
              'full_vintf_kernel_apex_linker_solver': 'NOT_RUN', 'device_boot': 'NOT_TESTED'}
    if not reference.is_file():
        return result
    evidence = read_json(reference)
    if any(any(actual.get(key) != value for key, value in expected.items())
           for actual, expected in [(base, evidence['identities']['base']), (donor, evidence['identities']['donor'])]):
        return result
    stage = Path(stage)
    def matches(rows):
        return all((stage / r['path']).is_file() and sha256(stage / r['path']) == r['sha256'] for r in rows)
    policy = evidence['selinux']
    if matches(policy['inputs']):
        result['host_cil_compile'] = {'status': 'REUSED-PASS-EXACT-INPUTS', 'policy_version': policy['policy_version'],
            'compiled_policy_sha256': policy['compiled_policy_sha256'], 'compiler_sha256': policy['compiler_sha256'],
            'libsepol_sha256': policy['libsepol_sha256'], 'inputs': policy['inputs'], 'compile_rerun': False,
            'scope': 'Exact staged CIL matches prior host compile; no kernel/runtime policy claim'}
    if matches(evidence['apk_checks']):
        result['apk_cryptographic_signatures'] = {'status': 'REUSED-EXACT-FILE-RESULTS', 'checks': evidence['apk_checks'],
            'verifier': evidence['apk_verifier'], 'verification_rerun': False,
            'scope': 'Only these exact staged archives; retain the standard-signing failure of the factory AI plugin. No claim for added APKs/custom loader trust'}
    result['reference'] = {'candidate_id': evidence['candidate_id'], 'manifest_sha256': evidence['manifest_sha256'],
                           'evidence_sha256': sha256(reference), 'scope': 'Offline check reuse only; new output does not inherit boot acceptance'}
    return result
