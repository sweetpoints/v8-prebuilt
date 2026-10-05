import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import desktop


class DesktopTest(unittest.TestCase):
    def pins(self):
        return {'v8': {'revision': 'a' * 40}, 'targets': {target: {'cpu': target.split('-')[1]} for target in desktop.TARGETS}}

    def object(self, target, bigobj=False):
        data = bytearray(24)
        if target.startswith('linux'):
            data[:6] = b'\x7fELF\x02\x01'
            struct.pack_into('<HH', data, 16, 1, desktop.MACHINES[target])
        elif bigobj:
            struct.pack_into('<HHHH', data, 0, 0, 0xFFFF, 2, desktop.MACHINES[target])
        else:
            struct.pack_into('<H', data, 0, desktop.MACHINES[target])
        return data

    def archive(self, members):
        data = b'!<arch>\n'
        for name, member in members:
            header = f'{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(member):<10}`\n'.encode()
            data += header + bytes(member) + (b'\n' if len(member) % 2 else b'')
        return data

    def test_target_configuration_is_relocatable_static_sdk_with_jit(self):
        for target in desktop.TARGETS:
            args = desktop.gn_arguments(target, self.pins())
            self.assertIn('target_cpu = ' + json.dumps(target.split('-')[1]), args)
            self.assertIn('v8_monolithic_for_shared_library = true', args)
            self.assertIn('use_custom_libcxx = true', args)
            self.assertIn('use_thin_lto = false', args)
            self.assertNotIn('v8_jitless', args)
            self.assertEqual(target.startswith('linux'), 'use_sysroot = true' in args)
        with self.assertRaises(ValueError):
            desktop.gn_arguments('linux-x86', self.pins())
        wrong = self.pins()
        wrong['targets']['linux-x64']['cpu'] = 'arm64'
        with self.assertRaises(ValueError):
            desktop.gn_arguments('linux-x64', wrong)

    def test_only_official_compiler_supported_hosts(self):
        with patch('desktop.platform.system', return_value='Linux'), patch('desktop.platform.machine', return_value='x86_64'):
            desktop.require_host('linux-arm64')
        with patch('desktop.platform.system', return_value='Linux'), patch('desktop.platform.machine', return_value='aarch64'):
            with self.assertRaises(ValueError):
                desktop.require_host('linux-arm64')
        with patch('desktop.platform.system', return_value='Windows'), patch('desktop.platform.machine', return_value='ARM64'):
            desktop.require_host('windows-arm64')
        env = {'SAFE': 'retained'}
        self.assertEqual('0', desktop.environment('windows-arm64', env)['DEPOT_TOOLS_WIN_TOOLCHAIN'])
        self.assertEqual({'SAFE': 'retained'}, env)

    def test_archive_validates_every_native_member_and_bigobj(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library'
            for target in desktop.TARGETS:
                path.write_bytes(self.archive([('/', b'index'), ('member.obj/', self.object(target))]))
                self.assertEqual(1, desktop.inspect_archive(path, target)['objectCount'])
                other = target.replace('x64', 'arm64') if target.endswith('x64') else target.replace('arm64', 'x64')
                path.write_bytes(self.archive([('ok.obj/', self.object(target)), ('wrong.obj/', self.object(other))]))
                with self.assertRaises(ValueError):
                    desktop.inspect_archive(path, target)
            path.write_bytes(self.archive([('big.obj/', self.object('windows-arm64', bigobj=True))]))
            desktop.inspect_archive(path, 'windows-arm64')
            data = self.object('windows-arm64', bigobj=True)
            struct.pack_into('<H', data, 4, 0)
            path.write_bytes(self.archive([('import.obj/', data)]))
            with self.assertRaises(ValueError):
                desktop.inspect_archive(path, 'windows-arm64')

    def test_archive_rejects_thin_truncated_empty_and_bitcode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library'
            for data in (b'!<thin>\n', b'!<arch>\n', self.archive([('a.o/', self.object('linux-x64'))])[:-1], self.archive([('a.o/', b'BC\xc0\xde')])):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    desktop.inspect_archive(path, 'linux-x64')

    def test_target_output_isolation_and_explicit_sysroot(self):
        self.assertEqual(4, len({desktop.output_directory(Path('v8'), t) for t in desktop.TARGETS}))
        calls = []
        desktop.prepare(Path('v8'), Path('depot'), {}, 'linux-arm64', lambda *args, **kwargs: calls.append(args))
        self.assertEqual('--arch=arm64', calls[0][0][-1])
        desktop.prepare(Path('v8'), Path('depot'), {}, 'windows-arm64', lambda *args, **kwargs: self.fail('unexpected sysroot download'))

    def test_windows_bootstrap_generates_official_git_wrappers_before_gclient(self):
        calls = []
        env = {'DEPOT_TOOLS_UPDATE': '0', 'DEPOT_TOOLS_WIN_TOOLCHAIN': '0'}
        depot = Path('pinned-depot')
        desktop.initialize_depot_windows(depot, env, lambda *args, **kwargs: calls.append(args))
        self.assertEqual(['cmd.exe', '/d', '/c', depot / 'bootstrap/win_tools.bat'], calls[0][0])
        self.assertEqual(['cmd.exe', '/d', '/c', depot / 'gclient.bat', '--version'], calls[1][0])
        self.assertTrue(all(call[2] == env for call in calls))
        self.assertEqual('0', env['DEPOT_TOOLS_UPDATE'])

    def test_runtime_target_only_archives_upstream_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = desktop.create_runtime_target(Path(directory))
            text = inputs['sdk_runtime/BUILD.gn'].read_text()
            self.assertIn('complete_static_lib = true', text)
            self.assertIn('//buildtools/third_party/libc++', text)
            self.assertIn('//:v8_monolith', text)
            self.assertNotIn('sources =', text)
            self.assertNotIn('source_v8', text)

    def test_sdk_profile_uses_actual_gn_outputs_and_matching_runtime_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            out = source / 'out/sdk'
            out.mkdir(parents=True)
            for target in ('linux-arm64', 'windows-x64'):
                suffix = '.lib' if target.startswith('windows') else '.a'
                paths = {}
                for label, name in (('//:v8_monolith', 'upstream-monolith'), ('//sdk_runtime:v8_cxx_runtime', 'official-runtime')):
                    path = out / (name + suffix)
                    path.write_bytes(self.archive([('member.o/', self.object(target))]))
                    paths[label] = path
                for directory in ('third_party/libc++/src/include', 'third_party/libc++abi/src/include'):
                    path = source / directory / 'header'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('official-header')
                config = out / 'gen/buildtools/third_party/libc++/__config_site'
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text('matching-generated-config')
                def run(args, *a, **kw):
                    self.assertIn('--root-target=//sdk_runtime:sdk', args)
                    return json.dumps({args[3]: {'outputs': ['//' + paths[args[3]].relative_to(source).as_posix()]}})
                result = desktop.sdk_profile(source, out, target, self.pins(), ['V8_COMPRESS_POINTERS'], run, 'gn', {})
                self.assertEqual(2, len(result['libraries']))
                self.assertEqual(config, result['runtimeHeaders']['include/c++/config/__config_site'])
                self.assertEqual(['V8_COMPRESS_POINTERS'], result['linking']['defines'])
                self.assertTrue(all(not Path(name).is_absolute() for name in result['linking']['libraries']))
                self.assertEqual('clang-cl' if target.startswith('windows') else 'clang++', result['linking']['compilerStyle'])
                self.assertEqual(['lib/' + path.name for path in paths.values()], result['linking']['libraries'])
                flags = result['linking']['compileOptions']
                self.assertIn('--target=' + ('x86_64-pc-windows-msvc' if target.startswith('windows') else 'aarch64-linux-gnu'), flags)
                self.assertIn('/GR-' if target.startswith('windows') else '-fno-rtti', flags)
                self.assertIn('/clang:-fno-exceptions' if target.startswith('windows') else '-fno-exceptions', flags)


if __name__ == '__main__':
    unittest.main()
