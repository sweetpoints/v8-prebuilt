#!/usr/bin/env python3
"""Validate all target provenance and produce deterministic release archives."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import tarfile
from sdk_smoke import PROBE
from release_pins import release_metadata

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

def verify_windows_arm_probe(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Windows ARM64 consumer PE evidence required')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if offset < 64 or offset > len(data) - 24 or data[offset:offset + 4] != b'PE\0\0':
        raise ValueError('Windows ARM64 consumer PE header invalid')
    if struct.unpack_from('<H', data, offset + 4)[0] != 0xaa64:
        raise ValueError('Windows ARM64 consumer must execute an ARM64 PE, not an emulated x64 PE')

def verify_feature_profile(root, target, sdk_artifact_revision=0):
    linking = json.loads(contained(root, target + '/linking.json').read_text())
    args = contained(root, target + '/args.gn').read_text()
    verify_feature_profile_data(linking, args, target, sdk_artifact_revision)

def verify_feature_profile_data(linking, args, target, sdk_artifact_revision=0):
    ios = target.startswith('ios-')
    expected = {'internationalization': True, 'temporal': True, 'icuData': 'embedded',
                'jit': False if ios else 'upstream-default',
                'webAssembly': False if ios else 'upstream-default',
                'experimentalRuntimeFlags': []}
    profile = linking.get('featureProfile')
    if (profile != expected or any(type(profile[key]) is not type(value) for key, value in expected.items())):
        raise ValueError('full SDK feature profile required')
    flags = {'v8_enable_i18n_support': True, 'v8_enable_temporal_support': True,
             'icu_use_data_file': False}
    if ios:
        flags.update(v8_jitless=True, v8_enable_webassembly=False)
    if sdk_artifact_revision and target.startswith('macos-'):
        flags.update(use_allocator_shim=False, use_partition_alloc_as_malloc=False)
    for flag, value in flags.items():
        assignments = re.findall(r'^\s*' + re.escape(flag) + r'\s*=\s*(.*?)\s*$', args, re.MULTILINE)
        if assignments != ['true' if value else 'false']:
            raise ValueError('full SDK GN feature configuration required')
    if sdk_artifact_revision and target.startswith('macos-'):
        # A manifest claiming upstream defaults cannot hide a reduced Mac
        # build behind the two allocator isolation flags. Omitted flags retain
        # upstream defaults; explicit assignments must preserve that profile.
        defaults = {'v8_jitless': False, 'v8_enable_webassembly': True,
                    'v8_enable_sandbox': True, 'v8_enable_pointer_compression': True,
                    'use_partition_alloc': True}
        for flag, value in defaults.items():
            assignments = re.findall(r'^\s*' + re.escape(flag) + r'\s*=\s*(.*?)\s*$', args, re.MULTILINE)
            if assignments and assignments != ['true' if value else 'false']:
                raise ValueError('full SDK GN feature configuration required')

def verify_library_grouping(target, linking, command):
    grouping = linking.get('staticLibraryGrouping', 'normal')
    rescan_target = target.startswith(('linux-', 'android-'))
    if grouping != ('rescan' if rescan_target else 'normal'):
        raise ValueError('SDK static library grouping policy differs from target')
    if not rescan_target:
        if isinstance(command, list) and any(value in command for value in ('-Wl,--start-group', '-Wl,--end-group')):
            raise ValueError('SDK archive rescan is unsupported for target')
        return
    libraries = linking.get('libraries')
    if (not isinstance(command, list) or any(not isinstance(value, str) for value in command)
            or not isinstance(libraries, list) or not libraries
            or any(not isinstance(value, str) for value in libraries)
            or len(set(libraries)) != len(libraries)
            or command.count('-Wl,--start-group') != 1 or command.count('-Wl,--end-group') != 1):
        raise ValueError('SDK archive rescan command evidence required')
    start, end = command.index('-Wl,--start-group'), command.index('-Wl,--end-group')
    grouped = command[start + 1:end] if start < end else []
    matches = lambda argument, library: argument == library or argument.endswith('/' + library)
    if (len(grouped) != len(libraries)
            or any(not matches(argument, library) for argument, library in zip(grouped, libraries))
            or any(matches(argument, library) for argument in command[:start] + command[end + 1:]
                   for library in libraries)):
        raise ValueError('SDK archive rescan must enclose all libraries in contract order')

def ios_bek_dependency(data):
    if len(data) < 32 or struct.unpack_from('<I', data)[0] != 0xfeedfacf:
        raise ValueError('iOS BrowserEngineKit consumer Mach-O evidence required')
    cpu, _, kind, count, size = struct.unpack_from('<5I', data, 4)
    if cpu != 0x100000c or kind != 2 or size > len(data) - 32:
        raise ValueError('iOS BrowserEngineKit consumer Mach-O evidence required')
    offset, found = 32, []
    for _ in range(count):
        if offset + 8 > 32 + size:
            raise ValueError('invalid iOS consumer load commands')
        command, length = struct.unpack_from('<II', data, offset)
        if length < 8 or offset + length > 32 + size:
            raise ValueError('invalid iOS consumer load commands')
        if command in (0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023):
            if length < 24:
                raise ValueError('invalid iOS consumer dylib load command')
            name_offset = struct.unpack_from('<I', data, offset + 8)[0]
            if not 24 <= name_offset < length:
                raise ValueError('invalid iOS consumer dylib name')
            name = data[offset + name_offset:offset + length].split(b'\0', 1)[0].decode('utf-8')
            if Path(name).name == 'BrowserEngineKit':
                if command != 0x80000018:
                    raise ValueError('iOS BrowserEngineKit must load weakly')
                found.append(name)
        offset += length
    if offset != 32 + size or len(found) != 1:
        raise ValueError('exactly one iOS BrowserEngineKit weak dependency required')
    return {'framework': 'BrowserEngineKit', 'loadCommand': 'LC_LOAD_WEAK_DYLIB', 'path': found[0]}

def verify_ios_bek(root, target, linking, entry, indexed):
    policy = linking.get('browserEngineKitPolicy')
    expected = {'schemaVersion': 1, 'framework': 'BrowserEngineKit', 'linkage': 'weak',
                'frameworkAvailableFromIOS': '17.4', 'requiredUndefinedSymbols': False,
                'nmTool': 'third_party/llvm-build/Release+Asserts/bin/llvm-nm'}
    if (not isinstance(policy, dict) or any(policy.get(key) != value or type(policy.get(key)) is not type(value)
                                           for key, value in expected.items())):
        raise ValueError('iOS BrowserEngineKit weak policy required')
    policy_name = target + '/validation/browser-engine-kit-policy.json'
    if policy_name not in indexed or json.loads(contained(root, policy_name).read_text()) != policy:
        raise ValueError('indexed iOS BrowserEngineKit policy evidence differs')
    archives = policy.get('checkedArchives')
    libraries = linking.get('libraries', [])
    if (not isinstance(archives, list) or not archives or any(not isinstance(item, dict) for item in archives)
            or not isinstance(libraries, list) or any(not isinstance(name, str) for name in libraries)
            or len(archives) != len(libraries)
            or {item.get('path') for item in archives} != set(libraries)
            or {path.relative_to(root / target).as_posix() for path in (root / target / 'lib').glob('*.a')} != set(libraries)):
        raise ValueError('iOS BrowserEngineKit must check every SDK library')
    for item in archives:
        if not re.fullmatch('[0-9a-f]{64}', item.get('undefinedSymbolsSha256', '')):
            raise ValueError('iOS BrowserEngineKit undefined-symbol evidence required')
        verify(root, {'path': target + '/' + item['path'], 'sha256': item['sha256']})
    options = linking.get('linkOptions', [])
    if options.count('BrowserEngineKit') != 1 or options.index('BrowserEngineKit') == 0 or options[options.index('BrowserEngineKit') - 1] != '-weak_framework':
        raise ValueError('iOS BrowserEngineKit weak link option required')
    smoke = entry['linkSmoke']
    if smoke.get('linkingSha256') != digest(contained(root, target + '/linking.json')):
        raise ValueError('iOS BrowserEngineKit linking evidence differs')
    executable = target + '/' + smoke.get('executable', '')
    if executable not in indexed or smoke.get('browserEngineKitDependency') != ios_bek_dependency(contained(root, executable).read_bytes()):
        raise ValueError('iOS BrowserEngineKit actual weak consumer evidence differs')

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

def package(inputs, pins_path, output, builder_revision, reuse_plan=None):
    if not re.fullmatch(r'[0-9a-f]{40}', builder_revision):
        raise ValueError('builder revision must be full Git SHA')
    pins = json.loads(pins_path.read_text())
    metadata = release_metadata(reuse_plan or {}, pins['v8']['version'])
    sdk_revision = metadata.get('sdkArtifactRevision', 0)
    if reuse_plan is not None:
        from release_resume import validate_generic_plan
        validate_generic_plan(reuse_plan, pins, builder_revision)
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
                if target == 'windows-arm64':
                    verify_windows_arm_probe(contained(manifest_path.parent, probe_name))
                allowed_extras.add(probe_name)
                sidecar_name = probe_name + '.json'
                cross_target = target in ('linux-arm64', 'windows-arm64')
                if cross_target and sidecar_name not in actual_files:
                    raise ValueError('cross-built SDK consumer compile proof required')
                if sidecar_name in actual_files:
                    prior = json.loads(contained(manifest_path.parent, sidecar_name).read_text())
                    proof_reports[sidecar_name] = prior
                    for key in ('version', 'linkingSha256', 'librarySha256', 'libraries', 'probeSha256', 'probeSourceSha256', 'compiler', 'compileCommand', 'sysroot'):
                        if prior.get(key) != report.get(key):
                            raise ValueError('cross-built SDK consumer proof differs')
                    if (not cross_target or prior.get('status') != 'compiled'
                            or prior.get('host') != report.get('compileHost')
                            or prior.get('host', {}).get('os') != ('Linux' if target == 'linux-arm64' else 'Windows')
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
            verify_feature_profile(manifest_path.parent, target, sdk_revision)
            linking = json.loads(contained(manifest_path.parent, target + '/linking.json').read_text())
            if target.startswith('android-'):
                if entry['linkSmoke'].get('linkingSha256') != digest(contained(manifest_path.parent, target + '/linking.json')):
                    raise ValueError('Android SDK link command contract evidence differs')
                verify_library_grouping(target, linking, entry['linkSmoke'].get('command'))
            elif target.startswith('ios-'):
                verify_library_grouping(target, linking, None)
                verify_ios_bek(manifest_path.parent, target, linking, entry, indexed)
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
                verify_library_grouping(target, linking, command)
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
    release = dict(provenance, builderRevision=builder_revision, packagingRevision=builder_revision, targets={}, licenses={})
    release.update(metadata)
    pins_bytes = (json.dumps(pins, indent=2, sort_keys=True) + '\n').encode()
    for target in TARGETS:
        root, manifest, entry, expected_files, proof_reports = found[target]
        entry = dict(entry)
        entry['producerRevision'] = builder_revision if reuse_plan is None else reuse_plan['targets'][target]['producerRevision']
        if reuse_plan is not None and 'producerInputHashes' in reuse_plan['targets'][target]:
            entry['producerInputHashes'] = reuse_plan['targets'][target]['producerInputHashes']
        if reuse_plan is not None and 'reuseProvenance' in reuse_plan['targets'][target]:
            entry['reuseProvenance'] = reuse_plan['targets'][target]['reuseProvenance']
            entry['producerInputHashes'] = reuse_plan['producerRecipes'][entry['producerRevision']]
        if reuse_plan is not None:
            entry['producerInputHashSemantics'] = 'git-blob-sha256'
            runtime_name = {'linux-arm64': 'linux-arm-runtime', 'windows-arm64': 'windows-arm-runtime'}.get(target)
            if runtime_name in reuse_plan.get('runtimeAssets', {}):
                runtime = reuse_plan['runtimeAssets'][runtime_name]
                entry['runtimeReuseProvenance'] = {key: runtime[key] for key in
                    ('runId', 'id', 'sha256', 'producerRevision', 'sdkArtifactId', 'sdkArtifactSha256')}
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
        single = dict(provenance, targets={target: entry}, licenses=manifest['licenses'], **metadata)
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
    p.add_argument('--reuse-plan', type=Path)
    a = p.parse_args()
    package(a.inputs, a.pins_file, a.output, a.builder_revision,
            json.loads(a.reuse_plan.read_text()) if a.reuse_plan else None)
