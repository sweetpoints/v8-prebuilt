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
                         'v8_use_external_startup_data = false', 'v8_monolithic_for_shared_library = false',
                         'ios_enable_code_signing = false', 'use_thin_lto = false',
                         'treat_warnings_as_errors = false']:
                self.assertIn(flag, args)
            self.assertNotIn('use_system_xcode =', args)
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

    def test_probe_uses_only_official_v8_api_and_initializes_jitless(self):
        self.assertIn('#include "v8.h"', ios.LINK_SMOKE)
        self.assertIn('v8::platform::NewDefaultPlatform()', ios.LINK_SMOKE)
        self.assertIn('v8::Script::Compile', ios.LINK_SMOKE)
        self.assertLess(ios.LINK_SMOKE.index('SetFlagsFromString("--jitless")'), ios.LINK_SMOKE.index('InitializePlatform'))
        self.assertNotIn('sv8_', ios.LINK_SMOKE)

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

    def test_archive_validation_does_not_require_an_app_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lib.a'
            path.write_bytes(archive_bytes(object_bytes()))
            with patch('ios.common.run', side_effect=AssertionError('No C ABI inspection expected')):
                self.assertEqual(ios.validate_binary(path, Path(directory), 'ios-arm64', {})['platform'], 2)

    def test_link_contract_is_unambiguous_static_system_libcxx(self):
        for target in ios.TARGETS:
            contract = ios.link_contract(target, PINS, ['V8_JITLESS'], ['Foundation.framework', 'Security.framework'], ['objc'])
            self.assertEqual(contract['schemaVersion'], 1)
            self.assertFalse(contract['dynamicLoading'])
            self.assertEqual(contract['runtimeFlags'], ['--jitless'])
            self.assertEqual(contract['libraries'], ['lib/libv8_monolith.a'])
            self.assertEqual(contract['includeDirs'], ['include'])
            self.assertEqual(contract['defines'], ['V8_JITLESS'])
            self.assertEqual(contract['systemLibraries'], ['c++', 'objc'])
            self.assertEqual(contract['linkOptions'], ['-framework', 'Foundation', '-framework', 'Security'])
            self.assertNotIn('CoreFoundation', contract['linkOptions'])
            self.assertIn('system; not bundled', contract['stdlib'])
            self.assertFalse(contract['externalStartupData'])
            self.assertNotIn('bridgeArchive', contract)
            self.assertEqual(contract['clangTarget'].endswith('-simulator'), target == 'ios-simulator-arm64')

    def test_probe_links_published_archive_and_defines_and_preserves_evidence(self):
        # Command/inspection test only; real V8 SDK link is mandatory in build().
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for target in ios.TARGETS:
                sdk_dir = root / target
                sdk_dir.mkdir()
                contract = ios.link_contract(target, PINS, ['V8_JITLESS'], ['Foundation.framework', 'Security.framework'], ['objc'])
                (sdk_dir / 'lib').mkdir()
                (sdk_dir / 'lib/libv8_monolith.a').write_bytes(b'static-archive')
                (sdk_dir / 'linking.json').write_text(json.dumps(contract))
                commands = []
                def run(args, *unused, **kwargs):
                    commands.append([str(arg) for arg in args])
                    if '--show-sdk-path' in args: return '/Apple/SDK'
                    Path(args[-1]).write_bytes(object_bytes(ios.PLATFORMS[target], filetype=2))
                with patch('ios.common.run', side_effect=run):
                    proof = ios.link_smoke(sdk_dir, root, target, PINS, {})
                self.assertTrue(proof['passed'])
                self.assertFalse(proof['runtimeExecuted'])
                self.assertIn('-DV8_JITLESS', commands[-1])
                self.assertIn('Security', commands[-1])
                self.assertIn('-lobjc', commands[-1])
                self.assertNotIn('CoreFoundation', commands[-1])
                self.assertEqual(proof['linkingSha256'], ios.common.sha(sdk_dir / 'linking.json'))
                self.assertIn(str(sdk_dir / 'lib/libv8_monolith.a'), commands[-1])
                self.assertFalse(any('sv8_' in arg for command in commands for arg in command))
                for name in ['link-smoke', 'link-smoke.cpp', 'link-smoke.json']:
                    self.assertTrue((sdk_dir / 'validation' / name).is_file())

    def test_gn_outputs_property_dict_is_unwrapped_and_target_root_is_pure_v8(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            out = source / 'out/ios'
            out.mkdir(parents=True)
            archive = out / 'libv8_monolith.a'
            archive.write_bytes(b'archive')
            response = {'//:v8_monolith': {'outputs': ['//out/ios/libv8_monolith.a']}}
            with patch('ios.common.run', return_value=json.dumps(response)) as run:
                self.assertEqual(ios.output_for(source, out, source / 'depot', {}, '//:v8_monolith'), archive)
                self.assertIn('--root-target=//:v8_monolith', run.call_args.args[0])

    def test_sdk_publication_excludes_producer_source_and_keeps_input_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'v8'
            source.mkdir()
            (source / 'include').mkdir()
            header = source / 'include/v8.h'
            header.write_text('#pragma once')
            (source / 'DEPS').write_text('fixed upstream dependencies')
            out = ios.output_directory(source, 'ios-arm64')
            out.mkdir(parents=True)
            archive = out / 'libv8_monolith.a'
            archive.write_bytes(archive_bytes(object_bytes()))
            pins = copy.deepcopy(PINS)
            pins['v8'] = {'version': '15.4.80.25'}
            expected_args = ios.gn_arguments('ios-arm64', pins)
            commands = []
            def run(args, *unused, **kwargs):
                commands.append([str(arg) for arg in args])
                return 'fixed toolchain/dependency information'
            def publish(src, target, actual_pins, files, entry, output_root):
                self.assertEqual(target, 'ios-arm64')
                self.assertEqual(set(files), {'include/v8.h', 'lib/libv8_monolith.a',
                    'linking.json', 'args.gn', 'defines.json', 'dependencies.txt'})
                self.assertEqual(files['args.gn'].read_text(), expected_args)
                self.assertRegex(entry['platformBuildInputSha256'], r'^[0-9a-f]{64}$')
                self.assertFalse(entry['validation']['runtimeTested'])
                return Path(directory) / 'published'
            with patch('ios.require_host'), patch('ios.common.source_version', return_value='15.4.80.25'), \
                 patch('ios.common.run', side_effect=run), patch('ios.output_for', return_value=archive), \
                 patch('ios.common.sdk_defines', return_value=['V8_TARGET_OS_IOS']) as defines, \
                 patch('ios.common.gn_property', return_value=[]), \
                 patch('ios.common.sdk_headers', return_value={'include/v8.h': header}), \
                 patch('ios.link_smoke', return_value={'passed': True, 'runtimeExecuted': False}), \
                 patch('ios.common.publish_sdk', side_effect=publish):
                self.assertEqual(ios.build(source, source / 'depot', {}, 'ios-arm64', 2, pins), Path(directory) / 'published')
                self.assertEqual(defines.call_args.kwargs['root_target'], '//:v8_monolith')
            self.assertTrue(any(command[-1] == 'v8_monolith' for command in commands))
            self.assertFalse(any('source_v8:source_v8' in arg for command in commands for arg in command))

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



if __name__ == '__main__': unittest.main()
