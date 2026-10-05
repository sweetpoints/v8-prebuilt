#!/usr/bin/env python3
"""Pinned iOS static SDK builder; device and simulator ARM64 are distinct platforms.

Called by build.py. This module never emits a loadable dylib. Consumers link the
combined C ABI archive and Apple's libc++; V8 public headers/monolith are supplied
for native embedders who need the upstream API instead. Do not link both archives.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import tempfile

import build as common

TARGETS = ('ios-arm64', 'ios-simulator-arm64')
PLATFORMS = {'ios-arm64': 2, 'ios-simulator-arm64': 7}
SDKS = {'ios-arm64': 'iphoneos', 'ios-simulator-arm64': 'iphonesimulator'}
STATIC_GN = '''static_library("source_v8") {
  output_name = "source_v8_bridge"
  sources = [ "//source_v8/source_v8.cpp" ]
  deps = [ "//:v8_monolith" ]
  configs += [ "//:external_config" ]
  configs -= [ "//build/config/clang:find_bad_constructs" ]
}
'''
LINK_SMOKE = '''#include "source_v8.h"
int main() {
  void *runtime = sv8_create(5000, 64);
  if (!runtime || !sv8_version()) return 1;
  sv8_start(runtime, "1 + 2", "{}", "");
  char *result = sv8_poll(runtime);
  sv8_free(result);
  sv8_cancel(runtime);
  sv8_destroy(runtime);
  return 0;
}
'''


def configuration(target, pins):
    if target not in TARGETS:
        raise ValueError('Unsupported iOS target: ' + target)
    config = pins['targets'][target]
    if config['cpu'] != 'arm64' or config.get('environment') != ('device' if target == 'ios-arm64' else 'simulator'):
        raise ValueError('iOS target CPU/environment contract mismatch')
    minimum = config['minIOS']
    if not isinstance(minimum, str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', minimum):
        raise ValueError('iOS minimum deployment version must be a version string')
    if tuple(map(int, minimum.split('.')[:2])) < (15, 0):
        raise ValueError('This SDK requires iOS 15.0 or newer')
    return config


def require_host(target):
    if target not in TARGETS:
        raise ValueError('Unsupported iOS target: ' + target)
    if (platform.system(), platform.machine()) != ('Darwin', 'arm64'):
        raise ValueError('iOS builds require macOS ARM64 with full Xcode')


def target_os(target):
    if target not in TARGETS:
        raise ValueError('Unsupported iOS target: ' + target)
    return 'ios'


def output_directory(source, target):
    if target not in TARGETS:
        raise ValueError('Unsupported iOS target: ' + target)
    return source / ('out/source_v8_' + target.replace('-', '_'))


def gn_arguments(target, pins):
    config = configuration(target, pins)
    args = {
        'target_os': 'ios', 'target_cpu': 'arm64', 'v8_target_cpu': 'arm64',
        'target_environment': config['environment'], 'target_platform': 'iphoneos',
        'ios_deployment_target': config['minIOS'], 'use_system_xcode': True,
        'is_debug': False, 'is_component_build': False, 'v8_monolithic': True,
        'v8_monolithic_for_shared_library': False, 'v8_use_external_startup_data': False,
        'use_custom_libcxx': False, 'v8_enable_i18n_support': False,
        'v8_enable_temporal_support': False, 'v8_enable_pointer_compression': False,
        'v8_enable_sandbox': False, 'v8_jitless': True,
        'v8_enable_turbofan': False, 'v8_enable_maglev': False,
        'v8_enable_sparkplug': False, 'v8_enable_webassembly': False,
        'use_remoteexec': False, 'use_lld': False, 'symbol_level': 0,
    }
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


def platform_source(source_text):
    """A separately hashed overlay leaves the base bridge source bytes unchanged."""
    anchor = '    V8::InitializePlatform(runtime_platform.get());'
    if source_text.count(anchor) != 1:
        raise ValueError('Expected one V8 initialization boundary for iOS jitless overlay')
    return source_text.replace(anchor, '    V8::SetFlagsFromString("--jitless");\n' + anchor)


def inspect_macho(data, target, filetype=1):
    if target not in TARGETS:
        raise ValueError('Unsupported iOS target')
    if len(data) < 32 or struct.unpack_from('<I', data)[0] != 0xfeedfacf:
        raise ValueError('Expected a thin little-endian Mach-O 64 binary')
    cpu, _, actual_type, commands, commands_size = struct.unpack_from('<5I', data, 4)
    if cpu != 0x0100000c or actual_type != filetype:
        raise ValueError('iOS archive must contain ARM64 Mach-O objects, not dylibs/host binaries')
    if 32 + commands_size > len(data):
        raise ValueError('Truncated Mach-O load commands')
    offset = 32
    build_platform = None
    minimum = None
    for _ in range(commands):
        if offset + 8 > 32 + commands_size:
            raise ValueError('Truncated Mach-O load command')
        command, size = struct.unpack_from('<II', data, offset)
        if size < 8 or offset + size > 32 + commands_size:
            raise ValueError('Invalid Mach-O load command size')
        if command == 0x32:
            if size < 24 or build_platform is not None:
                raise ValueError('Invalid or duplicate LC_BUILD_VERSION')
            build_platform, minimum = struct.unpack_from('<II', data, offset + 8)
        offset += size
    if offset != 32 + commands_size or build_platform != PLATFORMS[target]:
        raise ValueError('Mach-O device/simulator platform does not match target')
    return {'platform': build_platform, 'minIOS': '.'.join(map(str, [minimum >> 16, (minimum >> 8) & 255, minimum & 255]))}


def inspect_static_archive(path, target):
    data = path.read_bytes()
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('iOS SDK requires a self-contained static ar archive')
    offset, objects, minima = 8, 0, set()
    while offset < len(data):
        if offset + 60 > len(data) or data[offset + 58:offset + 60] != b'`\n':
            raise ValueError('Invalid static archive member header')
        header = data[offset:offset + 60]
        try:
            size = int(header[48:58].strip())
        except ValueError as error:
            raise ValueError('Invalid archive member size') from error
        if size < 0 or offset + 60 + size > len(data):
            raise ValueError('Truncated archive member')
        name = header[:16].decode('ascii').strip().rstrip('/')
        member = data[offset + 60:offset + 60 + size]
        if name.startswith('#1/'):
            length = int(name[3:])
            if length < 0 or length > len(member):
                raise ValueError('Invalid BSD archive filename')
            name = member[:length].decode('utf-8').rstrip('\0')
            member = member[length:]
        if name not in ('', '/', '__.SYMDEF', '__.SYMDEF SORTED', '__.SYMDEF_64', '__.SYMDEF_64 SORTED'):
            inspection = inspect_macho(member, target)
            minima.add(inspection['minIOS'])
            objects += 1
        offset += 60 + size + (size & 1)
    if offset != len(data) or not objects:
        raise ValueError('Empty or malformed iOS static archive')
    return {'format': 'static-ar', 'architecture': 'arm64', 'platform': PLATFORMS[target],
            'objectCount': objects, 'objectMinIOS': sorted(minima)}


def validate_binary(path, source, target, env, run=common.run):
    inspection = inspect_static_archive(path, target)
    output = run(['xcrun', 'nm', '-gU', path], source, env, capture=True)
    symbols = {line.split()[-1].removeprefix('_') for line in output.splitlines() if line.strip()}
    if not common.BRIDGE_EXPORTS.issubset(symbols):
        raise ValueError('iOS combined archive is missing sv8 C ABI functions')
    inspection['bridgeExports'] = sorted(common.BRIDGE_EXPORTS)
    return inspection


def output_for(source, out, depot, env, label):
    outputs = json.loads(common.run([depot / 'gn', 'desc', out, label, 'outputs', '--format=json',
                                    '--root-target=//source_v8:source_v8'], source, env, capture=True))
    archives = [source / value.removeprefix('//') for value in outputs if value.endswith('.a')]
    if len(archives) != 1 or not archives[0].is_file() or not archives[0].resolve().is_relative_to(out.resolve()):
        raise ValueError('Expected exactly one built archive inside target output: ' + label)
    return archives[0]


def link_contract(target, pins, defines):
    config = configuration(target, pins)
    return {
        'kind': 'ios-static-sdk', 'target': target, 'architecture': 'arm64',
        'environment': config['environment'], 'sdk': SDKS[target], 'minIOS': config['minIOS'],
        'clangTarget': 'arm64-apple-ios' + config['minIOS'] + ('-simulator' if target.endswith('simulator-arm64') else ''),
        'bridgeArchive': 'lib/libsource_v8.a', 'nativeV8Archive': 'lib/libv8_monolith.a',
        'linkExactlyOneArchive': True,
        'bridgeLinkArguments': ['-Wl,-force_load,lib/libsource_v8.a', '-lc++', '-framework', 'Foundation', '-framework', 'CoreFoundation']
            + [flag for symbol in sorted(common.BRIDGE_EXPORTS)
               for flag in ('-Wl,-u,_' + symbol, '-Wl,-exported_symbol,_' + symbol)],
        'includeDirectories': ['include', 'include/v8'], 'v8PublicDefines': defines,
        'cxxStandard': 'c++20', 'stdlib': 'Xcode SDK libc++ (system; not bundled)',
        'runtimeFlags': ['--jitless'], 'compileTimeJitless': True, 'webAssembly': False,
        'externalStartupData': False, 'dynamicLoading': False,
    }


def link_smoke(directory, source, target, pins, env):
    contract = json.loads((directory / 'linking.json').read_text())
    evidence = directory / 'validation'
    evidence.mkdir()
    probe = evidence / 'link-smoke.cpp'
    probe.write_text(LINK_SMOKE)
    sdk = common.run(['xcrun', '--sdk', SDKS[target], '--show-sdk-path'], source, env, capture=True).strip()
    output = evidence / 'link-smoke'
    common.run(['xcrun', '--sdk', SDKS[target], 'clang++', '-std=c++20', '-target', contract['clangTarget'],
                '-isysroot', sdk, '-I', directory / 'include', probe,
                '-Wl,-force_load,' + str(directory / 'lib/libsource_v8.a'), '-lc++',
                '-framework', 'Foundation', '-framework', 'CoreFoundation', '-Wl,-dead_strip',
                *[flag for symbol in sorted(common.BRIDGE_EXPORTS)
                  for flag in ('-Wl,-u,_' + symbol, '-Wl,-exported_symbol,_' + symbol)], '-o', output], source, env)
    inspection = inspect_macho(output.read_bytes(), target, filetype=2)
    exported_text = common.run(['xcrun', 'nm', '-gU', output], source, env, capture=True)
    exports = {line.split()[-1].removeprefix('_') for line in exported_text.splitlines() if line.strip()}
    if not common.BRIDGE_EXPORTS.issubset(exports):
        raise ValueError('Dead-stripped iOS executable is missing C ABI symbols')
    inspection['bridgeExports'] = sorted(common.BRIDGE_EXPORTS)
    result = {'passed': True, 'executableInspection': inspection, 'runtimeExecuted': False,
              'source': 'validation/link-smoke.cpp', 'executable': 'validation/link-smoke'}
    (evidence / 'link-smoke.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def inventory(directory, prefix):
    return [{'path': (Path(prefix) / path.relative_to(directory)).as_posix(), 'sha256': common.sha(path),
             'size': path.stat().st_size} for path in sorted(directory.rglob('*')) if path.is_file()]


def build(source, depot, env, target, jobs, pins, output_root=None):
    require_host(target)
    configuration(target, pins)
    if common.source_version(source) != pins['v8']['version']:
        raise ValueError('Official V8 source version differs from pins')
    base_digest = common.bridge_digest()
    overlay = source / 'source_v8'
    overlay.mkdir(exist_ok=True)
    cpp = platform_source((common.ROOT / 'src/source_v8.cpp').read_text())
    (overlay / 'source_v8.cpp').write_text(cpp)
    (overlay / 'BUILD.gn').write_text(STATIC_GN)
    args = gn_arguments(target, pins)
    builder_source = Path(__file__).read_text()
    derived = {'source_v8.cpp': cpp, 'BUILD.gn': STATIC_GN, 'args.gn': args, 'ios.py': builder_source}
    overlay_digest = hashlib.sha256(json.dumps(derived, sort_keys=True).encode()).hexdigest()
    out = output_directory(source, target)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'args.gn').write_text(args)
    common.run([depot / 'gn', 'gen', out, '--root-target=//source_v8:source_v8', '--fail-on-unused-args'], source, env)
    common.run([depot / 'autoninja', '-C', out, '-j', jobs, 'source_v8:source_v8'], source, env)
    bridge = output_for(source, out, depot, env, '//source_v8:source_v8')
    monolith = output_for(source, out, depot, env, '//:v8_monolith')
    defines = json.loads(common.run([depot / 'gn', 'desc', out, '//source_v8:source_v8', 'defines', '--format=json',
                                    '--root-target=//source_v8:source_v8'], source, env, capture=True))
    artifact = Path(common.DEFAULT_OUTPUT_ROOT if output_root is None else output_root) / pins['v8']['revision']
    artifact.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.' + target + '-', dir=artifact) as staging:
        directory = Path(staging)
        (directory / 'lib').mkdir()
        # Apple's libtool flattens both archives into a standalone consumer archive.
        common.run(['xcrun', 'libtool', '-static', '-o', directory / 'lib/libsource_v8.a', bridge, monolith], source, env)
        shutil.copyfile(monolith, directory / 'lib/libv8_monolith.a')
        inspection = validate_binary(directory / 'lib/libsource_v8.a', source, target, env)
        v8_inspection = inspect_static_archive(directory / 'lib/libv8_monolith.a', target)
        for minimum in inspection['objectMinIOS'] + v8_inspection['objectMinIOS']:
            expected = tuple(map(int, pins['targets'][target]['minIOS'].split('.')))
            expected = expected + (0,) * (3 - len(expected))
            if tuple(map(int, minimum.split('.'))) > expected:
                raise ValueError('An archive member requires newer iOS than the SDK deployment contract')
        build_inputs = directory / 'build-inputs'
        build_inputs.mkdir()
        for name, value in derived.items():
            (build_inputs / name).write_text(value)
        include = directory / 'include'
        shutil.copytree(source / 'include', include / 'v8')
        shutil.copyfile(common.ROOT / 'src/source_v8.h', include / 'source_v8.h')
        (directory / 'linking.json').write_text(json.dumps(link_contract(target, pins, defines), indent=2) + '\n')
        smoke = link_smoke(directory, source, target, pins, env)
        (directory / 'args.gn').write_text(args)
        (directory / 'defines.json').write_text(json.dumps(defines, indent=2) + '\n')
        (directory / 'dependencies.txt').write_text(common.run([depot / 'gclient', 'revinfo', '--actual'], source.parent, env, capture=True))
        clang = source / 'third_party/llvm-build/Release+Asserts/bin/clang++'
        toolchain = {
            'clang': common.run([clang, '--version'], source, env, capture=True).strip(),
            'gn': common.run([depot / 'gn', '--version'], source, env, capture=True).strip(),
            'xcode': common.run(['xcodebuild', '-version'], source, env, capture=True).strip(),
            'iosSdk': common.run(['xcrun', '--sdk', SDKS[target], '--show-sdk-version'], source, env, capture=True).strip(),
        }
        with (artifact / '.publish.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if common.bridge_digest() != base_digest or (overlay / 'source_v8.cpp').read_text() != cpp or (overlay / 'BUILD.gn').read_text() != STATIC_GN or (out / 'args.gn').read_text() != args or Path(__file__).read_text() != builder_source:
                raise ValueError('Base bridge or iOS overlay changed during build')
            manifest_path = artifact / 'manifest.json'
            previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
            bridge_contract = {'abi': 1, 'sourceSha256': base_digest}
            if previous and (previous['v8'] != pins['v8'] or previous['depotTools'] != pins['depotTools'] or previous['bridge'] != bridge_contract):
                raise ValueError('Existing artifact manifest provenance differs')
            licenses = common.package_licenses(source, artifact, previous.get('licenses', []) if previous else [])
            destination = artifact / target
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(directory, destination)
            binary = destination / 'lib/libsource_v8.a'
            entry = {
                'binary': (Path(target) / 'lib/libsource_v8.a').as_posix(), 'sha256': common.sha(binary), 'size': binary.stat().st_size,
                'host': {'os': platform.system(), 'cpu': platform.machine()}, 'gnArgs': args,
                'gnArgsSha256': common.sha(destination / 'args.gn'), 'depsSha256': common.sha(source / 'DEPS'),
                'dependencyInventorySha256': common.sha(destination / 'dependencies.txt'),
                'definesSha256': common.sha(destination / 'defines.json'), 'toolchain': toolchain,
                'minIOS': pins['targets'][target]['minIOS'], 'environment': pins['targets'][target]['environment'],
                'artifactKind': 'static-sdk', 'kind': 'ios-static-sdk', 'platformBuildInputSha256': overlay_digest,
                'targetFiles': inventory(destination, target), 'binaryInspection': inspection,
                'validation': {'built': True, 'linkTested': True, 'runtimeTested': False, 'sourceCompatibilityTested': False},
                'linkSmoke': smoke,
            }
            manifest = {'schemaVersion': 1, 'v8': pins['v8'], 'depotTools': pins['depotTools'],
                        'bridge': bridge_contract, 'targets': dict(previous['targets']) if previous else {}, 'licenses': licenses}
            manifest['targets'][target] = entry
            temporary = manifest_path.with_name('manifest.json.publishing')
            temporary.write_text(json.dumps(manifest, indent=2) + '\n')
            os.replace(temporary, manifest_path)
            print(json.dumps({'manifest': str(manifest_path), 'target': target, 'binarySha256': entry['sha256']}))
    return destination
