"""Offline tests: no source checkout, downloads, or compiler execution."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('official_v8_builder', Path(__file__).with_name('build.py'))
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def elf_fixture(machine=183, alignment=16384):
    binary = bytearray(120)
    binary[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<HH', binary, 16, 3, machine)
    struct.pack_into('<Q', binary, 32, 64)
    struct.pack_into('<HH', binary, 54, 56, 1)
    struct.pack_into('<I', binary, 64, 1)
    struct.pack_into('<Q', binary, 112, alignment)
    return bytes(binary)


def mach_fixture(cpu=0x0100000C, minimum=13 << 16):
    data = bytearray(56)
    data[:4] = b'\xcf\xfa\xed\xfe'
    struct.pack_into('<IIIII', data, 4, cpu, 0, 6, 1, 24)
    struct.pack_into('<IIIIII', data, 32, 0x32, 24, 1, minimum, minimum, 0)
    return bytes(data)


class BuildContractTests(unittest.TestCase):

    def test_macos_macho_and_exports_reject_wrong_cpu_and_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'libsource_v8.dylib'
            for target, cpu in [('macos-arm64', 0x0100000C), ('macos-x64', 0x01000007)]:
                binary.write_bytes(mach_fixture(cpu))
                symbols = '\n'.join('_' + name + ' T 0 1' for name in sorted(builder.BRIDGE_EXPORTS))
                with patch.object(builder, 'run', return_value=symbols):
                    self.assertEqual(builder.validate_macos_binary(binary, root, target, {})['machCpuType'], cpu)
                with patch.object(builder, 'run', return_value=symbols + '\n_bad_export T 0 1'):
                    with self.assertRaisesRegex(ValueError, 'exports differ'):
                        builder.validate_macos_binary(binary, root, target, {})
                with self.assertRaisesRegex(ValueError, 'architecture'):
                    builder.inspect_macos_binary(binary, 'macos-x64' if target == 'macos-arm64' else 'macos-arm64')
                binary.write_bytes(mach_fixture(cpu, 14 << 16))
                with self.assertRaisesRegex(ValueError, '13.0'):
                    builder.inspect_macos_binary(binary, target)

    def test_standalone_paths_and_original_bridge_labels(self):
        self.assertEqual(builder.ROOT, Path(__file__).resolve().parents[2])
        self.assertEqual(builder.DEFAULT_CACHE_ROOT, builder.ROOT / '.cache/v8-source')
        self.assertEqual(builder.DEFAULT_OUTPUT_ROOT, builder.ROOT / 'artifacts')
        self.assertEqual(set(builder.bridge_files()), {
            'src/source_v8.cpp', 'src/source_v8.h', 'src/android_exports.map',
            'tool/v8/source_v8.gni',
        })
        self.assertTrue(all(path.is_file() for path in builder.bridge_files().values()))
        self.assertNotIn('flutter', str(builder.bridge_files()['src/source_v8.cpp'].relative_to(builder.ROOT)))

    def test_macos_x64_uses_native_intel_host_and_pinned_cpu(self):
        with patch.object(builder.platform, 'system', return_value='Darwin'):
            with patch.object(builder.platform, 'machine', return_value='x86_64'):
                builder.require_host('macos-x64')
            with patch.object(builder.platform, 'machine', return_value='arm64'):
                with self.assertRaisesRegex(ValueError, 'x86_64'):
                    builder.require_host('macos-x64')
        args = builder.gn_arguments('macos-x64')
        self.assertIn('target_cpu = "x64"', args)
        self.assertIn('v8_target_cpu = "x64"', args)
        self.assertIn('mac_deployment_target = "13.0"', args)
        self.assertNotIn('android_ndk_api_level', args)

    def test_explicit_stable_pins_file_accepts_new_version_and_rejects_bad_provenance(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stable-pins.json'
            pins = builder.read_pins()
            pins['v8']['revision'] = 'a' * 40
            pins['v8']['version'] = '16.1.2.3'
            path.write_text(json.dumps(pins))
            self.assertEqual(builder.read_pins(path)['v8']['version'], '16.1.2.3')
            for version in ['16.1.2.0', '16.01.2', 'latest', 16, None]:
                pins['v8']['version'] = version
                path.write_text(json.dumps(pins))
                with self.assertRaises(ValueError):
                    builder.read_pins(path)
            pins['v8']['version'] = '16.1.2'
            pins['v8']['repository'] = 'https://chromium.googlesource.com/unexpected.git'
            path.write_text(json.dumps(pins))
            with self.assertRaisesRegex(ValueError, 'official'):
                builder.read_pins(path)

    def test_cli_threads_explicit_pins_cache_and_output_into_build(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'stable-pins.json'
            pins = builder.read_pins()
            pins['v8']['version'] = '16.1.2.3'
            path.write_text(json.dumps(pins))
            with patch.object(builder.sys, 'argv', ['build.py', 'build', '--target', 'macos-x64',
                 '--pins-file', str(path), '--cache-root', str(root / 'cache'),
                 '--output-root', str(root / 'artifacts'), '--jobs', '2']), \
                 patch.object(builder, 'bootstrap', return_value=(root / 'source', root / 'depot', {})) as bootstrap, \
                 patch.object(builder, 'build') as build:
                builder.main()
                self.assertEqual(bootstrap.call_args.args, ((root / 'cache').resolve(), 'macos-x64', pins))
                self.assertEqual(build.call_args.args, (root / 'source', root / 'depot', {}, 'macos-x64', 2, pins))
                self.assertEqual(build.call_args.kwargs, {'output_root': (root / 'artifacts').resolve()})

    def test_ios_build_dispatch_receives_explicit_pins_and_output(self):
        pins = builder.read_pins()
        with patch.object(builder, 'platform_module') as module:
            source, depot, output = Path('/source'), Path('/depot'), Path('/artifacts')
            result = builder.build(source, depot, {}, 'ios-arm64', 2, pins, output)
            module.assert_called_once_with('ios')
            module.return_value.build.assert_called_once_with(source, depot, {}, 'ios-arm64', 2, pins, output)
            self.assertIs(result, module.return_value.build.return_value)

    def test_windows_bootstrap_uses_scoped_environment_and_batch_tools(self):
        pins = builder.read_pins()
        desktop = builder.platform_module('desktop')
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'depot_tools-windows').mkdir()
            def checkout_run(args, cwd, env=None, capture=False):
                if capture and args[1:4] == ['remote', 'get-url', 'origin']:
                    return pins['depotTools']['repository']
                if capture and args[1:3] == ['rev-parse', 'HEAD']:
                    return pins['v8']['revision']
            with patch.object(builder, 'require_host'), patch.object(builder, 'run', side_effect=checkout_run) as execute:
                source, _, env = builder.bootstrap(cache, 'windows-arm64', pins)
                self.assertEqual(env['DEPOT_TOOLS_WIN_TOOLCHAIN'], '0')
                self.assertEqual(source.parent.name, 'windows-x64')
                self.assertEqual(execute.call_args_list[3].args[0][:3], ['cmd.exe', '/d', '/c'])
            depot = Path(directory)
            with patch.object(builder.os, 'name', 'nt'):
                self.assertEqual(builder.depot_command(depot, 'gn').name, 'gn.bat')
                self.assertEqual(builder.depot_command(depot, 'gclient').name, 'gclient.bat')

    def test_pins_are_official_fixed_revisions(self):
        pins = builder.read_pins()
        self.assertEqual(pins['v8']['version'], '15.4.80.24')
        self.assertEqual(pins['v8']['revision'], 'e422f6ef0c7b877b04e4872fd0bd3a1cc2ec2eee')
        self.assertEqual(len(pins['depotTools']['revision']), 40)

    def test_minimal_official_depot_initialization_avoids_extra_venvs(self):
        with patch.object(builder, 'run') as execute:
            depot = Path('/isolated/.cache/depot_tools')
            env = {'DEPOT_TOOLS_UPDATE': '0'}
            builder.initialize_depot(depot, env)
            command, working, passed_env = execute.call_args.args
            self.assertEqual(command[:2], ['bash', '-c'])
            self.assertIn('source "$1/bootstrap_python3"; bootstrap_python3', command[2])
            self.assertIn('source "$1/cipd_bin_setup.sh"; cipd_bin_setup', command[2])
            self.assertNotIn('ensure_bootstrap', command[2])
            self.assertNotIn('gsutil', command[2])
            self.assertNotIn('pylint', command[2])
            self.assertEqual(command[-1], depot)
            self.assertEqual(working, depot)
            self.assertEqual(passed_env['DEPOT_TOOLS_UPDATE'], '0')

    def test_android_refuses_mac_and_linux_arm_host(self):
        for host in [('Darwin', 'arm64'), ('Linux', 'aarch64')]:
            with patch.object(builder.platform, 'system', return_value=host[0]), patch.object(builder.platform, 'machine', return_value=host[1]):
                for target in ('android-arm64', 'android-x64'):
                    with self.assertRaisesRegex(ValueError, 'Linux x86_64'):
                        builder.require_host(target)
        with patch.object(builder.platform, 'system', return_value='Linux'), patch.object(builder.platform, 'machine', return_value='x86_64'):
            builder.require_host('android-arm64')
            builder.require_host('android-x64')

    def test_android_abis_share_only_checkout_not_output_or_artifact(self):
        pins = builder.read_pins()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'depot_tools-linux-x86_64').mkdir()
            def checkout_run(args, cwd, env=None, capture=False):
                if capture and args[1:4] == ['remote', 'get-url', 'origin']:
                    return pins['depotTools']['repository']
                if capture and args[1:3] == ['rev-parse', 'HEAD']:
                    return pins['v8']['revision']
                return None
            with patch.object(builder, 'require_host'), patch.object(builder, 'initialize_depot'), patch.object(builder, 'run', side_effect=checkout_run):
                arm, _, _ = builder.bootstrap(cache, 'android-arm64', pins)
                x64, _, _ = builder.bootstrap(cache, 'android-x64', pins)
            self.assertEqual(arm, x64)
            self.assertIn("target_os = ['android']", (arm.parent / '.gclient').read_text())
            self.assertEqual(arm.parent.name, 'android-arm64')

    def test_android_x64_gn_matches_fixed_toolchain_with_distinct_cpu(self):
        args = builder.gn_arguments('android-x64')
        self.assertIn('target_cpu = "x64"', args)
        self.assertIn('v8_target_cpu = "x64"', args)
        self.assertIn('target_os = "android"', args)
        self.assertIn('android_ndk_api_level = 26', args)
        self.assertNotIn('mac_deployment_target', args)
        self.assertEqual(builder.read_pins()['targets']['android-x64']['abi'], 'x86_64')
        with self.assertRaisesRegex(ValueError, 'Unsupported V8 target'):
            builder.gn_arguments('unknown-target')

    def test_android_actual_elf_machine_and_16k_segments_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'libsource_v8.so'
            for target, machine in [('android-arm64', 183), ('android-x64', 62)]:
                binary.write_bytes(elf_fixture(machine))
                actual = builder.inspect_android_elf(binary, target)
                self.assertEqual(actual['elfMachine'], machine)
                self.assertEqual(actual['loadSegmentAlignments'], [16384])
                with self.assertRaisesRegex(ValueError, 'differs'):
                    builder.inspect_android_elf(binary, 'android-x64' if machine == 183 else 'android-arm64')
                binary.write_bytes(elf_fixture(machine, 4096))
                with self.assertRaisesRegex(ValueError, '16 KiB'):
                    builder.inspect_android_elf(binary, target)
            binary.write_bytes(b'not ELF')
            with self.assertRaisesRegex(ValueError, 'ELF64'):
                builder.inspect_android_elf(binary, 'android-x64')

    def test_android_export_validation_rejects_missing_or_cpp_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'libsource_v8.so'
            binary.write_bytes(elf_fixture(62))
            exports = '\n'.join(name + ' T 0 1' for name in sorted(builder.BRIDGE_EXPORTS))
            with patch.object(builder, 'run', return_value=exports):
                actual = builder.validate_android_binary(binary, root, 'android-x64', {})
                self.assertEqual(actual['exports'], sorted(builder.BRIDGE_EXPORTS))
            for bad in [exports.replace('sv8_version', 'bad_version'), exports + '\n_ZN2v8Something T 0 1']:
                with patch.object(builder, 'run', return_value=bad):
                    with self.assertRaisesRegex(ValueError, 'exports differ'):
                        builder.validate_android_binary(binary, root, 'android-x64', {})

    def test_bridge_digest_uses_sorted_length_prefixed_labels_and_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Path(directory) / 'a'; a.write_bytes(b'first')
            b = Path(directory) / 'b'; b.write_bytes(b'second')
            expected = hashlib.sha256()
            for label, data in [(b'one', b'first'), (b'two', b'second')]:
                expected.update(struct.pack('>Q', len(label)) + label + struct.pack('>Q', len(data)) + data)
            with patch.object(builder, 'bridge_files', return_value={'two': b, 'one': a}):
                self.assertEqual(builder.bridge_digest(), expected.hexdigest())

    def test_shared_bridge_and_v8_use_same_libcxx_no_external_snapshot(self):
        args = builder.gn_arguments('android-arm64')
        self.assertIn('use_custom_libcxx = true', args)
        self.assertIn('v8_monolithic_for_shared_library = true', args)
        self.assertIn('v8_use_external_startup_data = false', args)
        self.assertIn('android_ndk_api_level = 26', args)
        mac_args = builder.gn_arguments('macos-arm64')
        self.assertIn('mac_deployment_target = "13.0"', mac_args)
        self.assertIn('use_lld = false', mac_args)
        self.assertNotIn('use_lld', args)

    def test_version_zero_patch_and_license_notice_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'v8'; (source / 'include').mkdir(parents=True)
            (source / 'include/v8-version.h').write_text('#define V8_MAJOR_VERSION 15\n#define V8_MINOR_VERSION 7\n#define V8_BUILD_NUMBER 36\n#define V8_PATCH_LEVEL 0\n')
            self.assertEqual(builder.source_version(source), '15.7.36')
            (source / 'LICENSE').write_text('license'); (source / 'AUTHORS').write_text('authors')
            dependency = source / 'third_party/example'; dependency.mkdir(parents=True)
            (dependency / 'NOTICE').write_text('notice')
            output = Path(directory) / 'artifact'
            entries = builder.package_licenses(source, output)
            self.assertEqual({entry['path'] for entry in entries}, {'licenses/LICENSE', 'licenses/AUTHORS', 'licenses/third_party/example/NOTICE', 'licenses/source_v8/LICENSE'})
            for entry in entries:
                self.assertEqual(builder.sha(output / entry['path']), entry['sha256'])

    def test_license_conflicts_reject_before_replacing_any_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'; source.mkdir()
            (source / 'third_party').mkdir()
            destination = Path(directory) / 'artifact'
            (source / 'LICENSE').write_text('original license')
            entries = builder.package_licenses(source, destination)
            (source / 'LICENSE').write_text('different license')
            (source / 'AUTHORS').write_text('new authors')
            with self.assertRaisesRegex(ValueError, 'License provenance conflict'):
                builder.package_licenses(source, destination, entries)
            self.assertEqual((destination / 'licenses/LICENSE').read_text(), 'original license')
            self.assertFalse((destination / 'licenses/AUTHORS').exists())

    def test_manifest_records_binaries_and_refuses_provenance_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'checkout/v8'
            (source / 'include').mkdir(parents=True)
            (source / 'include/v8-version.h').write_text('#define V8_MAJOR_VERSION 15\n#define V8_MINOR_VERSION 4\n#define V8_BUILD_NUMBER 80\n#define V8_PATCH_LEVEL 24\n')
            (source / 'DEPS').write_text('official pinned dependencies')
            (source / 'LICENSE').write_text('BSD')
            (source / 'third_party').mkdir()
            out = source / 'out/source_v8'; out.mkdir(parents=True)
            (out / 'libsource_v8.dylib').write_bytes(mach_fixture())
            (out / 'libsource_v8.so').write_bytes(elf_fixture())
            x64out = source / 'out/source_v8_android_x64'; x64out.mkdir(parents=True)
            (x64out / 'libsource_v8.so').write_bytes(elf_fixture(62))
            macx64out = source / 'out/source_v8_macos_x64'; macx64out.mkdir(parents=True)
            (macx64out / 'libsource_v8.dylib').write_bytes(mach_fixture(0x01000007))
            linuxout = source / 'out/source_v8_linux_x64'; linuxout.mkdir(parents=True)
            (linuxout / 'libsource_v8.so').write_bytes(elf_fixture(62))
            bridge = root / 'bridge'; bridge.mkdir()
            files = {}
            for name in ['source_v8.cpp', 'source_v8.h', 'android_exports.map', 'source_v8.gni']:
                path = bridge / name; path.write_text(name)
                files[name] = path
            def mocked_run(args, cwd, env=None, capture=False):
                if 'gen' in args:
                    self.assertIn('--root-target=//source_v8:source_v8', args)
                if not capture:
                    return None
                if '--version-info' in args:
                    return '(NEEDED) Shared library: [libc.so.6]\nName: GLIBC_2.31'
                if '--extern-only' in args:
                    return '\n'.join('_' + name + ' T 0 1' for name in sorted(builder.BRIDGE_EXPORTS))
                if '--dynamic' in args:
                    return '\n'.join(name + ' T 0 1' for name in sorted(builder.BRIDGE_EXPORTS))
                if 'revinfo' in args:
                    return 'dependency@fixedsha'
                if 'desc' in args:
                    self.assertIn('--root-target=//source_v8:source_v8', args)
                    return '{"defines": ["feature"]}'
                return 'official toolchain version'
            pins = builder.read_pins()
            package = root / 'package'
            with patch.object(builder, 'DEFAULT_OUTPUT_ROOT', package), patch.object(builder, 'bridge_files', return_value=files), patch.object(builder, 'run', side_effect=mocked_run):
                linux_notice = source / 'third_party/LinuxOnly/NOTICE'
                linux_notice.parent.mkdir()
                linux_notice.write_text('Linux dependency notice')
                builder.build(source, root / 'depot', {}, 'android-arm64', 1, pins)
                linux_notice.unlink()
                builder.build(source, root / 'depot', {}, 'android-x64', 1, pins)
                builder.build(source, root / 'depot', {}, 'macos-arm64', 1, pins)
                builder.build(source, root / 'depot', {}, 'macos-x64', 1, pins)
                builder.build(source, root / 'depot', {}, 'linux-x64', 1, pins)
                artifact = package / pins['v8']['revision']
                manifest = __import__('json').loads((artifact / 'manifest.json').read_text())
                self.assertEqual(set(manifest['targets']), {'android-arm64', 'android-x64', 'macos-arm64', 'macos-x64', 'linux-x64'})
                self.assertEqual(manifest['targets']['android-x64']['abi'], 'x86_64')
                self.assertEqual(manifest['targets']['android-x64']['binaryInspection']['elfMachine'], 62)
                self.assertEqual(manifest['targets']['android-arm64']['binaryInspection']['elfMachine'], 183)
                self.assertEqual((out / 'libsource_v8.so').read_bytes(), elf_fixture())
                self.assertIn('appleLinker', manifest['targets']['macos-arm64']['toolchain'])
                self.assertIn('xcode', manifest['targets']['macos-arm64']['toolchain'])
                self.assertIn('macSdk', manifest['targets']['macos-arm64']['toolchain'])
                self.assertNotIn('appleLinker', manifest['targets']['android-arm64']['toolchain'])
                self.assertIn('licenses/third_party/LinuxOnly/NOTICE', {entry['path'] for entry in manifest['licenses']})
                self.assertEqual((artifact / 'licenses/third_party/LinuxOnly/NOTICE').read_text(), 'Linux dependency notice')
                for target in manifest['targets'].values():
                    self.assertEqual(builder.sha(artifact / target['binary']), target['sha256'])
                    self.assertEqual(builder.sha(artifact / target['header']), target['headerSha256'])
                    self.assertEqual(target['artifactKind'], 'shared-bridge')
                    self.assertEqual(len(target['targetFiles']), 8 if 'platformBuildInputSha256' in target else 5)
                    for item in target['targetFiles']:
                        self.assertEqual(builder.sha(artifact / item['path']), item['sha256'])
                        self.assertNotIn('\\', item['path'])
                    self.assertFalse(target['validation']['runtimeTested'])
                    self.assertFalse(target['validation']['sourceCompatibilityTested'])
                old = (artifact / 'macos-arm64/libsource_v8.dylib').read_bytes()
                files['source_v8.cpp'].write_text('changed bridge')
                (out / 'libsource_v8.dylib').write_bytes(mach_fixture())
                with self.assertRaisesRegex(ValueError, 'provenance differs'):
                    builder.build(source, root / 'depot', {}, 'macos-arm64', 1, pins)
                self.assertEqual((artifact / 'macos-arm64/libsource_v8.dylib').read_bytes(), old)
                files['source_v8.cpp'].write_text('source_v8.cpp')
                changed_tools = {**pins, 'depotTools': {**pins['depotTools'], 'revision': 'a' * 40}}
                with self.assertRaisesRegex(ValueError, 'provenance differs'):
                    builder.build(source, root / 'depot', {}, 'macos-arm64', 1, changed_tools)
                self.assertEqual((artifact / 'macos-arm64/libsource_v8.dylib').read_bytes(), old)
                for mutate_overlay in [False, True]:
                    files['source_v8.cpp'].write_text('source_v8.cpp')
                    def mutation_run(args, cwd, env=None, capture=False):
                        if 'autoninja' in str(args[0]):
                            changed = source / 'source_v8/source_v8.cpp' if mutate_overlay else files['source_v8.cpp']
                            changed.write_text('edited while compiler was running')
                        return mocked_run(args, cwd, env, capture)
                    with patch.object(builder, 'run', side_effect=mutation_run):
                        with self.assertRaisesRegex(ValueError, 'changed during build'):
                            builder.build(source, root / 'depot', {}, 'macos-arm64', 1, pins)
                    self.assertEqual((artifact / 'macos-arm64/libsource_v8.dylib').read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
