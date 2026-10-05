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
from sdk_smoke import PROBE

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

def verify_feature_profile(root, target):
    linking = json.loads(contained(root, target + '/linking.json').read_text())
    ios = target.startswith('ios-')
    expected = {'internationalization': True, 'temporal': True, 'icuData': 'embedded',
                'jit': False if ios else 'upstream-default',
                'webAssembly': False if ios else 'upstream-default',
                'experimentalRuntimeFlags': []}
    profile = linking.get('featureProfile')
    if (profile != expected or any(type(profile[key]) is not type(value) for key, value in expected.items())):
        raise ValueError('full SDK feature profile required')
    args = contained(root, target + '/args.gn').read_text()
    flags = {'v8_enable_i18n_support': True, 'v8_enable_temporal_support': True,
             'icu_use_data_file': False}
    if ios:
        flags.update(v8_jitless=True, v8_enable_webassembly=False)
    for flag, value in flags.items():
        assignments = re.findall(r'^\s*' + re.escape(flag) + r'\s*=\s*(.*?)\s*$', args, re.MULTILINE)
        if assignments != ['true' if value else 'false']:
            raise ValueError('full SDK GN feature configuration required')

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
            binary = verify(manifest_path.parent, entry, 'binary')
            if binary.suffix not in ('.a', '.lib') or binary.read_bytes()[:8] != b'!<arch>\n':
                raise ValueError('self-contained V8 static archive required')
            proof_reports = {}
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
            extras = actual_files - indexed
            allowed_extras = {target + '/sdk-smoke.json'}
            if target.startswith(('macos-', 'linux-', 'windows-')):
                report = json.loads(contained(manifest_path.parent, target + '/sdk-smoke.json').read_text())
                proof_reports[target + '/sdk-smoke.json'] = report
                probe_name = target + '/' + report['probe']
                if report['probe'] not in ('validation/sdk-probe', 'validation/sdk-probe.exe'):
                    raise ValueError('unexpected SDK consumer probe path')
                verify(manifest_path.parent, {'path': probe_name, 'sha256': report['probeSha256']})
                allowed_extras.add(probe_name)
                sidecar_name = probe_name + '.json'
                if target == 'linux-arm64' and sidecar_name not in actual_files:
                    raise ValueError('cross-built SDK consumer compile proof required')
                if sidecar_name in actual_files:
                    prior = json.loads(contained(manifest_path.parent, sidecar_name).read_text())
                    proof_reports[sidecar_name] = prior
                    for key in ('version', 'linkingSha256', 'librarySha256', 'libraries', 'probeSha256', 'probeSourceSha256', 'compiler', 'compileCommand', 'sysroot'):
                        if prior.get(key) != report.get(key):
                            raise ValueError('cross-built SDK consumer proof differs')
                    if (target != 'linux-arm64' or prior.get('status') != 'compiled'
                            or prior.get('host') != report.get('compileHost')
                            or prior.get('host', {}).get('os') != 'Linux'
                            or prior.get('host', {}).get('machine', '').lower() not in ('x86_64', 'amd64')
                            or prior.get('nativeConsumerExecuted') is not False
                            or prior.get('cases') != ['official_api_compile', 'official_api_link']):
                        raise ValueError('invalid cross-built SDK consumer proof')
                    allowed_extras.add(sidecar_name)
            if extras - allowed_extras or indexed - actual_files:
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
            for filename, field in [('args.gn', 'gnArgsSha256'), ('dependencies.txt', 'dependencyInventorySha256'), ('defines.json', 'definesSha256'), ('linking.json', 'linkingSha256')]:
                if field in entry:
                    verify(manifest_path.parent, {'path': target + '/' + filename, 'sha256': entry[field]})
            if 'header' in entry:
                verify(manifest_path.parent, {'path': entry['header'], 'sha256': entry['headerSha256']})
            for item in entry.get('platformBuildInputs', []):
                verify(manifest_path.parent, item)
            verify_feature_profile(manifest_path.parent, target)
            if target.startswith(('macos-', 'linux-', 'windows-')):
                report_path = contained(manifest_path.parent, target + '/sdk-smoke.json')
                report = json.loads(report_path.read_text())
                linking = json.loads(contained(manifest_path.parent, target + '/linking.json').read_text())
                if report.get('linkingSha256') != digest(contained(manifest_path.parent, target + '/linking.json')):
                    raise ValueError('SDK linking contract evidence differs')
                libraries = report.get('libraries')
                if not isinstance(libraries, dict) or set(libraries) != set(linking.get('libraries', [])):
                    raise ValueError('SDK library set evidence differs')
                if not linking.get('libraries') or target + '/' + linking['libraries'][0] != entry['binary']:
                    raise ValueError('SDK primary monolith linking order differs')
                for relative, checksum in libraries.items():
                    verify(manifest_path.parent, {'path': target + '/' + relative, 'sha256': checksum})
                if not report.get('compiler') or report.get('probeSourceSha256') != hashlib.sha256(PROBE.encode()).hexdigest():
                    raise ValueError('SDK consumer compile evidence missing')
                command = report.get('compileCommand')
                if not isinstance(command, list) or not command or any(not isinstance(value, str) for value in command):
                    raise ValueError('SDK consumer compile command missing')
                for field in ('defines', 'compileOptions', 'linkOptions', 'systemLibraries'):
                    if not isinstance(linking.get(field), list) or any(not isinstance(value, str) for value in linking[field]):
                        raise ValueError('invalid SDK linking options')
                style = linking.get('compilerStyle', 'clang-cl' if target.startswith('windows-') else 'clang++')
                options = linking['compileOptions'] + linking['linkOptions']
                options += [('/D' if style == 'clang-cl' else '-D') + value for value in linking['defines']]
                options += [value if style == 'clang-cl' else '-l' + value for value in linking['systemLibraries']]
                if any(value not in command for value in options):
                    raise ValueError('SDK consumer command differs from linking contract')
                if entry.get('toolchain', {}).get('clang') and report['compiler'] != entry['toolchain']['clang']:
                    raise ValueError('SDK consumer compiler differs from pinned toolchain')
                if target.startswith('linux-'):
                    sysroot = report.get('sysroot')
                    arch = 'arm64' if target == 'linux-arm64' else 'amd64'
                    if not isinstance(sysroot, str) or not sysroot.endswith('debian_bullseye_' + arch + '-sysroot') or '--sysroot=' + sysroot not in command:
                        raise ValueError('SDK consumer target sysroot evidence missing')
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
            found[target] = (manifest_path.parent, m, entry, actual_files, proof_reports)
    if set(found) != set(TARGETS):
        raise ValueError('complete ten-target build required')
    output.mkdir(parents=True, exist_ok=False)
    assets = []
    release = dict(provenance, builderRevision=builder_revision, targets={}, licenses={})
    pins_bytes = (json.dumps(pins, indent=2, sort_keys=True) + '\n').encode()
    for target in TARGETS:
        root, manifest, entry, expected_files, proof_reports = found[target]
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
        if {name for name in entries if name.startswith(target + '/')} != expected_files:
            raise ValueError('SDK target payload changed during packaging')
        for item in entry.get('targetFiles', entry.get('files', [])) + manifest['licenses']:
            data = entries[item['path']]
            if hashlib.sha256(data).hexdigest() != item['sha256'] or ('size' in item and len(data) != item['size']):
                raise ValueError('SDK payload changed during packaging')
        if hashlib.sha256(entries[entry['binary']]).hexdigest() != entry['sha256']:
            raise ValueError('SDK primary monolith changed during packaging')
        for name, report in proof_reports.items():
            if json.loads(entries[name]) != report:
                raise ValueError('SDK consumer proof changed during packaging')
            probe_key = target + '/' + report.get('probe', 'validation/sdk-probe')
            if hashlib.sha256(entries[probe_key]).hexdigest() != report['probeSha256']:
                raise ValueError('SDK consumer executable changed during packaging')
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
