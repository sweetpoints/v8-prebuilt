#!/usr/bin/env python3
"""Build the embedded shared bridge from fixed official V8 source and DEPS."""
import argparse
from contextlib import contextmanager
import hashlib
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
TARGETS = ('macos-arm64', 'macos-x64', 'android-arm64', 'android-x64')
BRIDGE_EXPORTS = {'sv8_create', 'sv8_start', 'sv8_poll', 'sv8_resolve',
                  'sv8_sync_poll', 'sv8_sync_reply', 'sv8_cancel', 'sv8_destroy',
                  'sv8_free', 'sv8_version'}


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


def bridge_files():
    return {name: ROOT / name for name in ('src/source_v8.cpp', 'src/source_v8.h', 'src/android_exports.map')} | {
        'tool/v8/source_v8.gni': HERE / 'source_v8.gni'}


def bridge_digest(files=None):
    digest = hashlib.sha256()
    for label, path in sorted((bridge_files() if files is None else files).items()):
        name = label.encode('utf-8')
        data = path.read_bytes()
        digest.update(struct.pack('>Q', len(name)))
        digest.update(name)
        digest.update(struct.pack('>Q', len(data)))
        digest.update(data)
    return digest.hexdigest()


def require_host(target):
    if target not in TARGETS:
        raise ValueError('Unsupported V8 target: ' + target)
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
    depot = cache / ('depot_tools' if target.startswith('macos-') else 'depot_tools-linux-x86_64')
    if not depot.exists():
        run(['git', 'clone', '--depth', '1', pins['depotTools']['repository'], depot], cache)
    origin = run(['git', 'remote', 'get-url', 'origin'], depot, capture=True).strip()
    if origin != pins['depotTools']['repository']:
        raise ValueError('Unexpected depot_tools origin')
    run(['git', 'fetch', '--depth', '1', 'origin', pins['depotTools']['revision']], depot)
    run(['git', 'checkout', '--detach', pins['depotTools']['revision']], depot)
    env = dict(os.environ, PATH=str(depot) + os.pathsep + os.environ['PATH'], DEPOT_TOOLS_UPDATE='0')
    initialize_depot(depot, env)
    # Android ABIs share the fixed Linux-host DEPS checkout but have independent
    # GN output directories, so x64 cannot replace ARM64 objects or binaries.
    workspace_target = 'android-arm64' if target.startswith('android-') else target
    workspace = cache / pins['v8']['revision'] / workspace_target
    workspace.mkdir(parents=True, exist_ok=True)
    gclient = 'solutions = ' + repr([{'name': 'v8', 'url': pins['v8']['repository'] + '@' + pins['v8']['revision'], 'deps_file': 'DEPS', 'managed': False, 'custom_deps': {}, 'custom_vars': {}}]) + '\n'
    if target.startswith('android-'):
        gclient += "target_os = ['android']\n"
    (workspace / '.gclient').write_text(gclient)
    run([depot / 'gclient', 'sync', '--no-history', '--shallow', '--revision', 'v8@' + pins['v8']['revision']], workspace, env)
    source = workspace / 'v8'
    actual = run(['git', 'rev-parse', 'HEAD'], source, capture=True).strip()
    if actual != pins['v8']['revision']:
        raise ValueError('V8 checkout revision mismatch')
    return source, depot, env


def gn_arguments(target, pins=None):
    if target not in TARGETS:
        raise ValueError('Unsupported V8 target: ' + target)
    cpu = (read_pins() if pins is None else pins)['targets'][target]['cpu']
    args = {'is_debug': False, 'is_component_build': False, 'v8_monolithic': True,
            'v8_monolithic_for_shared_library': True, 'v8_use_external_startup_data': False,
            'use_custom_libcxx': True, 'v8_enable_i18n_support': False,
            'v8_enable_temporal_support': False, 'use_remoteexec': False,
            'symbol_level': 0, 'target_cpu': cpu, 'v8_target_cpu': cpu}
    if target.startswith('android-'):
        args.update(target_os='android', android_ndk_api_level=26)
    else:
        args.update(target_os='mac', mac_deployment_target='13.0', use_lld=False)
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


def inspect_android_elf(path, target):
    """Validate the actual ELF ABI and every LOAD segment's 16 KiB alignment."""
    if target not in ('android-arm64', 'android-x64'):
        raise ValueError('ELF inspection requires Android target')
    data = path.read_bytes()
    if len(data) < 64 or data[:6] != b'\x7fELF\x02\x01':
        raise ValueError('Android binary must be little-endian ELF64')
    machine = struct.unpack_from('<H', data, 18)[0]
    expected_machine = read_pins()['targets'][target]['elfMachine']
    if machine != expected_machine:
        raise ValueError(f'Android ELF machine {machine} differs from {target}')
    if struct.unpack_from('<H', data, 16)[0] != 3:
        raise ValueError('Android bridge must be an ELF shared library')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    if size < 56 or count == 0 or offset + size * count > len(data):
        raise ValueError('Invalid Android ELF program headers')
    alignments = []
    for index in range(count):
        position = offset + index * size
        if struct.unpack_from('<I', data, position)[0] != 1:
            continue
        file_offset, address = struct.unpack_from('<QQ', data, position + 8)
        alignment = struct.unpack_from('<Q', data, position + 48)[0]
        if alignment < 16384 or alignment & (alignment - 1) or file_offset % 16384 != address % 16384:
            raise ValueError('Android ELF LOAD segment does not support 16 KiB pages')
        alignments.append(alignment)
    if not alignments:
        raise ValueError('Android ELF has no LOAD segments')
    return {'elfMachine': machine, 'loadSegmentAlignments': alignments}


def validate_android_binary(path, source, target, env):
    inspection = inspect_android_elf(path, target)
    tools = source / 'third_party/llvm-build/Release+Asserts/bin'
    symbols = run([tools / 'llvm-nm', '--dynamic', '--defined-only', '--format=posix', path], source, env, capture=True)
    exported = {line.split()[0].split('@')[0] for line in symbols.splitlines() if line.strip()}
    if exported != BRIDGE_EXPORTS:
        raise ValueError('Android bridge dynamic exports differ from the sv8 ABI')
    inspection['exports'] = sorted(exported)
    return inspection


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
        label = str(relative)
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


def build(source, depot, env, target, jobs, pins, output_root=None):
    if source_version(source) != pins['v8']['version']:
        raise ValueError('Official source version differs from pins')
    compiled_bridge_digest = bridge_digest()
    overlay = source / 'source_v8'
    overlay.mkdir(exist_ok=True)
    for label, path in bridge_files().items():
        shutil.copyfile(path, overlay / path.name)
    copied_files = {label: overlay / path.name for label, path in bridge_files().items()}
    if bridge_digest(copied_files) != compiled_bridge_digest:
        raise ValueError('Bridge source changed while copying build overlay')
    (overlay / 'BUILD.gn').write_text('import("//source_v8/source_v8.gni")\nsource_v8_library("source_v8") {}\n')
    out = source / ({'android-x64': 'out/source_v8_android_x64',
                     'macos-x64': 'out/source_v8_macos_x64'}.get(target, 'out/source_v8'))
    out.mkdir(parents=True, exist_ok=True)
    args = gn_arguments(target, pins)
    (out / 'args.gn').write_text(args)
    run([depot / 'gn', 'gen', out, '--root-target=//source_v8:source_v8', '--fail-on-unused-args'], source, env)
    run([depot / 'autoninja', '-C', out, '-j', jobs, 'source_v8:source_v8'], source, env)
    suffix = '.dylib' if target.startswith('macos-') else '.so'
    built = out / ('libsource_v8' + suffix)
    if not built.is_file():
        raise ValueError('Shared bridge output missing')
    inspection = validate_android_binary(built, source, target, env) if target.startswith('android-') else None
    artifact = Path(DEFAULT_OUTPUT_ROOT if output_root is None else output_root) / pins['v8']['revision']
    artifact.mkdir(parents=True, exist_ok=True)
    with publication_lock(artifact / '.publish.lock'):
        if bridge_digest() != compiled_bridge_digest or bridge_digest(copied_files) != compiled_bridge_digest:
            raise ValueError('Bridge source or compiled overlay changed during build; refusing publication')
        manifest_path = artifact / 'manifest.json'
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text())
            if (previous['v8'] != pins['v8'] or previous['depotTools'] != pins['depotTools']
                    or previous['bridge'] != {'abi': 1, 'sourceSha256': compiled_bridge_digest}):
                raise ValueError('Existing artifact manifest provenance differs; use clean artifact directory')
        licenses = package_licenses(source, artifact, previous.get('licenses', []) if previous else [])
        destination = artifact / target
        destination.mkdir(parents=True, exist_ok=True)
        binary = destination / built.name
        temporary = binary.with_name(binary.name + '.publishing')
        shutil.copyfile(built, temporary)
        os.replace(temporary, binary)
        header = destination / 'include/source_v8.h'
        header.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / 'source_v8.h', header)
        (destination / 'args.gn').write_text(args)
        revinfo = run([depot / 'gclient', 'revinfo', '--actual'], source.parent, env, capture=True)
        (destination / 'dependencies.txt').write_text(revinfo)
        defines = run([depot / 'gn', 'desc', out, '//source_v8:source_v8', 'defines', '--format=json',
                       '--root-target=//source_v8:source_v8'], source, env, capture=True)
        (destination / 'defines.json').write_text(defines)
        clang = source / 'third_party/llvm-build/Release+Asserts/bin/clang++'
        toolchain = {'clang': run([clang, '--version'], source, env, capture=True).strip(),
                     'gn': run([depot / 'gn', '--version'], source, env, capture=True).strip()}
        if target.startswith('macos-'):
            toolchain['appleLinker'] = run(['xcrun', 'ld', '-v'], source, env, capture=True).strip()
            toolchain['xcode'] = run(['xcodebuild', '-version'], source, env, capture=True).strip()
            toolchain['macSdk'] = run(['xcrun', '--sdk', 'macosx', '--show-sdk-version'], source, env, capture=True).strip()
        entry = {'artifactKind': 'shared-bridge', 'binary': str(Path(target) / binary.name), 'sha256': sha(binary), 'size': binary.stat().st_size,
                 'header': str(Path(target) / 'include/source_v8.h'), 'headerSha256': sha(header),
                 'host': {'os': platform.system(), 'cpu': platform.machine()},
                 'gnArgs': args, 'gnArgsSha256': sha(destination / 'args.gn'),
                 'depsSha256': sha(source / 'DEPS'), 'dependencyInventorySha256': sha(destination / 'dependencies.txt'),
                 'definesSha256': sha(destination / 'defines.json'), 'toolchain': toolchain,
                 'validation': {'built': True, 'runtimeTested': False, 'sourceCompatibilityTested': False}}
        if target.startswith('macos-'):
            entry['minMacOS'] = '13.0'
        else:
            entry['minApi'] = pins['targets'][target]['minApi']
            entry['abi'] = pins['targets'][target]['abi']
            entry['binaryInspection'] = inspection
        manifest = {'schemaVersion': 1, 'v8': pins['v8'], 'depotTools': pins['depotTools'],
                    'bridge': {'abi': 1, 'sourceSha256': compiled_bridge_digest}, 'targets': {}}
        if previous is not None:
            manifest['targets'] = previous['targets']
        manifest['targets'][target] = entry
        manifest['licenses'] = licenses
        temporary_manifest = manifest_path.with_name('manifest.json.publishing')
        temporary_manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        os.replace(temporary_manifest, manifest_path)
        print(json.dumps({'manifest': str(manifest_path), 'target': target, 'binarySha256': entry['sha256']}))


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
