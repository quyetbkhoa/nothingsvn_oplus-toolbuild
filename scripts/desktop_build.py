"""Headless execution of the same saved project used by the GUI."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colornt.desktop_build import build, inspect_request
from colornt.io import read_json, write_json

parser = argparse.ArgumentParser()
parser.add_argument('project', type=Path)
parser.add_argument('--plan', action='store_true')
parser.add_argument('--report', type=Path)
args = parser.parse_args()
request = read_json(args.project)
result = inspect_request(request) if args.plan else build(request, lambda msg: print(msg, flush=True))
if args.report:
    write_json(args.report, result)
print(result['status'])
