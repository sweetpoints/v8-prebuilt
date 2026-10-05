"""Official Chromium GN desktop builds and binary ABI validation.

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
BRIDGE_EXPORTS = frozenset({
    'sv8_create', 'sv8_start', 'sv8_poll', 'sv8_resolve', 'sv8_sync_poll',
    'sv8_sync_reply', 'sv8_cancel', 'sv8_destroy', 'sv8_free', 'sv8_version',
})
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
        'use_custom_libcxx': True, 'v8_enable_i18n_support': False,
        'v8_enable_temporal_support': False, 'use_remoteexec': False,
        'symbol_level': 0, 'target_cpu': cpu, 'v8_target_cpu': cpu,
        'target_os': 'linux' if os_name == 'linux' else 'win',
        'is_clang': True, 'use_lld': True,
    }
    if os_name == 'linux':
        args['use_sysroot'] = True
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(args.items())) + '\n'


def output_directory(source, target):
    _target(target)
    return Path(source) / ('out/source_v8_' + target.replace('-', '_'))


def binary_name(target):
    os_name, _ = _target(target)
    return 'source_v8.dll' if os_name == 'windows' else 'libsource_v8.so'


def prepare(source, depot, env, target, run):
    """Explicitly install the pinned target sysroot, including the cross ARM64 one."""
    os_name, cpu = _target(target)
    if os_name == 'linux':
        import sys
        arch = 'amd64' if cpu == 'x64' else 'arm64'
        run([sys.executable, Path(source) / 'build/linux/sysroot_scripts/install-sysroot.py',
             '--arch=' + arch], source, env)


def inspect_binary(path, target):
    """Check binary headers independently of host architecture and loader."""
    os_name, _ = _target(target)
    data = Path(path).read_bytes()
    if os_name == 'linux':
        if len(data) < 64 or data[:6] != b'\x7fELF\x02\x01':
            raise ValueError('Desktop Linux bridge must be little-endian ELF64')
        kind, machine = struct.unpack_from('<HH', data, 16)
        if kind != 3 or machine != MACHINES[target]:
            raise ValueError('Linux ELF shared-library architecture mismatch')
        return {'elfMachine': machine}
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Windows bridge must contain a DOS/PE header')
    offset = struct.unpack_from('<I', data, 60)[0]
    if offset < 64 or offset + 26 > len(data) or data[offset:offset + 4] != b'PE\0\0':
        raise ValueError('Invalid Windows PE header')
    machine = struct.unpack_from('<H', data, offset + 4)[0]
    optional_size, flags = struct.unpack_from('<HH', data, offset + 20)
    if optional_size < 112 or offset + 24 + optional_size > len(data):
        raise ValueError('Invalid Windows PE optional header')
    if (machine != MACHINES[target] or not flags & 0x2000 or
            struct.unpack_from('<H', data, offset + 24)[0] != 0x20B):
        raise ValueError('Windows bridge must be a matching PE32+ DLL')
    return {'peMachine': machine}


def validate_binary(path, source, target, env, run):
    inspection = inspect_binary(path, target)
    os_name, _ = _target(target)
    tools = Path(source) / 'third_party/llvm-build/Release+Asserts/bin'
    suffix = '.exe' if platform.system() == 'Windows' else ''
    if os_name == 'linux':
        symbols = run([tools / ('llvm-nm' + suffix), '--dynamic', '--defined-only',
                       '--format=posix', path], source, env, capture=True)
        exported = {line.split()[0].split('@')[0] for line in symbols.splitlines() if line.strip()}
        dynamic = run([tools / ('llvm-readelf' + suffix), '--dynamic', '--version-info',
                       path], source, env, capture=True)
        dependencies = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', dynamic)
        if any('libc++' in name or 'libstdc++' in name for name in dependencies):
            raise ValueError('Linux bridge must embed its pinned C++ runtime')
        if '(TEXTREL)' in dynamic:
            raise ValueError('Linux bridge contains text relocations')
        versions = {tuple(map(int, value.split('.')))
                    for value in re.findall(r'\bGLIBC_(\d+(?:\.\d+)+)', dynamic)}
        if not versions or max(versions) > (2, 31):
            raise ValueError('Linux bridge must respect the pinned Bullseye glibc 2.31 baseline')
        inspection['maxGlibcVersion'] = '.'.join(map(str, max(versions)))
    else:
        exports = run([tools / ('llvm-readobj' + suffix), '--coff-exports', path],
                      source, env, capture=True)
        exported = set(re.findall(r'^\s*Name:\s*(\S+)\s*$', exports, re.MULTILINE))
        imports = run([tools / ('llvm-readobj' + suffix), '--coff-imports', path],
                      source, env, capture=True)
        dependencies = re.findall(r'^\s*Name:\s*(\S+)\s*$', imports, re.MULTILINE)
        if any(re.match(r'(?:vcruntime|msvcp|libc\+\+)', name, re.I) for name in dependencies):
            raise ValueError('Windows bridge must use the pinned non-component static CRT')
    if exported != BRIDGE_EXPORTS:
        raise ValueError('Desktop bridge exports differ from the sv8 C ABI')
    inspection['exports'] = sorted(exported)
    inspection['dependencies'] = sorted(set(dependencies))
    return inspection


def initialize_depot_windows(depot, env, run):
    """Initialize official pinned CIPD/Python/Git wrappers without updating source.

    DEPOT_TOOLS_UPDATE=0 skips update_depot_tools.bat, which normally invokes
    bootstrap/win_tools.bat. gclient's git_cache still requires git.bat, even
    when Git for Windows is already installed. Run the official bootstrap
    directly rather than enabling automatic source updates.
    """
    run(['cmd.exe', '/d', '/c', Path(depot) / 'bootstrap/win_tools.bat'], depot, env)
    run(['cmd.exe', '/d', '/c', Path(depot) / 'gclient.bat', '--version'], depot, env)


def create_overlay(source, bridge_exports=BRIDGE_EXPORTS):
    """Desktop-only GN inputs; preserve the existing shared bridge source digest.

    Return every generated build input for the independent platform input hash.
    The caller copies the unchanged source_v8.cpp/h before invoking this function.
    """
    if set(bridge_exports) != BRIDGE_EXPORTS:
        raise ValueError('Desktop overlay requires the complete sv8 ABI')
    overlay = Path(source) / 'source_v8'
    overlay.mkdir(parents=True, exist_ok=True)
    exports = overlay / 'windows_exports.def'
    exports.write_text('LIBRARY source_v8\nEXPORTS\n' +
                       ''.join('  ' + name + '\n' for name in sorted(bridge_exports)))
    version_map = overlay / 'linux_exports.map'
    version_map.write_text('{\n  global:\n' +
                           ''.join('    ' + name + ';\n' for name in sorted(bridge_exports)) +
                           '  local: *;\n};\n')
    build = overlay / 'BUILD.gn'
    build.write_text('''# Generated by the pinned desktop builder; hashed separately from bridge ABI.
shared_library("source_v8") {
  output_name = "source_v8"
  sources = [ "//source_v8/source_v8.cpp" ]
  deps = [ "//:v8_monolith" ]
  configs += [ "//:external_config" ]
  configs -= [ "//build/config/clang:find_bad_constructs" ]
  if (is_win) {
    ldflags = [ "/DEF:" + rebase_path("//source_v8/windows_exports.def", root_build_dir) ]
    inputs = [ "//source_v8/windows_exports.def" ]
    libs = [ "winmm.lib" ]
  } else if (is_linux) {
    ldflags = [
      "-Wl,--no-undefined",
      "-Wl,--exclude-libs,ALL",
      "-Wl,--version-script=" + rebase_path("//source_v8/linux_exports.map", root_build_dir),
    ]
    inputs = [ "//source_v8/linux_exports.map" ]
    libs = [ "dl", "m", "pthread" ]
  } else {
    assert(false, "Desktop overlay requires Linux or Windows")
  }
}
''')
    return {'desktop/BUILD.gn': build, 'desktop/windows_exports.def': exports,
            'desktop/linux_exports.map': version_map}
