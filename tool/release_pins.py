#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import re

def release_tag(version, sdkArtifactRevision=0):
    """Keep upstream source identity separate from an immutable SDK revision."""
    if (not isinstance(version, str) or not re.fullmatch(
            r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.[1-9][0-9]*)?', version)):
        raise ValueError('invalid fixed V8 version')
    if type(sdkArtifactRevision) is not int or sdkArtifactRevision < 0:
        raise ValueError('SDK artifact revision must be a nonnegative integer')
    return 'v8-' + version + (f'-sdk.{sdkArtifactRevision}' if sdkArtifactRevision else '')


def release_metadata(value, version):
    """Validate optional release-only fields; never put them in source pins."""
    revision = value.get('sdkArtifactRevision', 0)
    tag = release_tag(version, revision)
    if value.get('releaseTag', tag) != tag:
        raise ValueError('SDK release tag differs from artifact revision')
    replacements = value.get('replacesTargets', [])
    if replacements != (['macos-arm64', 'macos-x64'] if revision else []):
        raise ValueError('SDK artifact revision must replace exactly both Mac targets')
    if revision or any(key in value for key in ('sdkArtifactRevision', 'releaseTag', 'replacesTargets')):
        return {'sdkArtifactRevision': revision, 'releaseTag': tag, 'replacesTargets': replacements}
    return {}


def make_pins(base, detection):
    if not re.fullmatch(r'[0-9a-f]{40}', detection['revision']) or not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', detection['version']):
        raise ValueError('invalid detected fixed V8 identity')
    tag = release_tag(detection['version'], detection.get('sdkArtifactRevision', 0))
    if detection['tag'] != tag or detection.get('releaseTag', tag) != tag:
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
