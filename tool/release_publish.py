#!/usr/bin/env python3
"""Publish a complete release via draft; never overwrite existing assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.parse

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urllib.parse.urlparse(new_url).scheme != 'https':
            raise ValueError('release download redirect must use HTTPS')
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('invalid repository')
        self.base = 'https://api.github.com/repos/' + repository
        self.token = token

    def request(self, url, method='GET', body=None, content_type='application/json'):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': ('application/octet-stream' if method == 'GET' and content_type == 'application/octet-stream' else 'application/vnd.github+json'),
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'v8-prebuilt-release', 'Content-Type': content_type}
        with urllib.request.build_opener(SafeRedirect()).open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=120) as r:
            result = r.read()
            return json.loads(result) if content_type != 'application/octet-stream' and r.headers.get('Content-Type', '').startswith('application/json') else result

    def by_tag(self, tag):
        try:
            return self.request(self.base + '/releases/tags/' + tag)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # Draft releases are not always returned by the tag endpoint.
            page = 1
            while True:
                values = self.request(self.base + f'/releases?per_page=100&page={page}')
                for value in values:
                    if value['tag_name'] == tag:
                        return value
                if len(values) < 100:
                    return None
                page += 1

    def tag_revision(self, tag):
        try:
            value = self.request(self.base + '/git/ref/tags/' + tag)['object']
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        for _ in range(8):
            if value['type'] == 'commit':
                return value['sha']
            if value['type'] != 'tag':
                raise ValueError('release tag does not reference a commit')
            value = self.request(self.base + '/git/tags/' + value['sha'])['object']
        raise ValueError('release tag nesting too deep')

    def asset_bytes(self, asset):
        # Public release asset bytes, including draft assets authenticated via API.
        return self.request(asset['url'], content_type='application/octet-stream')


def publish(directory, repository, token, github=None):
    manifest = json.loads((directory / 'release-manifest.json').read_text())
    from release_package import TARGETS
    if set(manifest['targets']) != set(TARGETS):
        raise ValueError('complete ten-target release required')
    expected_assets = {f"v8-{manifest['v8']['version']}-{target}.tar.gz" for target in TARGETS} | {'pins.json'}
    if len(manifest['assets']) != len(expected_assets) or {a['name'] for a in manifest['assets']} != expected_assets:
        raise ValueError('complete expected release assets required')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest['builderRevision']):
        raise ValueError('fixed builder revision required')
    for target, entry in manifest['targets'].items():
        validation = entry.get('validation', {})
        if not validation.get('built') or validation.get('sourceCompatibilityTested'):
            raise ValueError('invalid release build verification scope')
        if target.startswith(('linux-', 'windows-', 'macos-')) and validation.get('runtimeTested') is not True:
            raise ValueError('native desktop runtime evidence required')
        if target.startswith(('android-', 'ios-')) and validation.get('runtimeTested'):
            raise ValueError('mobile device runtime evidence not provided')
    names = {a['name'] for a in manifest['assets']} | {'release-manifest.json', 'SHA256SUMS'}
    if {p.name for p in directory.iterdir() if p.is_file()} != names:
        raise ValueError('unexpected or missing release assets')
    for item in manifest['assets']:
        path = directory / item['name']
        if path.name != item['name'] or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256'] or path.stat().st_size != item['size']:
            raise ValueError('local release asset integrity failed')
    checksum_names = names - {'SHA256SUMS'}
    expected_checksums = ''.join(f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(checksum_names))
    if (directory / 'SHA256SUMS').read_text() != expected_checksums:
        raise ValueError('release checksum index differs')
    gh = github or GitHub(repository, token)
    tag = 'v8-' + manifest['v8']['version']
    release = gh.by_tag(tag)
    tag_revision = gh.tag_revision(tag)
    if tag_revision is not None and tag_revision != manifest['builderRevision']:
        raise ValueError('existing tag builder revision differs')
    if release is None:
        release = gh.request(gh.base + '/releases', 'POST', {
            'tag_name': tag, 'target_commitish': manifest['builderRevision'], 'name': tag,
            'draft': True, 'prerelease': False,
            'body': f"Official V8 source {manifest['v8']['revision']}. Ten target SDKs built by {manifest['builderRevision']}. See release-manifest.json and SHA256SUMS for provenance."
        })
    if release.get('prerelease') is not False or release.get('tag_name') != tag or release.get('target_commitish') != manifest['builderRevision']:
        raise ValueError('existing release identity differs')
    if not release['draft'] and tag_revision != manifest['builderRevision']:
        raise ValueError('published release tag identity differs')
    existing = {a['name']: a for a in release['assets']}
    if set(existing) - names:
        raise ValueError('unexpected remote release assets')
    if not release['draft'] and set(existing) != names:
        raise ValueError('published release is incomplete; refusing mutation')
    for name in sorted(names):
        payload = (directory / name).read_bytes()
        if name in existing:
            actual = gh.asset_bytes(existing[name])
            if not isinstance(actual, bytes) or hashlib.sha256(actual).digest() != hashlib.sha256(payload).digest():
                raise ValueError('existing release asset differs; refusing overwrite')
        elif release['draft']:
            url = release['upload_url'].split('{', 1)[0] + '?name=' + name
            gh.request(url, 'POST', payload, 'application/octet-stream')
    if release['draft']:
        # Re-fetch and verify every uploaded object before making it public.
        ready = gh.request(gh.base + '/releases/' + str(release['id']))
        if {a['name'] for a in ready['assets']} != names:
            raise ValueError('draft upload incomplete')
        for item in ready['assets']:
            actual = gh.asset_bytes(item)
            if not isinstance(actual, bytes) or actual != (directory / item['name']).read_bytes():
                raise ValueError('uploaded release asset verification failed')
        gh.request(gh.base + '/releases/' + str(release['id']), 'PATCH', {'draft': False, 'make_latest': 'true'})
    return tag

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--repository', required=True)
    a = p.parse_args()
    token = os.environ.get('GH_TOKEN')
    if not token:
        p.error('GH_TOKEN is required')
    print(publish(a.directory, a.repository, token))
