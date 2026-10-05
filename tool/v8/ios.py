#!/usr/bin/env python3
"""Pinned iOS static SDK builder; device and simulator ARM64 are distinct platforms.

Called by build.py. This module never emits a loadable dylib. Consumers link the
official V8 monolith and Apple's libc++; embedding bridges belong to consumers.
"""
import hashlib
import json
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
LINK_SMOKE = '''#include "v8.h"
#include "libplatform/libplatform.h"
int main() {
  v8::V8::SetFlagsFromString("--jitless");
  auto platform = v8::platform::NewDefaultPlatform();
  v8::V8::InitializePlatform(platform.get());
  if (!v8::V8::Initialize()) return 1;
  auto allocator = v8::ArrayBuffer::Allocator::NewDefaultAllocator();
  v8::Isolate::CreateParams params;
  params.array_buffer_allocator = allocator;
  auto isolate = v8::Isolate::New(params);
  int result = 1;
  {
    v8::Isolate::Scope isolate_scope(isolate);
    v8::HandleScope handle_scope(isolate);
    auto context = v8::Context::New(isolate);
    v8::Context::Scope context_scope(context);
    auto code = v8::String::NewFromUtf8Literal(isolate, "1 + 2");
    auto script = v8::Script::Compile(context, code).ToLocalChecked();
    auto value = script->Run(context).ToLocalChecked();
    result = value->Int32Value(context).FromMaybe(0) == 3 ? 0 : 1;
  }
  isolate->Dispose();
  delete allocator;
  v8::V8::Dispose();
  v8::V8::DisposePlatform();
  return result;
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
        'ios_deployment_target': config['minIOS'], 'ios_enable_code_signing': False,
        # Pinned ARM64 jitless builtins declare is_code outside its !V8_JITLESS
        # uses. Keep upstream bytes and warning output; do not promote warnings.
        'treat_warnings_as_errors': False,
        'is_debug': False, 'is_component_build': False, 'v8_monolithic': True,
        'v8_monolithic_for_shared_library': False, 'v8_use_external_startup_data': False,
        'use_custom_libcxx': False, 'v8_enable_i18n_support': False,
        'v8_enable_temporal_support': False, 'v8_enable_pointer_compression': False,
        'v8_enable_sandbox': False, 'v8_jitless': True,
        'v8_enable_turbofan': False, 'v8_enable_maglev': False,
        'v8_enable_sparkplug': False, 'v8_enable_webassembly': False,
        'use_remoteexec': False, 'use_lld': False, 'use_thin_lto': False, 'symbol_level': 0,
    }
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


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
    return inspect_static_archive(path, target)


def output_for(source, out, depot, env, label):
    outputs = common.gn_property(source, out, depot, env, label, 'outputs', root_target='//:v8_monolith')
    archives = [source / value.removeprefix('//') for value in outputs if value.endswith('.a')]
    if len(archives) != 1 or not archives[0].is_file() or not archives[0].resolve().is_relative_to(out.resolve()):
        raise ValueError('Expected exactly one built archive inside target output: ' + label)
    return archives[0]


def link_contract(target, pins, defines, frameworks=(), system_libraries=()):
    config = configuration(target, pins)
    return {
        'schemaVersion': 1, 'kind': 'ios-static-sdk', 'target': target, 'architecture': 'arm64',
        'environment': config['environment'], 'sdk': SDKS[target], 'minIOS': config['minIOS'],
        'clangTarget': 'arm64-apple-ios' + config['minIOS'] + ('-simulator' if target.endswith('simulator-arm64') else ''),
        'nativeV8Archive': 'lib/libv8_monolith.a',
        'includeDirs': ['include'], 'defines': defines, 'compileOptions': ['-std=c++20'],
        'libraries': ['lib/libv8_monolith.a'],
        'linkOptions': [arg for name in frameworks for arg in ('-framework', name.removesuffix('.framework'))],
        'systemLibraries': list(dict.fromkeys(['c++', *system_libraries])),
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
    command = ['xcrun', '--sdk', SDKS[target], 'clang++', *contract['compileOptions'],
               '-target', contract['clangTarget'], '-isysroot', sdk]
    command += ['-I' + str(directory / name) for name in contract['includeDirs']]
    command += ['-D' + define for define in contract['defines']]
    command += [probe, *[directory / name for name in contract['libraries']]]
    command += contract['linkOptions'] + ['-l' + name for name in contract['systemLibraries']]
    command += ['-Wl,-dead_strip', '-o', output]
    common.run(command, source, env)
    inspection = inspect_macho(output.read_bytes(), target, filetype=2)
    result = {'passed': True, 'executableInspection': inspection, 'runtimeExecuted': False,
              'source': 'validation/link-smoke.cpp', 'executable': 'validation/link-smoke',
              'command': [str(argument) for argument in command],
              'linkingSha256': common.sha(directory / 'linking.json'),
              'monolithSha256': common.sha(directory / 'lib/libv8_monolith.a')}
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
    args = gn_arguments(target, pins)
    builder_source = Path(__file__).read_text()
    build_inputs_digest = hashlib.sha256(json.dumps({'args.gn': args, 'ios.py': builder_source}, sort_keys=True).encode()).hexdigest()
    out = output_directory(source, target)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'args.gn').write_text(args)
    common.run([depot / 'gn', 'gen', out, '--root-target=//:v8_monolith', '--fail-on-unused-args'], source, env)
    common.run([depot / 'autoninja', '-C', out, '-j', jobs, 'v8_monolith'], source, env)
    monolith = output_for(source, out, depot, env, '//:v8_monolith')
    defines = common.sdk_defines(source, out, depot, env, root_target='//:v8_monolith')
    frameworks = common.gn_property(source, out, depot, env, '//:v8_monolith', 'frameworks', root_target='//:v8_monolith')
    system_libraries = common.gn_property(source, out, depot, env, '//:v8_monolith', 'libs', root_target='//:v8_monolith')
    with tempfile.TemporaryDirectory(prefix='v8-ios-sdk-') as staging:
        directory = Path(staging)
        files = common.sdk_headers(source, out)
        for relative, original in files.items():
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
        (directory / 'lib').mkdir()
        shutil.copyfile(monolith, directory / 'lib/libv8_monolith.a')
        inspection = validate_binary(directory / 'lib/libv8_monolith.a', source, target, env)
        for minimum in inspection['objectMinIOS']:
            expected = tuple(map(int, pins['targets'][target]['minIOS'].split('.')))
            expected = expected + (0,) * (3 - len(expected))
            if tuple(map(int, minimum.split('.'))) > expected:
                raise ValueError('An archive member requires newer iOS than the SDK deployment contract')
        (directory / 'linking.json').write_text(json.dumps(link_contract(target, pins, defines, frameworks, system_libraries), indent=2) + '\n')
        smoke = link_smoke(directory, source, target, pins, env)
        (directory / 'args.gn').write_text(args)
        (directory / 'defines.json').write_text(json.dumps(defines, indent=2) + '\n')
        (directory / 'dependencies.txt').write_text(common.run([depot / 'gclient', 'revinfo', '--actual'], source.parent, env, capture=True))
        (directory / 'build-inputs').mkdir()
        (directory / 'build-inputs/ios.py').write_text(builder_source)
        clang = source / 'third_party/llvm-build/Release+Asserts/bin/clang++'
        toolchain = {
            'clang': common.run([clang, '--version'], source, env, capture=True).strip(),
            'gn': common.run([depot / 'gn', '--version'], source, env, capture=True).strip(),
            'xcode': common.run(['xcodebuild', '-version'], source, env, capture=True).strip(),
            'iosSdk': common.run(['xcrun', '--sdk', SDKS[target], '--show-sdk-version'], source, env, capture=True).strip(),
        }
        if (out / 'args.gn').read_text() != args or Path(__file__).read_text() != builder_source:
            raise ValueError('iOS SDK build inputs changed during build')
        entry = {
            'binary': 'lib/libv8_monolith.a',
            'host': {'os': platform.system(), 'cpu': platform.machine()}, 'gnArgs': args,
            'gnArgsSha256': common.sha(directory / 'args.gn'), 'depsSha256': common.sha(source / 'DEPS'),
            'dependencyInventorySha256': common.sha(directory / 'dependencies.txt'),
            'definesSha256': common.sha(directory / 'defines.json'), 'toolchain': toolchain,
            'minIOS': pins['targets'][target]['minIOS'], 'environment': pins['targets'][target]['environment'],
            'platformBuildInputSha256': build_inputs_digest, 'binaryInspection': inspection,
            'validation': {'built': True, 'linkTested': True, 'runtimeTested': False, 'sourceCompatibilityTested': False},
            'linkSmoke': smoke,
        }
        return common.publish_sdk(source, target, pins,
            {path.relative_to(directory).as_posix(): path for path in directory.rglob('*') if path.is_file()},
            entry, output_root)
