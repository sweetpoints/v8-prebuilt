#!/usr/bin/env python3
"""Build a pure V8 static SDK from exact official V8 and depot_tools pins."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_CACHE_ROOT = ROOT / '.cache/v8-source'
DEFAULT_OUTPUT_ROOT = ROOT / 'artifacts'
TARGETS = ('macos-arm64', 'macos-x64', 'android-arm64', 'android-x64',
           'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64',
           'ios-arm64', 'ios-simulator-arm64')


def read_pins(path=None):
    pins = json.loads((HERE / 'pins.json' if path is None else Path(path)).read_text())
    if not isinstance(pins, dict) or pins.get('schemaVersion') != 1:
        raise ValueError('Unsupported pins schema version')
    if not isinstance(pins.get('v8'), dict) or not isinstance(pins.get('depotTools'), dict):
        raise ValueError('V8 and depot_tools pins must be objects')
    if not isinstance(pins['v8'].get('version'), str):
        raise ValueError('V8 version must be a string')
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:[1-9][0-9]*))?', pins['v8']['version']):
        raise ValueError('V8 version must be canonical major.minor.build[.nonzero-patch]')
    for key in ('v8', 'depotTools'):
        if not isinstance(pins[key].get('revision'), str) or not re.fullmatch(r'[a-f0-9]{40}', pins[key]['revision']):
            raise ValueError('Pins require complete lowercase commit SHA')
        repositories = {'v8': 'https://chromium.googlesource.com/v8/v8.git',
                        'depotTools': 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'}
        if pins[key].get('repository') != repositories[key]:
            raise ValueError('Only official Chromium source repositories permitted')
    if not isinstance(pins.get('targets'), dict) or set(pins['targets']) != set(TARGETS):
        raise ValueError('Pinned target contract does not match supported platforms')
    for target, config in pins['targets'].items():
        expected_cpu = 'arm64' if target.endswith('arm64') else 'x64'
        if not isinstance(config, dict) or config.get('cpu') != expected_cpu:
            raise ValueError('Pinned target CPU differs from target name: ' + target)
    return pins


@contextmanager
def publication_lock(path):
    # Windows cannot import fcntl. Both implementations lock the same file.
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run(args, cwd, env=None, capture=False):
    print('+ ' + ' '.join(str(arg) for arg in args), flush=True)
    return subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                          check=True, text=True, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=None)
def platform_module(name):
    spec = importlib.util.spec_from_file_location('source_v8_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def depot_command(depot, name):
    return depot / (name + ('.bat' if os.name == 'nt' else ''))


def require_host(target):
    if target not in TARGETS:
        raise ValueError('Unsupported V8 target: ' + target)
    if target.startswith(('linux-', 'windows-')):
        return platform_module('desktop').require_host(target)
    if target.startswith('ios-'):
        return platform_module('ios').require_host(target)
    host = (platform.system(), platform.machine())
    expected = (('Darwin', 'arm64' if target == 'macos-arm64' else 'x86_64')
                if target.startswith('macos-') else ('Linux', 'x86_64'))
    if host != expected:
        raise ValueError(f'{target} requires host {expected}; found {host}. Android must use Linux x86_64 runner.')


def initialize_depot(depot, env):
    # Follow the two official ensure_bootstrap prerequisites, avoiding unrelated
    # gsutil/pylint environment initialization and preserving this checkout pin.
    command = ('set -e; export DEPOT_TOOLS_DIR="$1"; '
               'source "$1/bootstrap_python3"; bootstrap_python3; '
               'source "$1/cipd_bin_setup.sh"; cipd_bin_setup >/dev/null')
    run(['bash', '-c', command, '--', depot], depot, env)


def bootstrap(cache, target, pins):
    require_host(target)
    cache.mkdir(parents=True, exist_ok=True)
    depot = cache / ('depot_tools' if target.startswith(('macos-', 'ios-'))
                     else 'depot_tools-windows' if target.startswith('windows-')
                     else 'depot_tools-linux-x86_64')
    if not depot.exists():
        run(['git', 'clone', '--depth', '1', pins['depotTools']['repository'], depot], cache)
    origin = run(['git', 'remote', 'get-url', 'origin'], depot, capture=True).strip()
    if origin != pins['depotTools']['repository']:
        raise ValueError('Unexpected depot_tools origin')
    run(['git', 'fetch', '--depth', '1', 'origin', pins['depotTools']['revision']], depot)
    run(['git', 'checkout', '--detach', pins['depotTools']['revision']], depot)
    env = dict(os.environ, PATH=str(depot) + os.pathsep + os.environ['PATH'], DEPOT_TOOLS_UPDATE='0')
    if target.startswith(('linux-', 'windows-')):
        env = platform_module('desktop').environment(target, env)
    if target.startswith('windows-'):
        platform_module('desktop').initialize_depot_windows(depot, env, run)
    else:
        initialize_depot(depot, env)
    # Android ABIs share the fixed Linux-host DEPS checkout but have independent
    # GN output directories, so x64 cannot replace ARM64 objects or binaries.
    workspace_target = ('android-arm64' if target.startswith('android-') else
                        'ios-arm64' if target.startswith('ios-') else
                        'linux-x64' if target.startswith('linux-') else
                        'windows-x64' if target.startswith('windows-') else target)
    workspace = cache / pins['v8']['revision'] / workspace_target
    workspace.mkdir(parents=True, exist_ok=True)
    gclient = 'solutions = ' + repr([{'name': 'v8', 'url': pins['v8']['repository'] + '@' + pins['v8']['revision'], 'deps_file': 'DEPS', 'managed': False, 'custom_deps': {}, 'custom_vars': {}}]) + '\n'
    if target.startswith('android-'):
        gclient += "target_os = ['android']\n"
    elif target.startswith('ios-'):
        gclient += "target_os = ['ios']\n"
    (workspace / '.gclient').write_text(gclient)
    run([depot_command(depot, 'gclient'), 'sync', '--no-history', '--shallow', '--revision', 'v8@' + pins['v8']['revision']], workspace, env)
    source = workspace / 'v8'
    actual = run(['git', 'rev-parse', 'HEAD'], source, capture=True).strip()
    if actual != pins['v8']['revision']:
        raise ValueError('V8 checkout revision mismatch')
    return source, depot, env


def gn_arguments(target, pins=None):
    if target not in TARGETS:
        raise ValueError('Unsupported V8 target: ' + target)
    pins = read_pins() if pins is None else pins
    if target.startswith(('linux-', 'windows-')):
        return platform_module('desktop').gn_arguments(target, pins)
    if target.startswith('ios-'):
        return platform_module('ios').gn_arguments(target, pins)
    cpu = pins['targets'][target]['cpu']
    args = {'is_debug': False, 'is_component_build': False, 'v8_monolithic': True,
            'v8_monolithic_for_shared_library': True, 'v8_use_external_startup_data': False,
            'use_custom_libcxx': True, 'v8_enable_i18n_support': False,
            'v8_enable_temporal_support': False, 'use_remoteexec': False,
            'symbol_level': 0, 'use_thin_lto': False, 'target_cpu': cpu, 'v8_target_cpu': cpu}
    if target.startswith('android-'):
        args.update(target_os='android', android_ndk_api_level=26)
    else:
        args.update(target_os='mac', mac_deployment_target='13.0', use_lld=False)
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


def source_version(source):
    header = (source / 'include/v8-version.h').read_text()
    names = ['V8_MAJOR_VERSION', 'V8_MINOR_VERSION', 'V8_BUILD_NUMBER', 'V8_PATCH_LEVEL']
    parts = [re.search(r'#define\s+' + name + r'\s+(\d+)', header).group(1) for name in names]
    return '.'.join(parts[:-1] if parts[-1] == '0' else parts)


def package_licenses(source, destination, existing=()):
    entries = {entry['path']: entry for entry in existing}
    copies = []
    roots = [source / name for name in ('LICENSE', 'AUTHORS')]
    for pattern in ('LICENSE*', 'COPYING*', 'NOTICE*', 'AUTHORS*'):
        roots.extend((source / 'third_party').rglob(pattern))
    for path in sorted(set(roots)):
        if not path.is_file() or '.git' in path.parts:
            continue
        relative = Path('licenses') / path.relative_to(source)
        label = relative.as_posix()
        digest = sha(path)
        if label in entries and entries[label]['sha256'] != digest:
            raise ValueError(f'License provenance conflict at {label}')
        entries[label] = {'path': label, 'sha256': digest}
        copies.append((path, destination / relative))
    # Preflight all conflicts before replacing any indexed license file.
    for path, output in copies:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
    return [entries[label] for label in sorted(entries)]


def gn_property(source, out, depot, env, label, prop, root_target='//sdk_runtime:sdk'):
    result = json.loads(run([depot_command(depot, 'gn'), 'desc', out, label, prop,
                             '--format=json', '--root-target=' + root_target], source, env, capture=True))
    if label not in result or prop not in result[label]:
        raise ValueError('GN did not report ' + label + ' ' + prop)
    return result[label][prop]


def sdk_defines(source, out, depot, env, root_target='//sdk_runtime:sdk'):
    public = gn_property(source, out, depot, env, '//:headers_config', 'defines', root_target)
    compiled = gn_property(source, out, depot, env, '//:v8_monolith', 'defines', root_target)
    runtime = [value for value in compiled if value == 'NDEBUG' or
               value.startswith(('_LIBCPP_', '_LIBCXXABI_'))]
    return list(dict.fromkeys(public + runtime))


def gn_output(source, out, depot, env, label):
    outputs = gn_property(source, out, depot, env, label, 'outputs')
    if len(outputs) != 1 or not outputs[0].startswith('//'):
        raise ValueError('Expected one official archive output for ' + label)
    return source / outputs[0][2:]


def sdk_headers(source, out):
    files = {}
    for base in (source / 'include', out / 'gen/include'):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*')):
            if path.is_file():
                name = (Path('include') / path.relative_to(base)).as_posix()
                if name in files and sha(files[name]) != sha(path):
                    raise ValueError('Generated/public V8 header conflict: ' + name)
                files[name] = path
    if 'include/v8.h' not in files:
        raise ValueError('Official public V8 headers are missing')
    return files


def publish_sdk(source, target, pins, files, entry, output_root=None):
    artifact = Path(DEFAULT_OUTPUT_ROOT if output_root is None else output_root) / pins['v8']['revision']
    artifact.mkdir(parents=True, exist_ok=True)
    if entry.get('binary') not in files or 'include/v8.h' not in files or 'linking.json' not in files:
        raise ValueError('SDK must include its monolith, public V8 header and linking contract')
    for name, path in files.items():
        if (not isinstance(name, str) or '\\' in name or ':' in name or name.startswith('/') or
                any(part in ('', '.', '..') for part in name.split('/')) or not path.is_file()):
            raise ValueError('Invalid SDK file: ' + str(name))
    with publication_lock(artifact / '.publish.lock'):
        manifest_path = artifact / 'manifest.json'
        previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        if previous is not None and (previous['v8'] != pins['v8'] or previous['depotTools'] != pins['depotTools']):
            raise ValueError('Existing SDK manifest source provenance differs')
        licenses = package_licenses(source, artifact, previous.get('licenses', []) if previous else [])
        destination = artifact / target
        destination.mkdir(parents=True, exist_ok=True)
        index = []
        for name, path in sorted(files.items()):
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            copied_hash = sha(path)
            shutil.copyfile(path, output)
            if sha(output) != copied_hash or sha(path) != copied_hash:
                raise ValueError('SDK file changed while publishing: ' + name)
            index.append({'path': (Path(target) / name).as_posix(), 'sha256': copied_hash,
                          'size': output.stat().st_size})
        result = dict(entry)
        result['artifactKind'] = 'v8-static-sdk'
        result['binary'] = (Path(target) / entry['binary']).as_posix()
        result['sha256'] = sha(artifact / result['binary'])
        result['size'] = (artifact / result['binary']).stat().st_size
        result['targetFiles'] = index
        for name, key in [('args.gn', 'gnArgsSha256'), ('defines.json', 'definesSha256'),
                          ('dependencies.txt', 'dependencyInventorySha256'), ('linking.json', 'linkingSha256')]:
            if name in files:
                result[key] = sha(destination / name)
        manifest = {'schemaVersion': 1, 'v8': pins['v8'], 'depotTools': pins['depotTools'],
                    'targets': dict(previous['targets']) if previous else {}, 'licenses': licenses}
        manifest['targets'][target] = result
        temporary = manifest_path.with_name('manifest.json.publishing')
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        os.replace(temporary, manifest_path)
    print(json.dumps({'manifest': str(manifest_path), 'target': target, 'binarySha256': result['sha256']}))
    return destination



def inspect_apple_android_archive(path, target):
    expected = (0x0100000C if target == 'macos-arm64' else 0x01000007) if target.startswith('macos-') else (183 if target == 'android-arm64' else 62)
    count = 0
    total = path.stat().st_size
    with path.open('rb') as archive:
        if archive.read(8) != b'!<arch>\n':
            raise ValueError('SDK requires a complete native static archive')
        while archive.tell() < total:
            header = archive.read(60)
            if len(header) != 60 or header[58:] != b'`\n':
                raise ValueError('Invalid SDK static archive member')
            name = header[:16].decode('ascii').strip()
            size = int(header[48:58].decode('ascii').strip())
            end = archive.tell() + size
            if size < 0 or end > total:
                raise ValueError('Truncated SDK static archive')
            if name.startswith('#1/'):
                name_size = int(name[3:])
                if name_size < 0 or name_size > size:
                    raise ValueError('Invalid BSD archive name')
                name = archive.read(name_size).rstrip(b'\0').decode('utf-8')
            if name not in ('/', '//', '/SYM64/') and not name.startswith('__.SYMDEF'):
                data = archive.read(min(32, end - archive.tell()))
                if target.startswith('macos-'):
                    if len(data) < 16 or data[:4] != b'\xcf\xfa\xed\xfe':
                        raise ValueError('SDK archive member must be native Mach-O 64')
                    machine, kind = struct.unpack_from('<I', data, 4)[0], struct.unpack_from('<I', data, 12)[0]
                else:
                    if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01':
                        raise ValueError('SDK archive member must be native ELF64')
                    kind, machine = struct.unpack_from('<HH', data, 16)
                if kind != 1 or machine != expected:
                    raise ValueError('SDK archive relocatable object architecture mismatch')
                count += 1
            archive.seek(end + size % 2)
        final_position = archive.tell()
    if count == 0 or final_position != total:
        raise ValueError('SDK archive has no native objects or invalid padding')
    return {'format': 'static-archive', 'objectMachine': expected, 'objectCount': count}


def apple_android_profile(source, out, target, pins, defines, depot, env):
    monolith = gn_output(source, out, depot, env, '//:v8_monolith')
    runtime = gn_output(source, out, depot, env, '//sdk_runtime:v8_cxx_runtime')
    libraries = {'lib/' + monolith.name: monolith, 'lib/' + runtime.name: runtime}
    for library in libraries.values():
        inspect_apple_android_archive(library, target)
    headers = {}
    for original, prefix in [(source / 'third_party/libc++/src/include', 'runtime/include/c++'),
                             (source / 'third_party/libc++abi/src/include', 'runtime/include/abi')]:
        for path in original.rglob('*'):
            if path.is_file():
                headers[(Path(prefix) / path.relative_to(original)).as_posix()] = path
    site = out / 'gen/buildtools/third_party/libc++/__config_site'
    if not site.is_file():
        site = source / 'buildtools/third_party/libc++/__config_site'
    if not site.is_file():
        raise ValueError('Pinned libc++ configuration header missing')
    headers['runtime/include/config/__config_site'] = site
    options = ['-std=c++20', '-fno-rtti', '-fno-exceptions', '-nostdinc++', '-fPIC']
    link = ['-nostdlib++']
    if target.startswith('macos-'):
        triple = ('arm64' if target == 'macos-arm64' else 'x86_64') + '-apple-macos13.0'
        options += ['--target=' + triple, '-mmacosx-version-min=13.0']
        link += ['--target=' + triple, '-mmacosx-version-min=13.0']
        systems = ['pthread']
    else:
        triple = ('aarch64' if target == 'android-arm64' else 'x86_64') + '-linux-android26'
        options += ['--target=' + triple]
        link += ['--target=' + triple, '-Wl,-z,max-page-size=16384']
        systems = ['dl', 'log', 'm', 'pthread']
    return {'libraries': libraries, 'runtimeHeaders': headers, 'linking': {
        'schemaVersion': 1, 'includeDirs': ['include', 'runtime/include/config', 'runtime/include/c++', 'runtime/include/abi'],
        'defines': defines, 'compileOptions': options, 'libraries': list(libraries),
        'linkOptions': link, 'systemLibraries': systems,
        'compilerStyle': 'clang++', 'cxxRuntime': 'pinned Chromium libc++ (__Cr ABI)',
        'sysrootRequirement': ({'kind': 'apple-macos-sdk', 'minOS': '13.0'} if target.startswith('macos-') else
                               {'kind': 'android-ndk-sysroot', 'minApi': 26, 'target': triple})}}


def build(source, depot, env, target, jobs, pins, output_root=None):
    if target.startswith('ios-'):
        return platform_module('ios').build(source, depot, env, target, jobs, pins, output_root)
    desktop = platform_module('desktop')
    if target.startswith(('linux-', 'windows-')):
        env = desktop.environment(target, env)
        desktop.prepare(source, depot, env, target, run)
    if source_version(source) != pins['v8']['version']:
        raise ValueError('Official source version differs from pins')
    build_inputs = desktop.create_runtime_target(source)
    out = source / ('out/v8_sdk_' + target.replace('-', '_'))
    out.mkdir(parents=True, exist_ok=True)
    args = gn_arguments(target, pins)
    (out / 'args.gn').write_text(args)
    run([depot_command(depot, 'gn'), 'gen', out, '--root-target=//sdk_runtime:sdk', '--fail-on-unused-args'], source, env)
    run([depot_command(depot, 'autoninja'), '-C', out, '-j', jobs, 'v8_monolith', 'sdk_runtime:v8_cxx_runtime'], source, env)
    defines = sdk_defines(source, out, depot, env)
    profile = (desktop.sdk_profile(source, out, target, pins, defines, run, depot_command(depot, 'gn'), env)
               if target.startswith(('linux-', 'windows-')) else
               apple_android_profile(source, out, target, pins, defines, depot, env))
    files = sdk_headers(source, out) | profile['libraries'] | profile['runtimeHeaders']
    files |= {'build-inputs/' + path.name: path for path in build_inputs.values()}
    metadata = out / 'sdk-metadata'
    metadata.mkdir(exist_ok=True)
    (metadata / 'linking.json').write_text(json.dumps(profile['linking'], indent=2) + '\n')
    (metadata / 'defines.json').write_text(json.dumps(defines, indent=2) + '\n')
    inventory = run([depot_command(depot, 'gclient'), 'revinfo', '--actual'], source.parent, env, capture=True)
    (metadata / 'dependencies.txt').write_text(inventory)
    files |= {'linking.json': metadata / 'linking.json', 'defines.json': metadata / 'defines.json',
              'dependencies.txt': metadata / 'dependencies.txt', 'args.gn': out / 'args.gn'}
    compiler = source / ('third_party/llvm-build/Release+Asserts/bin/clang-cl.exe' if target.startswith('windows-')
                         else 'third_party/llvm-build/Release+Asserts/bin/clang++')
    entry = {'binary': next(name for name in profile['libraries'] if 'v8_monolith' in name),
             'gnArgs': args, 'depsSha256': sha(source / 'DEPS'), 'targetConfig': pins['targets'][target],
             'host': {'os': platform.system(), 'cpu': platform.machine()},
             'toolchain': {'clang': run([compiler, '--version'], source, env, capture=True).strip(),
                           'gn': run([depot_command(depot, 'gn'), '--version'], source, env, capture=True).strip()},
             'validation': {'built': True, 'linkTested': False, 'runtimeTested': False}}
    return publish_sdk(source, target, pins, files, entry, output_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bootstrap', 'build'])
    parser.add_argument('--pins-file', type=Path, default=HERE / 'pins.json',
                        help='Exact V8/depot_tools source pins (default: %(default)s)')
    parser.add_argument('--target', required=True, choices=TARGETS)
    parser.add_argument('--cache-root', type=Path, default=DEFAULT_CACHE_ROOT,
                        help='Pinned source and depot_tools cache (default: %(default)s)')
    parser.add_argument('--output-root', type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help='Artifact root; a V8 revision directory is added (default: %(default)s)')
    parser.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    pins = read_pins(args.pins_file)
    cache = args.cache_root.resolve()
    source, depot, env = bootstrap(cache, args.target, pins)
    if args.action == 'bootstrap':
        print(json.dumps({'source': str(source), 'revision': pins['v8']['revision']}))
        return
    build(source, depot, env, args.target, args.jobs, pins, output_root=args.output_root.resolve())


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError, OSError) as error:
        print(f'Official V8 build failed: {error}', file=sys.stderr)
        sys.exit(1)
