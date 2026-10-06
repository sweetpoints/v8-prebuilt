"""Official Chromium GN desktop static V8 SDK profiles.

Linux ARM64 is cross-built on Linux x64 using the pinned Debian sysroot.
Windows builds run on Windows with the installed Visual Studio toolchain.
Reference: https://v8.dev/docs/build-gn and pinned build/vs_toolchain.py,
build/linux/sysroot_scripts/install-sysroot.py, build/config/win/BUILD.gn.
"""
import json
import platform
import re
import struct
from pathlib import Path

TARGETS = ('linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64')
MACHINES = {'linux-x64': 62, 'linux-arm64': 183,
            'windows-x64': 0x8664, 'windows-arm64': 0xAA64}


def _target(target):
    if target not in TARGETS:
        raise ValueError('Unsupported desktop target: ' + target)
    return target.split('-', 1)


def require_host(target):
    os_name, _ = _target(target)
    host = (platform.system(), platform.machine().lower())
    allowed = {'linux': ('Linux', {'x86_64', 'amd64'}),
               'windows': ('Windows', {'amd64', 'x86_64', 'arm64', 'aarch64'})}
    system, cpus = allowed[os_name]
    if host[0] != system or host[1] not in cpus:
        raise ValueError(f'{target} requires {system} host with CPU in {sorted(cpus)}; found {host}')


def environment(target, env):
    os_name, _ = _target(target)
    result = dict(env)
    if os_name == 'windows':
        # Official Chromium local VS path: no Google-internal SDK download.
        result['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'
    return result


def gn_arguments(target, pins):
    os_name, cpu = _target(target)
    config = pins['targets'][target]
    if config['cpu'] != cpu:
        raise ValueError('Pinned desktop CPU does not match target')
    args = {
        'is_debug': False, 'is_component_build': False, 'v8_monolithic': True,
        'v8_monolithic_for_shared_library': True, 'v8_use_external_startup_data': False,
        'use_custom_libcxx': True, 'v8_enable_i18n_support': True,
        'v8_enable_temporal_support': True, 'icu_use_data_file': False, 'use_remoteexec': False,
        'symbol_level': 0, 'target_cpu': cpu, 'v8_target_cpu': cpu,
        'use_thin_lto': False, 'use_cxx23': False,
        'target_os': 'linux' if os_name == 'linux' else 'win',
        'is_clang': True, 'use_lld': True,
    }
    if os_name == 'linux':
        args['use_sysroot'] = True
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


def output_directory(source, target):
    _target(target)
    return Path(source) / ('out/source_v8_' + target.replace('-', '_'))


def prepare(source, depot, env, target, run):
    """Explicitly install the pinned target sysroot, including the cross ARM64 one."""
    os_name, cpu = _target(target)
    if os_name == 'linux':
        import sys
        arch = 'amd64' if cpu == 'x64' else 'arm64'
        run([sys.executable, Path(source) / 'build/linux/sysroot_scripts/install-sysroot.py',
             '--arch=' + arch], source, env)


def initialize_depot_windows(depot, env, run):
    """Initialize official pinned CIPD/Python/Git wrappers without updating source.

    DEPOT_TOOLS_UPDATE=0 skips update_depot_tools.bat, which normally invokes
    bootstrap/win_tools.bat. gclient's git_cache still requires git.bat, even
    when Git for Windows is already installed. Run the official bootstrap
    directly rather than enabling automatic source updates.
    """
    run(['cmd.exe', '/d', '/c', Path(depot) / 'bootstrap/win_tools.bat'], depot, env)
    run(['cmd.exe', '/d', '/c', Path(depot) / 'gclient.bat', '--version'], depot, env)



def create_runtime_target(source):
    """Archive the official C++ runtime dependency graph without modifying V8.

    Windows libc++ is an official source_set, rather than a static library.
    complete_static_lib packages its objects and the relevant platform ABI
    implementation. Follow Chromium's public shared_library_deps group because
    libc++ and common_deps both restrict direct visibility. V8's own
    v8_monolith archive remains unchanged.
    """
    directory = Path(source) / 'sdk_runtime'
    directory.mkdir(parents=True, exist_ok=True)
    build = directory / 'BUILD.gn'
    build.write_text('''# Pure SDK runtime archive: only upstream Chromium dependencies.
group("sdk") {
  deps = [ "//:v8_monolith", ":v8_cxx_runtime" ]
}
static_library("v8_cxx_runtime") {
  complete_static_lib = true
  output_name = "v8_cxx_runtime"
  deps = [ "//build/config:shared_library_deps" ]
  configs -= [ "//build/config/compiler:thin_archive" ]
}
''')
    return {'sdk_runtime/BUILD.gn': build}


def inspect_archive(path, target):
    """Validate every regular native member; reject thin or mismatched archives."""
    os_name, _ = _target(target)
    path = Path(path)
    total = path.stat().st_size
    objects = 0
    with path.open('rb') as archive:
        if archive.read(8) != b'!<arch>\n':
            raise ValueError('SDK library must be a complete regular static archive')
        while archive.tell() < total:
            header = archive.read(60)
            if len(header) != 60 or header[58:60] != b'`\n':
                raise ValueError('Invalid static archive member header')
            try:
                name = header[:16].decode('ascii').strip()
                size = int(header[48:58].decode('ascii').strip())
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError('Invalid static archive member metadata') from error
            start = archive.tell()
            end = start + size
            if size < 0 or end > total:
                raise ValueError('Truncated static archive member')
            if name.startswith('#1/'):
                try:
                    name_size = int(name[3:])
                except ValueError as error:
                    raise ValueError('Invalid BSD archive filename') from error
                if name_size > size or name_size < 0:
                    raise ValueError('Invalid BSD archive filename size')
                name = archive.read(name_size).rstrip(b'\0').decode('utf-8')
            if name not in ('/', '//', '/SYM64/') and not name.startswith('__.SYMDEF'):
                data = archive.read(min(24, end - archive.tell()))
                if os_name == 'linux':
                    if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01':
                        raise ValueError('SDK archive contains a non-native ELF64 object')
                    kind, machine = struct.unpack_from('<HH', data, 16)
                    if kind != 1:
                        raise ValueError('SDK archive member is not a relocatable object')
                else:
                    if len(data) < 20:
                        raise ValueError('SDK archive contains a truncated COFF object')
                    machine = struct.unpack_from('<H', data)[0]
                    if data[:4] == b'\0\0\xff\xff':
                        # Microsoft bigobj has the machine field at byte six.
                        if struct.unpack_from('<H', data, 4)[0] != 2:
                            raise ValueError('SDK archive contains an import object')
                        machine = struct.unpack_from('<H', data, 6)[0]
                if machine != MACHINES[target]:
                    raise ValueError('SDK static archive object architecture mismatch')
                objects += 1
            archive.seek(end + size % 2)
        if archive.tell() != total or objects == 0:
            raise ValueError('SDK archive has no objects or invalid alignment')
    return {'format': 'static-archive', 'objectMachine': MACHINES[target], 'objectCount': objects}


def _gn_values(source, out, label, key, run, gn, env, config=False):
    values = json.loads(run([gn, 'desc', out, label, key, '--format=json', '--root-target=//sdk_runtime:sdk'],
                            source, env, capture=True))
    if isinstance(values, dict):
        if set(values) != {label} or not isinstance(values[label], dict):
            raise ValueError('GN output target differs from requested label')
        values = values[label].get(key, [] if config else None)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError('GN ' + key + ' must contain a string array')
    return values


def _consumer_abi_options(flags):
    # Export ABI choices, never the build tree's absolute/relative include paths.
    exact = {'-fno-rtti', '-frtti', '/GR-', '/GR', '-fno-exceptions', '-fexceptions',
             '-fexperimental-relative-c++-abi-vtables', '-fno-experimental-relative-c++-abi-vtables',
             '-fshort-wchar', '-fno-short-wchar', '-fshort-enums', '-fno-short-enums',
             '-fsized-deallocation', '-fno-sized-deallocation'}
    prefixes = ('-std=', '/std:', '/EH', '-fc++-abi=', '-fclang-abi-compat=',
                '-fms-compatibility-version=', '-fpack-struct=')
    return [flag for flag in flags
            if (flag.removeprefix('/clang:') in exact or
                flag.removeprefix('/clang:').startswith(prefixes))]


def _gn_archive(source, out, label, run, gn, env, target):
    values = _gn_values(source, out, label, 'outputs', run, gn, env)
    extension = '.lib' if target.startswith('windows-') else '.a'
    archives = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError('GN output must be a path')
        path = (Path(source) / value[2:] if value.startswith('//') else
                Path(value) if Path(value).is_absolute() else Path(out) / value)
        if path.suffix == extension:
            if not path.resolve().is_relative_to(Path(out).resolve()):
                raise ValueError('GN archive output escaped target directory')
            inspect_archive(path, target)
            archives.append(path)
    if len(archives) != 1:
        raise ValueError('Expected exactly one static archive for ' + label)
    return archives[0]


def _link_dependencies(source, out, target, libs, ldflags):
    """Separate actual GN system libraries from source-local static inputs."""
    windows = target.startswith('windows-')
    system, archives = [], []
    values = list(libs)
    if windows:
        # Rust's official std configs deliberately express these as ldflags.
        values += [flag for flag in ldflags if flag.lower().endswith('.lib') and
                   not flag.startswith(('-', '/DEFAULTLIB:', '/defaultlib:'))]
    for value in values:
        value = value.replace('\\', '/')
        if value.startswith('//'):
            path = Path(source) / value[2:]
        elif Path(value).is_absolute() or re.match(r'^[A-Za-z]:/', value):
            path = Path(value)
        elif '/' in value:
            path = Path(out) / value
        else:
            if not re.fullmatch(r'[A-Za-z0-9_+.-]+', value):
                raise ValueError('GN system library is not a portable library name')
            name = value if not windows or value.lower().endswith('.lib') else value + '.lib'
            if name.lower() not in {item.lower() for item in system}:
                system.append(name)
            continue
        if path.suffix.lower() not in ('.a', '.lib') or not path.is_file():
            raise ValueError('GN archive dependency missing or unsupported: ' + value)
        path = path.resolve()
        if path not in archives:
            archives.append(path)
    return system, archives


def sdk_profile(source, out, target, pins, defines, run, gn, env):
    """Collect actual GN outputs plus their matching C++ runtime headers."""
    os_name, cpu = _target(target)
    if pins['targets'][target]['cpu'] != target.split('-')[1]:
        raise ValueError('Pinned desktop CPU does not match target')
    if not isinstance(defines, list) or any(not isinstance(item, str) for item in defines):
        raise ValueError('GN defines must be a string array')
    source, out = Path(source), Path(out)
    libraries = {}
    for label in ('//:v8_monolith', '//sdk_runtime:v8_cxx_runtime'):
        path = _gn_archive(source, out, label, run, gn, env, target)
        libraries['lib/' + path.name] = path
    headers = {}
    roots = ((source / 'third_party/libc++/src/include', 'include/c++/v1'),
             (source / 'third_party/libc++abi/src/include', 'include/c++abi'))
    for directory, destination in roots:
        if not directory.is_dir():
            raise ValueError('Pinned C++ runtime headers missing')
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                if path.is_symlink():
                    raise ValueError('Runtime header symlinks are not SDK payloads')
                headers[destination + '/' + path.relative_to(directory).as_posix()] = path
    vendor = source / 'buildtools/third_party/libc++'
    generated_vendor = out / 'gen/buildtools/third_party/libc++'
    vendor_names = {path.name for directory in (vendor, generated_vendor) if directory.is_dir()
                    for path in directory.iterdir() if path.is_file() and path.name.startswith('__')}
    if not {'__config_site', '__assertion_handler'}.issubset(vendor_names):
        raise ValueError('Matching Chromium libc++ vendor headers missing')
    for name in sorted(vendor_names):
        path = generated_vendor / name
        if not path.is_file():
            path = vendor / name
        if path.is_symlink():
            raise ValueError('Runtime vendor header symlinks are not SDK payloads')
        headers['include/c++/config/' + name] = path
    abi_options = _consumer_abi_options(
        _gn_values(source, out, '//:v8_monolith', 'cflags_cc', run, gn, env))
    official_libs = _gn_values(source, out, '//:v8_monolith', 'libs', run, gn, env)
    official_ldflags = _gn_values(source, out, '//:v8_monolith', 'ldflags', run, gn, env)
    # GN attaches these to final executables/shared libraries, not monolith.
    # In particular Linux Rust std needs default_libs' explicit pthread.
    official_libs += _gn_values(source, out, '//build/config:default_libs',
                               'libs', run, gn, env, config=True)
    # Static libraries do not necessarily inherit Rust's final-executable
    # configs. Read their resolved official values rather than copying names.
    for label in ('//build/rust/std:stdlib_dependent_libs',
                  '//build/rust/std:stdlib_public_dependent_libs'):
        official_libs += _gn_values(source, out, label, 'libs', run, gn, env, config=True)
        if os_name == 'windows':
            official_ldflags += _gn_values(source, out, label, 'ldflags', run, gn, env, config=True)
    system_libraries, archive_dependencies = _link_dependencies(
        source, out, target, official_libs, official_ldflags)
    triple = ({'x64': 'x86_64', 'arm64': 'aarch64'}[cpu] +
              ('-pc-windows-msvc' if os_name == 'windows' else '-linux-gnu'))
    linking = {
        'schemaVersion': 1,
        'compilerStyle': 'clang-cl' if os_name == 'windows' else 'clang++',
        'includeDirs': ['include', 'include/c++/config', 'include/c++/v1', 'include/c++abi'],
        'defines': defines,
        'compileOptions': (['/std:c++20', '/MT', '/GR-', '/clang:-fno-exceptions', '--target=' + triple] if os_name == 'windows' else
                           ['-std=c++20', '-nostdinc++', '-fPIC', '-fno-rtti', '-fno-exceptions', '--target=' + triple]),
        'libraries': list(libraries),
        'linkOptions': ['/machine:' + cpu] if os_name == 'windows' else ['-nostdlib++', '--target=' + triple],
        'systemLibraries': system_libraries,
        'cxxRuntime': 'pinned Chromium libc++ (__Cr ABI)',
        'featureProfile': {
            'internationalization': True, 'temporal': True, 'icuData': 'embedded',
            'jit': 'upstream-default', 'webAssembly': 'upstream-default',
            'experimentalRuntimeFlags': [],
        },
        'crt': 'static MSVC /MT' if os_name == 'windows' else 'system glibc',
    }
    linking['abiCompileOptions'] = abi_options
    linking['compileOptions'] = list(dict.fromkeys(linking['compileOptions'] + abi_options))
    if os_name == 'linux':
        actual_cflags = _gn_values(source, out, '//:v8_monolith', 'cflags', run, gn, env)
        if '-pthread' in actual_cflags:
            linking['compileOptions'].append('-pthread')
        linking['sysrootRequirement'] = {
            'kind': 'chromium-linux-sysroot', 'architecture': 'amd64' if cpu == 'x64' else 'arm64',
            'distribution': 'debian-bullseye',
            'sourceRevision': pins['v8']['revision'],
        }
    return {'libraries': libraries, 'runtimeHeaders': headers, 'linking': linking,
            'archiveDependencies': archive_dependencies}
