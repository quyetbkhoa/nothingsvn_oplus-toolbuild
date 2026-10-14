"""One-time elevated preparation of app work roots, not Windows system settings."""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt.jobs import local_config, load_choices, BuildJob, volume
from colornt.tools import ROOT
from colornt.io import write_json
from colornt.workspace_prepare import prepare_owned_roots
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result', type=Path, default=ROOT / 'config/workspace-prepared.json')
    parser.add_argument('--nonce', default='manual')
    args = parser.parse_args()
    try:
        config = local_config()
        selected = load_choices()
        job = BuildJob(selected)
        work, project, fallback = job.work_base, job.project, job.fallback
        roots = [work / 'case-jobs']
        if volume(fallback) != 'H:' or Path('H:/').exists():
            roots.append(fallback / hashlib.sha256(str(project).encode()).hexdigest()[:12])
        protected = [Path(config[k]) for k in ('base', 'donor_folder', 'reference_output', 'build_candidate', 'repair_image') if config.get(k)]
        protected += [selected[k] for k in ('base', 'donor') if selected.get(k)]
        result = prepare_owned_roots(roots, project, protected)
    except Exception as exc:
        result = {'schema': 2, 'status': 'failed', 'roots': [], 'stage': 'configuration', 'error': str(exc)}
    result['nonce'] = args.nonce
    write_json(args.result, result)
    print('Workspace preparation: ' + result['status'], flush=True)
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
