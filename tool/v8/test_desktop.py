import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import desktop


class DesktopTest(unittest.TestCase):
    def pins(self):
        return {'targets': {target: {'cpu': target.split('-')[1]} for target in desktop.TARGETS}}

    def binary(self, target):
        if target.startswith('linux'):
            data = bytearray(64)
            data[:6] = b'\x7fELF\x02\x01'
            struct.pack_into('<HH', data, 16, 3, desktop.MACHINES[target])
        else:
            data = bytearray(256)
            data[:2] = b'MZ'
            struct.pack_into('<I', data, 60, 64)
            data[64:68] = b'PE\0\0'
            struct.pack_into('<H', data, 68, desktop.MACHINES[target])
            struct.pack_into('<HHH', data, 84, 112, 0x2000, 0x20B)
        return data

    def test_target_configuration_preserves_cpu_and_jit(self):
        for target in desktop.TARGETS:
            args = desktop.gn_arguments(target, self.pins())
            self.assertIn('target_cpu = ' + json.dumps(target.split('-')[1]), args)
            self.assertIn('v8_monolithic_for_shared_library = true', args)
            self.assertIn('use_custom_libcxx = true', args)
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

    def test_architecture_headers_reject_wrong_abi_and_not_dll(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binary'
            for target in desktop.TARGETS:
                data = self.binary(target)
                path.write_bytes(data)
                desktop.inspect_binary(path, target)
                other = target.replace('x64', 'arm64') if target.endswith('x64') else target.replace('arm64', 'x64')
                with self.assertRaises(ValueError):
                    desktop.inspect_binary(path, other)
            struct.pack_into('<H', data, 86, 0)
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                desktop.inspect_binary(path, 'windows-arm64')
            path.write_bytes(b'MZ')
            with self.assertRaises(ValueError):
                desktop.inspect_binary(path, 'windows-arm64')

    def test_target_output_isolation_and_explicit_sysroot(self):
        self.assertEqual(4, len({desktop.output_directory(Path('v8'), t) for t in desktop.TARGETS}))
        calls = []
        desktop.prepare(Path('v8'), Path('depot'), {}, 'linux-arm64', lambda *args, **kwargs: calls.append(args))
        self.assertEqual('--arch=arm64', calls[0][0][-1])
        desktop.prepare(Path('v8'), Path('depot'), {}, 'windows-arm64', lambda *args, **kwargs: self.fail('unexpected sysroot download'))

    def test_overlay_exports_and_preserves_bridge_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source_v8').mkdir()
            header = root / 'source_v8/source_v8.h'
            header.write_text('unchanged C ABI')
            inputs = desktop.create_overlay(root)
            self.assertEqual('unchanged C ABI', header.read_text())
            self.assertEqual(3, len(inputs))
            exports = (root / 'source_v8/windows_exports.def').read_text().splitlines()[2:]
            self.assertEqual(desktop.BRIDGE_EXPORTS, {line.strip() for line in exports})
            self.assertIn('--no-undefined', inputs['desktop/BUILD.gn'].read_text())
            with self.assertRaises(ValueError):
                desktop.create_overlay(root, {'sv8_create'})

    def test_validation_checks_exports_runtime_dependencies_and_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'library'
            path.write_bytes(self.binary('linux-x64'))
            symbols = '\n'.join(name + ' T 0' for name in desktop.BRIDGE_EXPORTS)
            dynamic = '(NEEDED) Shared library: [libc.so.6]\nName: GLIBC_2.31'
            def runner(args, *a, **kw):
                return symbols if 'llvm-nm' in str(args[0]) else dynamic
            result = desktop.validate_binary(path, root, 'linux-x64', {}, runner)
            self.assertEqual('2.31', result['maxGlibcVersion'])
            dynamic = '(NEEDED) Shared library: [libstdc++.so.6]\nName: GLIBC_2.31'
            with self.assertRaises(ValueError):
                desktop.validate_binary(path, root, 'linux-x64', {}, runner)
            dynamic = 'Name: GLIBC_2.34'
            with self.assertRaises(ValueError):
                desktop.validate_binary(path, root, 'linux-x64', {}, runner)
            dynamic = 'Name: GLIBC_2.31'
            symbols += '\nprivate_extra T 0'
            with self.assertRaises(ValueError):
                desktop.validate_binary(path, root, 'linux-x64', {}, runner)
            path.write_bytes(self.binary('windows-arm64'))
            def windows_runner(args, *a, **kw):
                return ('\n'.join('  Name: ' + name for name in desktop.BRIDGE_EXPORTS)
                        if '--coff-exports' in args else '  Name: KERNEL32.dll')
            result = desktop.validate_binary(path, root, 'windows-arm64', {}, windows_runner)
            self.assertEqual(['KERNEL32.dll'], result['dependencies'])
            def bad_windows_runner(args, *a, **kw):
                return windows_runner(args, *a, **kw) if '--coff-exports' in args else '  Name: VCRUNTIME140.dll'
            with self.assertRaises(ValueError):
                desktop.validate_binary(path, root, 'windows-arm64', {}, bad_windows_runner)


if __name__ == '__main__':
    unittest.main()
