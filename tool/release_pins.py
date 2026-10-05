#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import re

def make_pins(base, detection):
    if not re.fullmatch(r'[0-9a-f]{40}', detection['revision']) or not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', detection['version']):
        raise ValueError('invalid detected fixed V8 identity')
    if detection['tag'] != 'v8-' + detection['version']:
        raise ValueError('detected release tag differs')
    pins = json.loads(json.dumps(base))
    pins['v8'].update(version=detection['version'], revision=detection['revision'])
    return pins

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--base', type=Path, default=Path('tool/v8/pins.json'))
    p.add_argument('--detection', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.write_text(json.dumps(make_pins(json.loads(a.base.read_text()), json.loads(a.detection.read_text())), indent=2) + '\n')
