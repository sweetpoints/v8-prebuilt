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
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_CACHE_ROOT = ROOT / '.cache/v8-source'
DEFAULT_OUTPUT_ROOT = ROOT / 'artifacts'
TARGETS = ('macos-arm64', 'macos-x64', 'android-arm64', 'android-x64',
           'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64',
           'ios-arm64', 'ios-simulator-arm64')



# V8 15.5.35.21's GN flag reader mistakes -isysroot's absolute SDK
# operand for a clang-cl switch. Limit the workaround to this reviewed file;
# restore the upstream checkout even when Ninja fails so bootstrap provenance
# and its tracked-diff checks remain meaningful.
IOS_METAGEN_FLAGS_SHA256 = '883f9abe171cace4fab78870badf82018956809903c17456b018c172b23131e5'
IOS_METAGEN_DRIVER_LINE = '  cl_mode = any(f.startswith("/") for f in cflags)'
IOS_METAGEN_DRIVER_FIX = '  # Path operands of POSIX compiler options are not clang-cl switches.\n  path_options = {"-isysroot", "--sysroot", "-I", "-isystem", "-iquote",\n                  "-include", "-imacros", "-resource-dir", "-iframework"}\n  cl_mode = any(f.startswith("/") for i, f in enumerate(cflags)\n                if i == 0 or cflags[i - 1] not in path_options)'


@contextmanager
def ios_metagen_flags(source, out):
    """Yield indexed workaround evidence while using the reviewed TU flags."""
    path = source / 'tools/metagen/compile_flags.py'
    if not path.exists():
        yield {}
        return
    original = path.read_bytes()
    text = original.decode('utf-8')
    if IOS_METAGEN_DRIVER_LINE not in text:
        # A newer upstream flag reader does not contain this known defect.
        yield {}
        return
    digest = hashlib.sha256(original).hexdigest()
    if digest != IOS_METAGEN_FLAGS_SHA256 or text.count(IOS_METAGEN_DRIVER_LINE) != 1:
        raise ValueError('Unreviewed upstream metagen driver detection; refusing to patch')
    patched = text.replace(IOS_METAGEN_DRIVER_LINE, IOS_METAGEN_DRIVER_FIX).encode('utf-8')
    directory = out / 'sdk-metadata/metagen-driver-fix'
    directory.mkdir(parents=True, exist_ok=True)
    before = directory / 'compile_flags.original.py'
    after = directory / 'compile_flags.patched.py'
    report = directory / 'metagen-driver-fix.json'
    before.write_bytes(original)
    after.write_bytes(patched)
    report.write_text(json.dumps({
        'schemaVersion': 1, 'reason': 'posix-sdk-path-is-not-clang-cl-option',
        'sourcePath': 'tools/metagen/compile_flags.py',
        'originalSha256': digest, 'patchedSha256': hashlib.sha256(patched).hexdigest(),
        'original': 'build-inputs/compile_flags.original.py',
        'patched': 'build-inputs/compile_flags.patched.py',
    }, indent=2) + '\n')
    path.write_bytes(patched)
    try:
        yield {'build-inputs/compile_flags.original.py': before,
               'build-inputs/compile_flags.patched.py': after,
               'validation/metagen-driver-fix.json': report}
    finally:
        changed = not path.is_file() or path.read_bytes() != patched
        path.write_bytes(original)
        if changed:
            raise ValueError('Upstream metagen workaround changed during build')


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


def initialize_windows_depot(depot, env):
    platform_module('desktop').initialize_depot_windows(depot, env, run)
    # gclient downloads multiple DEPS concurrently. Initialize the fixed
    # gsutil package once before those subprocesses compete for its 30-second
    # bootstrap lock; keep the upstream wrapper, checksums and locking intact.
    run([depot_command(depot, 'gsutil.py'), 'version'], depot, env)


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



def bootstrap_snapshot(source, depot, workspace, pins, env):
    for path, pin in ((source, pins['v8']), (depot, pins['depotTools'])):
        origin = run(['git', 'remote', 'get-url', 'origin'], path, env, capture=True).strip()
        revision = run(['git', 'rev-parse', 'HEAD'], path, env, capture=True).strip()
        if origin != pin['repository'] or revision != pin['revision']:
            raise ValueError('Bootstrapped source/depot_tools origin or revision differs from pins')
    inventory = run([depot_command(depot, 'gclient'), 'revinfo', '--actual'], workspace, env, capture=True)
    repositories = {'depotTools': depot}
    for line in inventory.splitlines():
        match = re.fullmatch(r'(v8[^:]*): (.+)@([a-f0-9]{40})', line)
        if match is None:
            # CIPD package labels use a different inventory syntax. Their full
            # fixed inventory remains covered by dependencyInventorySha256.
            continue
        relative, _, revision = match.groups()
        if not re.fullmatch(r'v8(?:/[A-Za-z0-9_+.\-]+)*', relative) or any(part in ('.', '..') for part in relative.split('/')):
            raise ValueError('Invalid dependency inventory path')
        path = workspace / relative
        if not (path / '.git').exists():
            raise ValueError('Missing Git dependency in bootstrap inventory: ' + relative)
        actual = run(['git', 'rev-parse', 'HEAD'], path, env, capture=True).strip()
        if actual != revision:
            raise ValueError('Git dependency revision differs from actual inventory: ' + relative)
        repositories[relative] = path
    if 'v8' not in repositories:
        raise ValueError('Bootstrap inventory must include the pinned V8 Git checkout')
    tracked = {}
    for name, path in sorted(repositories.items()):
        # Capture the post-hook baseline, including intentional upstream hook
        # changes. Never print its contents or reset the checkout.
        diff = run(['git', 'diff', '--binary', 'HEAD', '--'], path, env, capture=True)
        tracked[name] = hashlib.sha256(diff.encode('utf-8')).hexdigest()
    return {'schemaVersion': 1, 'pins': pins, 'depsSha256': sha(source / 'DEPS'),
            'gclientSha256': sha(workspace / '.gclient'),
            'dependencyInventorySha256': hashlib.sha256(inventory.encode('utf-8')).hexdigest(),
            'trackedDiffSha256': tracked}


def bootstrap(cache, target, pins, reuse=False):
    require_host(target)
    cache.mkdir(parents=True, exist_ok=True)
    depot = cache / ('depot_tools' if target.startswith(('macos-', 'ios-'))
                     else 'depot_tools-windows' if target.startswith('windows-')
                     else 'depot_tools-linux-x86_64')
    workspace_target = ('android-arm64' if target.startswith('android-') else
                        'ios-arm64' if target.startswith('ios-') else
                        'linux-x64' if target.startswith('linux-') else
                        'windows-x64' if target.startswith('windows-') else target)
    workspace = cache / pins['v8']['revision'] / workspace_target
    source = workspace / 'v8'
    marker = workspace / 'bootstrap-state.json'
    env = dict(os.environ, PATH=str(depot) + os.pathsep + os.environ['PATH'], DEPOT_TOOLS_UPDATE='0')
    if target.startswith(('linux-', 'windows-')):
        env = platform_module('desktop').environment(target, env)
    if reuse and marker.is_file():
        recorded = json.loads(marker.read_text())
        if not isinstance(recorded, dict) or type(recorded.get('schemaVersion')) is not int or recorded.get('schemaVersion') != 1 or recorded.get('pins') != pins:
            raise ValueError('Bootstrap marker pins differ; use an explicitly bootstrapped clean cache')
        actual = bootstrap_snapshot(source, depot, workspace, pins, env)
        if recorded != actual:
            raise ValueError('Bootstrapped source/dependency contents changed; refusing cache reuse')
        return source, depot, env
    # An interrupted explicit bootstrap must not leave a previous successful
    # marker authorizing reuse without this run completing its hooks.
    marker.unlink(missing_ok=True)
    if not depot.exists():
        run(['git', 'clone', '--depth', '1', pins['depotTools']['repository'], depot], cache)
    origin = run(['git', 'remote', 'get-url', 'origin'], depot, capture=True).strip()
    if origin != pins['depotTools']['repository']:
        raise ValueError('Unexpected depot_tools origin')
    run(['git', 'fetch', '--depth', '1', 'origin', pins['depotTools']['revision']], depot)
    run(['git', 'checkout', '--detach', pins['depotTools']['revision']], depot)
    if target.startswith('windows-'):
        initialize_windows_depot(depot, env)
    else:
        initialize_depot(depot, env)
    # Android ABIs share the fixed Linux-host DEPS checkout but have independent
    # GN output directories, so x64 cannot replace ARM64 objects or binaries.
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
    state = bootstrap_snapshot(source, depot, workspace, pins, env)
    temporary_marker = marker.with_name('bootstrap-state.json.publishing')
    temporary_marker.write_text(json.dumps(state, indent=2) + '\n')
    os.replace(temporary_marker, marker)
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
            'use_custom_libcxx': True, 'v8_enable_i18n_support': True,
            'v8_enable_temporal_support': True, 'icu_use_data_file': False, 'use_remoteexec': False,
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
    expected = (0x01000007 if target == 'macos-x64' else 0x0100000C) if target.startswith(('macos-', 'ios-')) else (183 if target == 'android-arm64' else 62)
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
                if target.startswith(('macos-', 'ios-')):
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



def archive_members(path):
    """Yield native/archive metadata members, resolving GNU, COFF and BSD names."""
    with path.open('rb') as archive:
        if archive.read(8) != b'!<arch>\n':
            raise ValueError('Rust SDK input must be a complete archive: ' + str(path))
        names = b''
        total = path.stat().st_size
        while archive.tell() < total:
            header = archive.read(60)
            if len(header) != 60 or header[58:] != b'`\n':
                raise ValueError('Invalid Rust archive header')
            name = header[:16].decode('ascii').strip()
            size = int(header[48:58].decode('ascii').strip())
            if size < 0 or size > total - archive.tell():
                raise ValueError('Truncated Rust archive')
            payload = archive.read(size)
            if size % 2 and archive.read(1) != b'\n':
                raise ValueError('Invalid Rust archive padding')
            if name == '//':
                names = payload
                continue
            if name in ('/', '/SYM64/') or name.startswith('__.SYMDEF'):
                continue
            if name.startswith('#1/'):
                length = int(name[3:])
                if length < 0 or length > len(payload):
                    raise ValueError('Invalid Rust BSD member name')
                name = payload[:length].rstrip(b'\0').decode('utf-8')
                payload = payload[length:]
            elif re.fullmatch(r'/[0-9]+', name):
                offset = int(name[1:])
                # Microsoft COFF longnames are NUL terminated; GNU names
                # terminate with slash-newline. Rust emits both formats.
                terminator = b'\0' if b'\0' in names else b'/\n'
                end = names.find(terminator, offset)
                if offset >= len(names) or end < 0:
                    raise ValueError('Invalid Rust extended archive member name')
                name = names[offset:end].decode('utf-8')
            else:
                name = name.removesuffix('/')
            if name.startswith('__.SYMDEF'):
                continue
            yield name, payload


def validate_rust_object(data, target):
    if target.startswith(('macos-', 'ios-')):
        expected = 0x01000007 if target == 'macos-x64' else 0x0100000C
        if len(data) < 16 or data[:4] != b'\xcf\xfa\xed\xfe':
            raise ValueError('Rust SDK object must be native Mach-O64 (no LLVM bitcode)')
        machine, kind = struct.unpack_from('<I', data, 4)[0], struct.unpack_from('<I', data, 12)[0]
        if machine != expected or kind != 1:
            raise ValueError('Rust SDK Mach-O object architecture mismatch')
    elif target.startswith('windows-'):
        expected = 0xAA64 if target.endswith('arm64') else 0x8664
        machine = struct.unpack_from('<H', data, 6 if data[:4] == b'\0\0\xff\xff' else 0)[0] if len(data) >= 20 else 0
        if machine != expected:
            raise ValueError('Rust SDK object must be matching native COFF (no LLVM bitcode)')
    else:
        expected = 183 if target.endswith('arm64') else 62
        if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01':
            raise ValueError('Rust SDK object must be native ELF64 (no LLVM bitcode)')
        kind, machine = struct.unpack_from('<HH', data, 16)
        if machine != expected or kind != 1:
            raise ValueError('Rust SDK ELF object architecture mismatch')


def ninja_list(value):
    # GN writes these concrete paths as Ninja tokens, including $-escaped spaces.
    tokens, token, index = [], '', 0
    while index < len(value):
        char = value[index]
        if char == '$':
            index += 1
            if index >= len(value) or value[index] not in (' ', '$', ':'):
                raise ValueError('Unexpected variable in GN Rust archive paths')
            token += value[index]
        elif char.isspace():
            if token: tokens.append(token); token = ''
        else:
            token += char
        index += 1
    if token: tokens.append(token)
    return tokens


def sdk_rust_inputs(source, out):
    # The static-library alink rule does not include GN's implicit rlibs in its
    # response file. Use the exact target link list rather than all host crates.
    ninja = out / 'obj/v8_monolith.ninja'
    text = ninja.read_text().replace('$\n', '')
    values = re.findall(r'^  rlibs = (.*)$', text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError('Full V8 SDK requires one official monolith Rust link list')
    paths = list(dict.fromkeys(ninja_list(values[0])))
    if not paths or not any('temporal_capi' in path for path in paths) or not any('libstd_' in path for path in paths):
        raise ValueError('Full V8 SDK Rust link list must include Temporal and Rust stdlib')
    inputs = []
    for name in paths:
        path = (out / name).resolve()
        if not path.is_relative_to(source.resolve()) or path.suffix != '.rlib':
            raise ValueError('Rust SDK input escapes fixed V8 source or is not an rlib')
        inputs.append(path)
    return inputs


def sdk_rust_runtime(source, out, target, env):
    inputs = sdk_rust_inputs(source, out)
    metadata = out / 'sdk-metadata'; metadata.mkdir(exist_ok=True)
    destination = metadata / ('v8_rust_runtime.lib' if target.startswith('windows-') else 'libv8_rust_runtime.a')
    evidence, count = [], 0
    with destination.open('wb') as archive:
        archive.write(b'!<arch>\n')
        for input_index, path in enumerate(inputs):
            objects, skipped = 0, 0
            for name, data in archive_members(path):
                if name.endswith('.rmeta'):
                    skipped += 1
                    continue
                validate_rust_object(data, target)
                # Archive member names may repeat across crates; unique short
                # names preserve every native object when indexing/linking.
                member = f'r{input_index:04x}{objects:06x}.o/'
                header = f'{member:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n'.encode('ascii')
                if len(header) != 60:
                    raise ValueError('Rust SDK object exceeds archive header limits')
                archive.write(header); archive.write(data)
                if len(data) % 2: archive.write(b'\n')
                objects += 1
            count += objects
            evidence.append({'path': path.relative_to(source.resolve()).as_posix(), 'sha256': sha(path),
                             'nativeObjects': objects, 'metadataMembersOmitted': skipped})
    if count == 0:
        raise ValueError('Rust SDK runtime has no native objects')
    if target.startswith('windows-'):
        # The official Windows Clang package ships lld-link, not llvm-ar.
        # Match toolchain.gni's librarian mode and flatten the native-only
        # archive into a complete indexed COFF library (never a thin archive).
        tool = source / 'third_party/llvm-build/Release+Asserts/bin/lld-link.exe'
        indexed = destination.with_name('v8_rust_runtime.indexed.lib')
        indexed.unlink(missing_ok=True)
        run([tool, '/lib', '/OUT:' + str(indexed), destination], source, env)
        merged_objects = 0
        for name, data in archive_members(indexed):
            validate_rust_object(data, target)
            merged_objects += 1
        if merged_objects != count:
            raise ValueError('Official Windows librarian lost Rust native objects')
        indexed.replace(destination)
    else:
        tool = source / 'third_party/llvm-build/Release+Asserts/bin/llvm-ar'
        run([tool, ('--format=darwin' if target.startswith(('macos-', 'ios-')) else '--format=gnu'), 's', destination], source, env)
    report = metadata / 'rust-runtime.json'
    report.write_text(json.dumps({'schemaVersion': 1, 'target': target, 'inputs': evidence,
                                 'nativeObjects': count, 'sha256': sha(destination)}, indent=2) + '\n')
    return destination, report


def sdk_archive_dependency(source, out, path, target, env):
    path = path.resolve()
    if not path.is_relative_to(source.resolve()) or path.suffix not in ('.a', '.lib'):
        raise ValueError('SDK archive dependency must come from fixed source checkout')
    with path.open('rb') as handle:
        magic = handle.read(4)
    if magic in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        if not target.startswith(('macos-', 'ios-')):
            raise ValueError('Universal Apple archive on non-Apple target')
        output = out / 'sdk-metadata/archive-dependencies' / path.name
        output.parent.mkdir(parents=True, exist_ok=True)
        run(['xcrun', 'lipo', '-thin', 'x86_64' if target == 'macos-x64' else 'arm64', path, '-output', output], source, env)
        path = output
    if target.startswith(('windows-', 'linux-')):
        platform_module('desktop').inspect_archive(path, target)
    else:
        inspect_apple_android_archive(path, target)
    return path


def sdk_library_arguments(root, contract):
    grouping = contract.get('staticLibraryGrouping')
    if grouping not in (None, 'rescan'):
        raise ValueError('Unsupported static library grouping policy')
    libraries = [root / name for name in contract['libraries']]
    return ['-Wl,--start-group', *libraries, '-Wl,--end-group'] if grouping == 'rescan' else libraries


def sdk_abi_options(source, out, depot, env):
    flags = gn_property(source, out, depot, env, '//:v8_monolith', 'cflags_cc')
    names = {'-fexperimental-relative-c++-abi-vtables', '-fno-experimental-relative-c++-abi-vtables'}
    return [flag for flag in flags if flag in names or flag.removeprefix('/clang:') in names]


ANDROID_LINK_PROBE = """#include <v8.h>
#include <libplatform/libplatform.h>
extern "C" int v8_sdk_link_test() {
  if (!v8::V8::InitializeICUDefaultLocation(nullptr)) return 2;
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
    auto code = v8::String::NewFromUtf8Literal(isolate,
        "new Intl.NumberFormat('de-DE').format(1234.5) === '1.234,5' && "
        "Temporal.PlainDate.from('2024-01-02').add({days:1}).toString() === '2024-01-03' && "
        "typeof WebAssembly.compile === 'function' && typeof Promise === 'function'");
    auto script = v8::Script::Compile(context, code).ToLocalChecked();
    auto value = script->Run(context).ToLocalChecked();
    result = value->BooleanValue(isolate) ? 0 : 1;
  }
  isolate->Dispose();
  delete allocator;
  v8::V8::Dispose();
  v8::V8::DisposePlatform();
  return result;
}
"""


def inspect_android_consumer(path, target):
    data = path.read_bytes()
    if target not in ('android-arm64', 'android-x64') or len(data) < 64 or data[:6] != b'\x7fELF\x02\x01':
        raise ValueError('Android SDK consumer must be a little-endian ELF64 shared library')
    kind, machine = struct.unpack_from('<HH', data, 16)
    expected = 183 if target == 'android-arm64' else 62
    if kind != 3 or machine != expected:
        raise ValueError('Android SDK consumer shared-library architecture mismatch')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    if size < 56 or count == 0 or offset + size * count > len(data):
        raise ValueError('Invalid Android SDK consumer ELF program headers')
    alignments = []
    for index in range(count):
        position = offset + index * size
        if struct.unpack_from('<I', data, position)[0] == 1:
            alignment = struct.unpack_from('<Q', data, position + 48)[0]
            if alignment < 16384 or alignment & (alignment - 1):
                raise ValueError('Android SDK consumer LOAD segments must support 16 KiB pages')
            alignments.append(alignment)
    if not alignments:
        raise ValueError('Android SDK consumer has no LOAD segments')
    return {'elfMachine': machine, 'loadSegmentAlignments': alignments}


def android_link_smoke(source, target, sdk_root, compiler, env):
    sysroot = source / 'third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot'
    if not sysroot.is_dir():
        raise ValueError('Pinned official Android NDK sysroot missing')
    contract = json.loads((sdk_root / 'linking.json').read_text())
    probe = sdk_root / 'validation/android-sdk-link.cc'
    binary = sdk_root / 'validation/libv8_sdk_link_test.so'
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(ANDROID_LINK_PROBE)
    command = [compiler, *contract['compileOptions'], '-shared', '-fPIC',
               '--sysroot=' + str(sysroot), '-fuse-ld=lld', '-Wl,--no-undefined']
    command += ['-I' + str(sdk_root / name) for name in contract['includeDirs']]
    command += ['-D' + value for value in contract['defines']]
    command += [probe, '-o', binary]
    command += sdk_library_arguments(sdk_root, contract)
    command += contract['linkOptions']
    command += ['-l' + value for value in contract['systemLibraries']]
    run(command, source, env)
    inspection = inspect_android_consumer(binary, target)
    report = {'passed': True, 'runtimeExecuted': False, 'binaryInspection': inspection,
              'binary': 'validation/libv8_sdk_link_test.so', 'binarySha256': sha(binary),
              'monolithSha256': sha(sdk_root / next(name for name in contract['libraries'] if 'v8_monolith' in name)),
              'linkingSha256': sha(sdk_root / 'linking.json'),
              'compilerVersion': run([compiler, '--version'], source, env, capture=True).strip(),
              'command': [str(argument) for argument in command],
              'sysrootRequirement': contract.get('sysrootRequirement'),
              'host': {'os': platform.system(), 'cpu': platform.machine()}}
    report_path = sdk_root / 'validation/android-link-report.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    return report, {'validation/android-sdk-link.cc': probe,
                    'validation/libv8_sdk_link_test.so': binary,
                    'validation/android-link-report.json': report_path}


def apple_android_profile(source, out, target, pins, defines, depot, env):
    monolith = gn_output(source, out, depot, env, '//:v8_monolith')
    runtime = gn_output(source, out, depot, env, '//sdk_runtime:v8_cxx_runtime')
    libraries = {'lib/' + monolith.name: monolith, 'lib/' + runtime.name: runtime}
    archive_dependencies, official_system_libraries = [], []
    for library in libraries.values():
        inspect_apple_android_archive(library, target)
    if target.startswith('android-'):
        architecture = 'aarch64' if target == 'android-arm64' else 'x86_64'
        builtins = list((source / 'third_party/llvm-build/Release+Asserts/lib/clang').glob(
            '*/lib/linux/libclang_rt.builtins-' + architecture + '-android.a'))
        if len(builtins) != 1:
            raise ValueError('Expected one pinned Android compiler builtins archive')
        libraries['lib/' + builtins[0].name] = builtins[0]
        inspect_apple_android_archive(builtins[0], target)
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
    for vendor_headers in (source / 'buildtools/third_party/libc++', out / 'gen/buildtools/third_party/libc++'):
        for path in vendor_headers.rglob('*'):
            if path.is_file() and (path.name.startswith('__') or path.suffix in ('.h', '.inc')):
                headers[(Path('runtime/include/config') / path.relative_to(vendor_headers)).as_posix()] = path
    if 'runtime/include/config/__assertion_handler' not in headers:
        raise ValueError('Pinned libc++ vendor assertion header missing')
    headers['runtime/include/config/__config_site'] = site
    options = ['-std=c++20', '-fno-rtti', '-fno-exceptions', '-nostdinc++', '-fPIC']
    link = ['-nostdlib++']
    if target.startswith('macos-'):
        triple = ('arm64' if target == 'macos-arm64' else 'x86_64') + '-apple-macos13.0'
        options += ['--target=' + triple, '-mmacosx-version-min=13.0']
        link += ['--target=' + triple, '-mmacosx-version-min=13.0']
        frameworks = gn_property(source, out, depot, env, '//:v8_monolith', 'frameworks')
        for framework in frameworks:
            name = Path(framework).name.removesuffix('.framework')
            if not re.fullmatch(r'[A-Za-z0-9_]+', name):
                raise ValueError('Invalid official Apple framework name')
            link += ['-framework', name]
        systems = ['pthread']
    else:
        triple = ('aarch64' if target == 'android-arm64' else 'x86_64') + '-linux-android26'
        options += ['--target=' + triple]
        # Chromium builds its own libunwind through libc++abi. The complete
        # runtime archive includes those objects; match the official driver
        # flag instead of accidentally selecting an unrelated NDK unwind lib.
        official_ldflags = gn_property(source, out, depot, env, '//:v8_monolith', 'ldflags')
        if '--unwindlib=none' not in official_ldflags:
            raise ValueError('Pinned Android custom runtime must disable toolchain unwind library')
        link += ['--target=' + triple, '-Wl,-z,max-page-size=16384', '--unwindlib=none']
        # Allocator shim's all_dependent_configs require link-time wrappers,
        # including __real_realpath/getcwd references pulled in by ICU.
        # Preserve the complete symbol set from this fixed GN graph.
        wrapper_flags = gn_property(source, out, depot, env,
                                    '//third_party/partition_alloc/src/partition_alloc:wrap_malloc_symbols', 'ldflags')
        for flag in dict.fromkeys(official_ldflags + wrapper_flags):
            if flag.startswith(('-Wl,-wrap,', '-Wl,--wrap=')):
                if not re.fullmatch(r'-Wl,(?:-wrap,|--wrap=)[A-Za-z_][A-Za-z0-9_]*', flag):
                    raise ValueError('Invalid official Android allocator wrapper flag')
                link.append(flag)
        systems = ['dl', 'log', 'm']
    official_libraries = gn_property(source, out, depot, env, '//:v8_monolith', 'libs')
    for library in official_libraries:
        if library.startswith('//'):
            archive_dependencies.append(source / library[2:])
        elif re.fullmatch(r'[A-Za-z0-9_+.-]+', library) and not library.endswith(('.a', '.lib')):
            official_system_libraries.append(library)
        else:
            raise ValueError('Unsupported official SDK dependency library: ' + library)
    systems = list(dict.fromkeys(systems + official_system_libraries))
    return {'libraries': libraries, 'runtimeHeaders': headers, 'archiveDependencies': archive_dependencies, 'linking': {
        'schemaVersion': 1, 'includeDirs': ['include', 'runtime/include/config', 'runtime/include/c++', 'runtime/include/abi'],
        'defines': defines, 'compileOptions': options, 'libraries': list(libraries),
        'linkOptions': link, 'systemLibraries': systems,
        'compilerStyle': 'clang++', 'cxxRuntime': 'pinned Chromium libc++ (__Cr ABI)',
        'sysrootRequirement': ({'kind': 'apple-macos-sdk', 'minOS': '13.0'} if target.startswith('macos-') else
                               {'kind': 'android-ndk', 'minApi': 26, 'target': triple})}}


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
    abi_flags = sdk_abi_options(source, out, depot, env)
    if profile['linking'].get('compilerStyle') == 'clang-cl':
        abi_flags = [flag if flag.startswith('/clang:') else '/clang:' + flag for flag in abi_flags]
    profile['linking']['compileOptions'] += abi_flags
    profile['linking']['abiCompileOptions'] = list(dict.fromkeys(profile['linking'].get('abiCompileOptions', []) + abi_flags))
    for dependency in profile.get('archiveDependencies', []):
        library = sdk_archive_dependency(source, out, dependency, target, env)
        name = 'lib/' + library.name
        if name in profile['libraries'] and sha(profile['libraries'][name]) != sha(library):
            raise ValueError('SDK archive dependency filename conflict: ' + name)
        profile['libraries'][name] = library
        if name not in profile['linking']['libraries']: profile['linking']['libraries'].append(name)
    rust_runtime, rust_evidence = sdk_rust_runtime(source, out, target, env)
    rust_name = 'lib/' + rust_runtime.name
    profile['libraries'][rust_name] = rust_runtime
    profile['linking']['libraries'].insert(1, rust_name)
    if target.startswith(('linux-', 'android-')):
        profile['linking']['staticLibraryGrouping'] = 'rescan'
    profile['linking']['featureProfile'] = {'internationalization': True, 'temporal': True,
        'icuData': 'embedded', 'jit': 'upstream-default', 'webAssembly': 'upstream-default',
        'experimentalRuntimeFlags': []}
    files = sdk_headers(source, out) | profile['libraries'] | profile['runtimeHeaders']
    files['rust-runtime.json'] = rust_evidence
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
    if target.startswith('android-'):
        with tempfile.TemporaryDirectory(prefix='v8-sdk-android-consumer-') as temporary:
            sdk_root = Path(temporary)
            for name, path in files.items():
                staged = sdk_root / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, staged)
            report, validation_files = android_link_smoke(source, target, sdk_root, compiler, env)
            entry['validation']['linkTested'] = True
            entry['linkSmoke'] = report
            return publish_sdk(source, target, pins, files | validation_files, entry, output_root)
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
    source, depot, env = bootstrap(cache, args.target, pins, reuse=args.action == 'build')
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
