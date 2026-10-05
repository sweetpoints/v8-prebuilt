#!/usr/bin/env python3
"""Validate all target provenance and produce deterministic release archives."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

TARGETS = ('android-arm64', 'android-x64', 'ios-arm64', 'ios-simulator-arm64',
           'macos-arm64', 'macos-x64', 'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def contained(root, name):
    p = Path(name)
    if '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or not p.parts or p.as_posix() != name:
        raise ValueError('unsafe artifact path')
    result = root / p
    if result.is_symlink() or not result.is_file() or not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('missing or unsafe artifact file')
    return result

def verify(root, item, key='path'):
    p = contained(root, item[key])
    if digest(p) != item['sha256'] or ('size' in item and p.stat().st_size != item['size']):
        raise ValueError('artifact hash or size mismatch')
    return p

def archive(path, entries):
    with path.open('xb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w') as tar:
                for name, data in sorted(entries.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o644
                    info.mtime = 0
                    tar.addfile(info, io.BytesIO(data))

def package(inputs, pins_path, output, builder_revision):
    if not re.fullmatch(r'[0-9a-f]{40}', builder_revision):
        raise ValueError('builder revision must be full Git SHA')
    pins = json.loads(pins_path.read_text())
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', pins['v8']['version']):
        raise ValueError('invalid V8 version')
    found = {}
    provenance = None
    for manifest_path in sorted(inputs.rglob('manifest.json')):
        m = json.loads(manifest_path.read_text())
        if m.get('schemaVersion') != 1 or m['v8'] != pins['v8'] or m['depotTools'] != pins['depotTools']:
            raise ValueError('manifest provenance differs from pins')
        if 'bridge' in m:
            raise ValueError('pure SDK manifest must not contain a project bridge')
        base = {key: m[key] for key in ('schemaVersion', 'v8', 'depotTools')}
        if provenance is not None and provenance != base:
            raise ValueError('target provenance differs')
        provenance = base
        for target, entry in m['targets'].items():
            if target not in TARGETS or target in found:
                raise ValueError('unknown or duplicate target')
            if entry.get('artifactKind') != 'v8-static-sdk':
                raise ValueError('pure V8 static SDK artifact required')
            verify(manifest_path.parent, entry, 'binary')
            inventory = entry.get('targetFiles', entry.get('files'))
            if not isinstance(inventory, list) or not inventory:
                raise ValueError('build target file inventory required')
            indexed = set()
            for item in inventory:
                if not item['path'].startswith(target + '/') or item['path'] in indexed:
                    raise ValueError('invalid build target file inventory')
                verify(manifest_path.parent, item)
                indexed.add(item['path'])
            actual_files = {p.relative_to(manifest_path.parent).as_posix() for p in (manifest_path.parent / target).rglob('*') if p.is_file()}
            if actual_files - indexed - {target + '/sdk-smoke.json'} or indexed - actual_files:
                raise ValueError('unindexed target artifact file')
            if not entry.get('validation', {}).get('built'):
                raise ValueError('successful native build evidence required')
            if target.startswith('android-'):
                smoke = entry.get('linkSmoke', {})
                inspection = smoke.get('binaryInspection', {})
                alignments = inspection.get('loadSegmentAlignments', [])
                if (not entry.get('validation', {}).get('linkTested') or smoke.get('passed') is not True
                        or inspection.get('elfMachine') != (183 if target == 'android-arm64' else 62)
                        or not alignments or any(x < 16384 for x in alignments)
                        or smoke.get('runtimeExecuted') is not False):
                    raise ValueError('Android final consumer ELF/16KiB link evidence required')
            if target.startswith('ios-'):
                smoke = entry.get('linkSmoke', {})
                expected_platform = 2 if target == 'ios-arm64' else 7
                if (not entry.get('validation', {}).get('linkTested') or smoke.get('passed') is not True
                        or smoke.get('runtimeExecuted') is not False
                        or smoke.get('executableInspection', {}).get('platform') != expected_platform):
                    raise ValueError('iOS static SDK link/platform build evidence required')
            for filename, field in [('args.gn', 'gnArgsSha256'), ('dependencies.txt', 'dependencyInventorySha256'), ('defines.json', 'definesSha256')]:
                if field in entry:
                    verify(manifest_path.parent, {'path': target + '/' + filename, 'sha256': entry[field]})
            if 'header' in entry:
                verify(manifest_path.parent, {'path': entry['header'], 'sha256': entry['headerSha256']})
            for item in entry.get('platformBuildInputs', []):
                verify(manifest_path.parent, item)
            if target.startswith(('macos-', 'linux-', 'windows-')):
                report_path = contained(manifest_path.parent, target + '/sdk-smoke.json')
                report = json.loads(report_path.read_text())
                os_name = {'macos': 'Darwin', 'linux': 'Linux', 'windows': 'Windows'}[target.split('-')[0]]
                machine = report.get('host', {}).get('machine', '').lower()
                expected_machines = ('arm64', 'aarch64') if target.endswith('arm64') else ('x86_64', 'amd64')
                if (report.get('schemaVersion') != 1 or report.get('status') != 'passed'
                        or report.get('version') != pins['v8']['version']
                        or report.get('librarySha256') != entry['sha256']
                        or report.get('host', {}).get('os') != os_name or machine not in expected_machines
                        or report.get('scope') != 'official V8 API SDK consumer'
                        or report.get('nativeConsumerExecuted') is not True
                        or set(report.get('cases', [])) != {'official_api_compile', 'official_api_link', 'official_api_execute'}):
                    raise ValueError('native runtime smoke evidence differs')
                entry = dict(entry, validation=dict(entry.get('validation', {}), runtimeTested=True, sourceCompatibilityTested=False))
            elif entry.get('validation', {}).get('runtimeTested'):
                raise ValueError('Android/iOS runtime evidence not provided')
            for item in m.get('licenses', []):
                verify(manifest_path.parent, item)
            if not m.get('licenses'):
                raise ValueError('license bundle required')
            for item in entry.get('files', []):
                verify(manifest_path.parent, item)
            found[target] = (manifest_path.parent, m, entry)
    if set(found) != set(TARGETS):
        raise ValueError('complete ten-target build required')
    output.mkdir(parents=True, exist_ok=False)
    assets = []
    release = dict(provenance, builderRevision=builder_revision, targets={}, licenses={})
    pins_bytes = (json.dumps(pins, indent=2, sort_keys=True) + '\n').encode()
    for target in TARGETS:
        root, manifest, entry = found[target]
        entry = dict(entry)
        entries = {'pins.json': pins_bytes}
        for p in sorted((root / target).rglob('*')):
            if p.is_symlink():
                raise ValueError('symlinks are not release payloads')
            if p.is_file():
                name = str(p.relative_to(root)).replace('\\', '/')
                entries[name] = contained(root, name).read_bytes()
        for item in manifest['licenses']:
            entries[item['path']] = contained(root, item['path']).read_bytes()
        entry['files'] = [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)} for name, data in sorted(entries.items()) if name.startswith(target + '/')]
        entry['targetFiles'] = entry['files']
        single = dict(provenance, targets={target: entry}, licenses=manifest['licenses'])
        entries['manifest.json'] = (json.dumps(single, indent=2, sort_keys=True) + '\n').encode()
        if target + '/include/v8.h' not in entries or target + '/linking.json' not in entries:
            raise ValueError('consumer V8 headers and linking contract required')
        if any(Path(name).name.startswith('source_v8.') for name in entries):
            raise ValueError('project bridge is not a pure V8 SDK payload')
        name = f"v8-{pins['v8']['version']}-{target}.tar.gz"
        dest = output / name
        archive(dest, entries)
        assets.append({'name': name, 'target': target, 'sha256': digest(dest), 'size': dest.stat().st_size})
        release['targets'][target] = entry
        release['licenses'][target] = manifest['licenses']
    (output / 'pins.json').write_bytes(pins_bytes)
    assets.append({'name': 'pins.json', 'sha256': digest(output / 'pins.json'), 'size': len(pins_bytes)})
    release['assets'] = assets
    manifest = output / 'release-manifest.json'
    manifest.write_text(json.dumps(release, indent=2, sort_keys=True) + '\n')
    check = sorted([*assets, {'name': manifest.name, 'sha256': digest(manifest)}], key=lambda x: x['name'])
    (output / 'SHA256SUMS').write_text(''.join(f"{x['sha256']}  {x['name']}\n" for x in check))
    return release

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--inputs', type=Path, required=True)
    p.add_argument('--pins-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--builder-revision', required=True)
    a = p.parse_args()
    package(a.inputs, a.pins_file, a.output, a.builder_revision)
