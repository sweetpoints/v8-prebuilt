#!/usr/bin/env python3
"""Resolve official current Stable's V8 branch tip; never choose a maximum tag."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.parse

V8_REPOSITORY = 'https://chromium.googlesource.com/v8/v8.git'
STABLE_URL = 'https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Linux&num=1'
TARGETS = {'android-arm64', 'android-x64', 'ios-arm64', 'ios-simulator-arm64',
           'macos-arm64', 'macos-x64', 'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64'}
SHA = re.compile(r'[0-9a-f]{40}')
JSON_LIMIT = 4 * 1024 * 1024
# Ten full SDK file inventories exceed ordinary GitHub API JSON responses.
MANIFEST_LIMIT = 32 * 1024 * 1024


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https':
            raise ValueError('remote redirect must retain HTTPS')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        origin = urllib.parse.urlsplit(req.full_url)
        if (origin.hostname, origin.port) != (target.hostname, target.port):
            redirected.remove_header('Authorization')
        return redirected


def fetch(url, token=None, accept='application/json', *, max_bytes=JSON_LIMIT):
    headers = {'Accept': accept, 'User-Agent': 'legado-v8-stable-detector'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read(max_bytes + 1)


def read_json(url, token=None):
    data = fetch(url, token)
    if len(data) > JSON_LIMIT:
        raise ValueError('remote response exceeds limit')
    return json.loads(data)


def stable_release(rows):
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('expected one official Stable release')
    row = rows[0]
    milestone = row.get('milestone')
    version = row.get('version')
    if (row.get('channel') != 'Stable' or row.get('platform') != 'Linux'
            or type(milestone) is not int or milestone <= 0
            or not isinstance(version, str)
            or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version)
            or int(version.split('.')[0]) != milestone):
        raise ValueError('invalid official Stable release identity')
    return row


def milestone_branch(rows, milestone):
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('ambiguous official milestone')
    row = rows[0]
    expected = f'{milestone // 10}.{milestone % 10}'
    if row.get('milestone') != milestone or row.get('v8_branch') != expected:
        raise ValueError('official milestone and V8 branch disagree')
    return expected


def branch_revision(repository, branch):
    ref = f'refs/branch-heads/{branch}'
    result = subprocess.run(['git', 'ls-remote', '--refs', repository, ref],
                            check=True, capture_output=True, text=True, timeout=90)
    lines = result.stdout.strip().splitlines()
    if len(lines) != 1:
        raise ValueError('missing or ambiguous official V8 branch ref')
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != ref or not SHA.fullmatch(fields[0]):
        raise ValueError('invalid official V8 revision')
    return fields[0]


def header_version(encoded, branch):
    source = base64.b64decode(encoded, validate=True).decode('utf-8')
    values = []
    for name in ('MAJOR_VERSION', 'MINOR_VERSION', 'BUILD_NUMBER', 'PATCH_LEVEL', 'IS_CANDIDATE_VERSION'):
        matches = re.findall(r'^\s*#define\s+V8_' + name + r'\s+(\d+)\s*$', source, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError('missing or duplicate pinned V8 version definition')
        values.append(int(matches[0]))
    if values[4] != 0 or f'{values[0]}.{values[1]}' != branch:
        raise ValueError('pinned V8 header is not the selected stable branch')
    return '.'.join(str(value) for value in values[:4 if values[3] else 3])


def release_decision(release, manifest, version, revision):
    if release is None:
        return True, 'new'
    if release.get('prerelease'):
        raise ValueError('stable tag already belongs to a prerelease')
    if release.get('tag_name') != f'v8-{version}':
        raise ValueError('GitHub release tag differs')
    if manifest is not None:
        v8 = manifest.get('v8', {})
        if (manifest.get('schemaVersion') != 1 or v8.get('version') != version
                or v8.get('revision') != revision or v8.get('repository') != V8_REPOSITORY):
            raise ValueError('existing tag provenance conflicts with current stable revision')
        targets = manifest.get('targets')
        if ('bridge' in manifest or not isinstance(targets, dict)
                or any(not isinstance(entry, dict) or entry.get('artifactKind') != 'v8-static-sdk'
                       for entry in targets.values())):
            raise ValueError('existing tag is not a pure V8 SDK release')
    if release.get('draft') is True:
        return True, 'draft'
    if manifest is None or set(manifest.get('targets', {})) != TARGETS:
        raise ValueError('published release lacks complete matching manifest')
    assets = release.get('assets', [])
    names = [item.get('name') for item in assets]
    expected = {f'v8-{version}-{target}.tar.gz' for target in TARGETS}
    expected.update({'release-manifest.json', 'pins.json', 'SHA256SUMS'})
    if not expected.issubset(names) or len(names) != len(set(names)):
        raise ValueError('published release lacks complete distinct assets')
    return False, 'published'


def published_release(repository, tag, token):
    url = f'https://api.github.com/repos/{repository}/releases/tags/{tag}'
    try:
        release = read_json(url, token)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, None
        raise
    assets = [a for a in release.get('assets', []) if a.get('name') == 'release-manifest.json']
    if len(assets) > 1:
        raise ValueError('duplicate release manifest asset')
    manifest = None
    if assets:
        asset_id = assets[0].get('id')
        if type(asset_id) is not int or asset_id <= 0:
            raise ValueError('invalid release manifest asset ID')
        size = assets[0].get('size')
        if type(size) is not int or not 0 < size <= MANIFEST_LIMIT:
            raise ValueError('invalid or oversized release manifest asset size')
        data = fetch(f'https://api.github.com/repos/{repository}/releases/assets/{asset_id}',
                     token, 'application/octet-stream', max_bytes=MANIFEST_LIMIT)
        if len(data) > MANIFEST_LIMIT:
            raise ValueError('release manifest exceeds limit')
        if len(data) != size:
            raise ValueError('release manifest download size differs from asset metadata')
        manifest = json.loads(data)
    return release, manifest


def detect(repository, token=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository must be owner/repo')
    stable = stable_release(read_json(STABLE_URL))
    branch = milestone_branch(read_json('https://chromiumdash.appspot.com/fetch_milestones?mstone='
                                        + str(stable['milestone'])), stable['milestone'])
    revision = branch_revision(V8_REPOSITORY, branch)
    header = fetch(f'{V8_REPOSITORY}/+/{revision}/include/v8-version.h?format=TEXT')
    version = header_version(header.strip(), branch)
    # Refuse to emit a build when Stable switched branches during the lookup.
    current = stable_release(read_json(STABLE_URL))
    if current['milestone'] != stable['milestone']:
        raise ValueError('Stable milestone changed during detection; retry')
    tag = f'v8-{version}'
    release, manifest = published_release(repository, tag, token)
    should_build, status = release_decision(release, manifest, version, revision)
    return {'schemaVersion': 1, 'version': version, 'revision': revision,
            'repository': V8_REPOSITORY, 'branch': f'refs/branch-heads/{branch}',
            'chrome_version': current['version'], 'milestone': stable['milestone'],
            'tag': tag, 'should_build': should_build, 'status': status}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='GitHub release repository owner/repo')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = detect(args.repository, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))
        payload = json.dumps(result, indent=2, sort_keys=True) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_name(args.output.name + '.tmp')
            temporary.write_text(payload, encoding='utf-8')
            temporary.replace(args.output)
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8') as out:
                for key in ('version', 'revision', 'tag', 'should_build'):
                    value = result[key]
                    out.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
        sys.stdout.write(payload)
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Do not expose request headers or token-bearing HTTP diagnostics.
        print(f'stable detection failed: {type(error).__name__}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
