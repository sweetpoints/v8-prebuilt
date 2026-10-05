"""iOS contract tests, plus small real Apple SDK object/archive checks when available."""
import copy
import json
from pathlib import Path
import platform
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ios

PINS = {'targets': {
    'ios-arm64': {'cpu': 'arm64', 'environment': 'device', 'minIOS': '15.0'},
    'ios-simulator-arm64': {'cpu': 'arm64', 'environment': 'simulator', 'minIOS': '15.0'},
}}


def object_bytes(build_platform=2, cpu=0x0100000c, filetype=1):
    header = struct.pack('<8I', 0xfeedfacf, cpu, 0, filetype, 1, 24, 0, 0)
    return header + struct.pack('<6I', 0x32, 24, build_platform, 15 << 16, 27 << 16, 0)


def archive_bytes(data, name='sample.o/'):
    header = f'{name:<16}{0:<12}{0:<6}{0:<6}{0:<8}{len(data):<10}`\n'.encode()
    return b'!<arch>\n' + header + data + (b'\n' if len(data) & 1 else b'')


class IOSContractTest(unittest.TestCase):
    def test_distinct_arm64_device_and_simulator_arguments(self):
        for target, environment in [('ios-arm64', 'device'), ('ios-simulator-arm64', 'simulator')]:
            args = ios.gn_arguments(target, PINS)
            self.assertIn('target_environment = ' + json.dumps(environment), args)
            self.assertIn('target_cpu = "arm64"', args)
            for flag in ['v8_jitless = true', 'use_custom_libcxx = false', 'v8_enable_pointer_compression = false',
                         'v8_enable_sandbox = false', 'v8_enable_webassembly = false', 'is_component_build = false',
                         'v8_use_external_startup_data = false', 'v8_monolithic_for_shared_library = false']:
                self.assertIn(flag, args)
        self.assertNotEqual(ios.output_directory(Path('/source'), 'ios-arm64'), ios.output_directory(Path('/source'), 'ios-simulator-arm64'))

    def test_invalid_platform_host_and_version_rejected(self):
        bad = copy.deepcopy(PINS)
        bad['targets']['ios-arm64']['environment'] = 'simulator'
        with self.assertRaises(ValueError): ios.gn_arguments('ios-arm64', bad)
        bad = copy.deepcopy(PINS)
        bad['targets']['ios-arm64']['minIOS'] = '14.0'
        with self.assertRaises(ValueError): ios.gn_arguments('ios-arm64', bad)
        with patch('ios.platform.system', return_value='Linux'):
            with self.assertRaises(ValueError): ios.require_host('ios-arm64')
        with self.assertRaises(ValueError): ios.target_os('macos-arm64')

    def test_jitless_overlay_precedes_initialization_without_mutating_base(self):
        base = 'prefix\n    V8::InitializePlatform(runtime_platform.get());\n    V8::Initialize();'
        derived = ios.platform_source(base)
        self.assertIn('V8::SetFlagsFromString("--jitless");\n    V8::InitializePlatform', derived)
        self.assertNotIn('SetFlagsFromString', base)
        for invalid in ['', base + base]:
            with self.assertRaises(ValueError): ios.platform_source(invalid)
        self.assertIn('static_library(', ios.STATIC_GN)
        self.assertNotIn('shared_library(', ios.STATIC_GN)

    def test_platform_archive_rejects_host_and_dylib_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lib.a'
            for target, value in [('ios-arm64', 2), ('ios-simulator-arm64', 7)]:
                path.write_bytes(archive_bytes(object_bytes(value)))
                self.assertEqual(ios.inspect_static_archive(path, target)['platform'], value)
                other = 'ios-simulator-arm64' if target == 'ios-arm64' else 'ios-arm64'
                with self.assertRaises(ValueError): ios.inspect_static_archive(path, other)
            for data in [object_bytes(1), object_bytes(2, filetype=6), object_bytes(2, cpu=0x01000007), b'bitcode', b'']:
                path.write_bytes(archive_bytes(data))
                with self.assertRaises(ValueError): ios.inspect_static_archive(path, 'ios-arm64')
            for data in [b'!<thin>\n', b'!<arch>\n', archive_bytes(object_bytes())[:-1]]:
                path.write_bytes(data)
                with self.assertRaises(ValueError): ios.inspect_static_archive(path, 'ios-arm64')

    def test_bsd_extended_member_names_and_symbol_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lib.a'
            name = b'long-example.o\0\0'
            member = archive_bytes(name + object_bytes(), '#1/' + str(len(name)))
            symbols = archive_bytes(b'index', '__.SYMDEF/')[8:]
            path.write_bytes(b'!<arch>\n' + symbols + member[8:])
            self.assertEqual(ios.inspect_static_archive(path, 'ios-arm64')['objectCount'], 1)

    def test_exact_bridge_exports_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lib.a'
            path.write_bytes(archive_bytes(object_bytes()))
            run = lambda *args, **kwargs: '\n'.join('000 T _' + name for name in ios.common.BRIDGE_EXPORTS)
            self.assertEqual(len(ios.validate_binary(path, Path(directory), 'ios-arm64', {}, run)['bridgeExports']), 10)
            with self.assertRaises(ValueError): ios.validate_binary(path, Path(directory), 'ios-arm64', {}, lambda *a, **k: '_sv8_create')

    def test_link_contract_is_unambiguous_static_system_libcxx(self):
        for target in ios.TARGETS:
            contract = ios.link_contract(target, PINS, ['V8_JITLESS'])
            self.assertFalse(contract['dynamicLoading'])
            self.assertTrue(contract['linkExactlyOneArchive'])
            self.assertEqual(contract['runtimeFlags'], ['--jitless'])
            self.assertEqual(contract['bridgeArchive'], 'lib/libsource_v8.a')
            self.assertIn('system; not bundled', contract['stdlib'])
            self.assertFalse(contract['externalStartupData'])
            for name in ios.common.BRIDGE_EXPORTS:
                self.assertIn('-Wl,-u,_' + name, contract['bridgeLinkArguments'])
                self.assertIn('-Wl,-exported_symbol,_' + name, contract['bridgeLinkArguments'])
            self.assertEqual(contract['clangTarget'].endswith('-simulator'), target == 'ios-simulator-arm64')

    def test_inventory_indexes_every_file_by_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'include').mkdir()
            (root / 'include/source_v8.h').write_text('header')
            entries = ios.inventory(root, 'ios-arm64')
            self.assertEqual(entries[0]['path'], 'ios-arm64/include/source_v8.h')
            self.assertEqual(entries[0]['sha256'], ios.common.sha(root / 'include/source_v8.h'))


@unittest.skipUnless(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'Apple ARM64 SDK required')
class AppleSDKObjectTest(unittest.TestCase):
    def test_real_device_and_simulator_archive_members_have_distinct_platforms(self):
        # This proves the actual SDK/archive inspection contract, not a V8 runtime build.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'probe.c'
            source.write_text('int probe(void) { return 42; }\n')
            for target in ios.TARGETS:
                sdk = subprocess.check_output(['xcrun', '--sdk', ios.SDKS[target], '--show-sdk-path'], text=True).strip()
                obj = root / (target + '.o')
                archive = root / (target + '.a')
                triple = ios.link_contract(target, PINS, [])['clangTarget']
                subprocess.run(['xcrun', 'clang', '-target', triple, '-isysroot', sdk, '-c', str(source), '-o', str(obj)], check=True)
                subprocess.run(['xcrun', 'libtool', '-static', '-o', str(archive), str(obj)], check=True)
                self.assertEqual(ios.inspect_static_archive(archive, target)['platform'], ios.PLATFORMS[target])

    def test_real_apple_link_probe_is_preserved_without_claiming_v8_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for target in ios.TARGETS:
                sdk_dir = root / target
                (sdk_dir / 'include').mkdir(parents=True)
                (sdk_dir / 'lib').mkdir()
                shutil.copyfile(ios.common.ROOT / 'src/source_v8.h', sdk_dir / 'include/source_v8.h')
                stub = sdk_dir / 'stub.cpp'
                stub.write_text('#include "source_v8.h"\n'
                    'void *sv8_create(int,int){return nullptr;}\n'
                    'void sv8_start(void*,const char*,const char*,const char*){}\n'
                    'char *sv8_poll(void*){return nullptr;}\n'
                    'void sv8_cancel(void*){}\nvoid sv8_destroy(void*){}\n'
                    'void sv8_resolve(void*,int,const char*,int){}\n'
                    'char *sv8_sync_poll(void*){return nullptr;}\n'
                    'void sv8_sync_reply(void*,int,const char*,int){}\n'
                    'void sv8_free(char*){}\nconst char *sv8_version(){return "stub";}\n')
                sdk = subprocess.check_output(['xcrun', '--sdk', ios.SDKS[target], '--show-sdk-path'], text=True).strip()
                obj = sdk_dir / 'stub.o'
                contract = ios.link_contract(target, PINS, [])
                subprocess.run(['xcrun', 'clang++', '-target', contract['clangTarget'], '-isysroot', sdk,
                    '-I', str(sdk_dir / 'include'), '-c', str(stub), '-o', str(obj)], check=True)
                subprocess.run(['xcrun', 'libtool', '-static', '-o', str(sdk_dir / 'lib/libsource_v8.a'), str(obj)], check=True)
                (sdk_dir / 'linking.json').write_text(json.dumps(contract))
                proof = ios.link_smoke(sdk_dir, root, target, PINS, dict(os.environ))
                self.assertTrue(proof['passed'])
                self.assertFalse(proof['runtimeExecuted'])
                self.assertEqual(proof['executableInspection']['platform'], ios.PLATFORMS[target])
                for name in ['link-smoke', 'link-smoke.cpp', 'link-smoke.json']:
                    self.assertTrue((sdk_dir / 'validation' / name).is_file())


if __name__ == '__main__': unittest.main()
