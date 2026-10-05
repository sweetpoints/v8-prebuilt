"""Offline pure SDK contracts: no downloads, source compilation or app bridge."""
import importlib.util
import json
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('pure_v8_builder', Path(__file__).with_name('build.py'))
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildContractTests(unittest.TestCase):
    def test_standalone_sdk_has_no_bridge_sources_or_gn_template(self):
        self.assertEqual(builder.ROOT, Path(__file__).resolve().parents[2])
        self.assertFalse((builder.ROOT / 'src/source_v8.cpp').exists())
        self.assertFalse((builder.HERE / 'source_v8.gni').exists())
        self.assertNotIn('BRIDGE_EXPORTS', Path(builder.__file__).read_text())
        self.assertEqual(len(builder.TARGETS), 10)

    def test_pins_are_exact_official_source_revisions(self):
        pins = builder.read_pins()
        self.assertEqual(pins['v8']['revision'], 'e422f6ef0c7b877b04e4872fd0bd3a1cc2ec2eee')
        self.assertEqual(pins['v8']['version'], '15.4.80.24')
        self.assertEqual(len(pins['depotTools']['revision']), 40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pins.json'
            pins['v8']['version'] = '16.1.2.3'
            pins['v8']['revision'] = 'a' * 40
            path.write_text(json.dumps(pins))
            self.assertEqual(builder.read_pins(path)['v8']['version'], '16.1.2.3')
            for value in ['16.01.2', '16.1.2.0', 'latest', None]:
                pins['v8']['version'] = value
                path.write_text(json.dumps(pins))
                with self.assertRaises(ValueError): builder.read_pins(path)
            pins['v8']['version'] = '16.1.2'
            pins['v8']['repository'] = 'https://chromium.googlesource.com/other.git'
            path.write_text(json.dumps(pins))
            with self.assertRaisesRegex(ValueError, 'official'): builder.read_pins(path)

    def test_mac_android_sdk_gn_disable_lto_and_keep_pic(self):
        for target in ('macos-arm64', 'macos-x64', 'android-arm64', 'android-x64'):
            args = builder.gn_arguments(target)
            self.assertIn('use_thin_lto = false', args)
            self.assertIn('v8_monolithic_for_shared_library = true', args)
            self.assertIn('v8_use_external_startup_data = false', args)
            self.assertIn('use_custom_libcxx = true', args)
        self.assertIn('target_cpu = "x64"', builder.gn_arguments('macos-x64'))
        self.assertIn('android_ndk_api_level = 26', builder.gn_arguments('android-arm64'))
        with self.assertRaises(ValueError): builder.gn_arguments('unknown')

    def test_macos_native_host_and_android_linux_host_are_strict(self):
        with patch.object(builder.platform, 'system', return_value='Darwin'):
            with patch.object(builder.platform, 'machine', return_value='x86_64'):
                builder.require_host('macos-x64')
            with patch.object(builder.platform, 'machine', return_value='arm64'):
                with self.assertRaises(ValueError): builder.require_host('macos-x64')
                with self.assertRaises(ValueError): builder.require_host('android-arm64')

    def test_native_archive_inspection_rejects_bitcode_and_wrong_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sdk.a'
            def archive(data):
                header = ('sample.o/'.ljust(16) + '0'.ljust(12) + '0'.ljust(6) + '0'.ljust(6) +
                          '100644'.ljust(8) + str(len(data)).ljust(10) + '`\n').encode()
                path.write_bytes(b'!<arch>\n' + header + data + (b'\n' if len(data) % 2 else b''))
            data = bytearray(32); data[:4] = b'\xcf\xfa\xed\xfe'
            struct.pack_into('<I', data, 4, 0x0100000C); struct.pack_into('<I', data, 12, 1)
            archive(data)
            self.assertEqual(builder.inspect_apple_android_archive(path, 'macos-arm64')['objectCount'], 1)
            with self.assertRaisesRegex(ValueError, 'architecture'): builder.inspect_apple_android_archive(path, 'macos-x64')
            archive(b'BC\xc0\xde' + bytes(28))
            with self.assertRaisesRegex(ValueError, 'native'): builder.inspect_apple_android_archive(path, 'macos-arm64')

    def test_source_version_uses_canonical_nonzero_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'include').mkdir()
            header = root / 'include/v8-version.h'
            text = '#define V8_MAJOR_VERSION 15\n#define V8_MINOR_VERSION 7\n#define V8_BUILD_NUMBER 36\n#define V8_PATCH_LEVEL '
            header.write_text(text + '0\n')
            self.assertEqual(builder.source_version(root), '15.7.36')
            header.write_text(text + '1\n')
            self.assertEqual(builder.source_version(root), '15.7.36.1')

    def test_sdk_headers_include_generated_and_reject_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'v8'; out = source / 'out/sdk'
            (source / 'include').mkdir(parents=True)
            (out / 'gen/include').mkdir(parents=True)
            (source / 'include/v8.h').write_text('public')
            (out / 'gen/include/v8-gn.h').write_text('generated features')
            self.assertEqual(set(builder.sdk_headers(source, out)), {'include/v8.h', 'include/v8-gn.h'})
            (out / 'gen/include/v8.h').write_text('conflict')
            with self.assertRaisesRegex(ValueError, 'conflict'): builder.sdk_headers(source, out)

    def test_upstream_license_inventory_does_not_mix_script_gpl(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'v8'; source.mkdir()
            (source / 'third_party/example').mkdir(parents=True)
            (source / 'LICENSE').write_text('V8 BSD')
            (source / 'third_party/example/NOTICE').write_text('dependency')
            output = Path(directory) / 'sdk'
            entries = builder.package_licenses(source, output)
            self.assertEqual({entry['path'] for entry in entries}, {'licenses/LICENSE', 'licenses/third_party/example/NOTICE'})
            self.assertFalse((output / 'licenses/source_v8/LICENSE').exists())
            (source / 'LICENSE').write_text('different')
            with self.assertRaisesRegex(ValueError, 'conflict'): builder.package_licenses(source, output, entries)
            self.assertEqual((output / 'licenses/LICENSE').read_text(), 'V8 BSD')

    def test_published_sdk_is_indexed_and_refuses_source_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; source.mkdir()
            (source / 'third_party').mkdir(); (source / 'LICENSE').write_text('V8 BSD')
            files = {}
            for name, value in {'lib/libv8_monolith.a':'archive', 'include/v8.h':'public',
                                'linking.json':'{}', 'args.gn':'official flags',
                                'lib/libv8_cxx_runtime.a':'runtime'}.items():
                path = root / 'inputs' / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value); files[name] = path
            pins = builder.read_pins()
            output = root / 'artifacts'
            entry = {'binary':'lib/libv8_monolith.a', 'validation': {'built':True, 'runtimeTested':False}}
            destination = builder.publish_sdk(source, 'macos-arm64', pins, files, entry, output)
            manifest = json.loads((destination.parent / 'manifest.json').read_text())
            self.assertNotIn('bridge', manifest)
            target = manifest['targets']['macos-arm64']
            self.assertEqual(target['artifactKind'], 'v8-static-sdk')
            self.assertEqual(target['binary'], 'macos-arm64/lib/libv8_monolith.a')
            self.assertEqual(len(target['targetFiles']), 5)
            for item in target['targetFiles']:
                self.assertEqual(builder.sha(destination.parent / item['path']), item['sha256'])
                self.assertNotIn('\\', item['path'])
            changed = {**pins, 'v8': {**pins['v8'], 'version':'15.4.80.25'}}
            files['lib/libv8_monolith.a'].write_text('changed archive')
            with self.assertRaisesRegex(ValueError, 'provenance'):
                builder.publish_sdk(source, 'macos-arm64', changed, files, entry, output)
            self.assertEqual((destination / 'lib/libv8_monolith.a').read_text(), 'archive')
            for unsafe in ('../escape', '/absolute', 'C:\\bad', 'dir//bad'):
                with self.assertRaises(ValueError):
                    builder.publish_sdk(source, 'macos-arm64', pins, files | {unsafe: files['include/v8.h']}, entry, output)

    def test_sdk_defines_use_public_headers_config_and_runtime_abi_only(self):
        with patch.object(builder, 'gn_property', side_effect=[['V8_COMPRESS_POINTERS', 'V8_ENABLE_SANDBOX'],
                          ['V8_INTERNAL_ONLY', 'NDEBUG', '_LIBCPP_HARDENING_MODE=1', 'CR_CLANG_REVISION=internal']]):
            self.assertEqual(builder.sdk_defines(Path('/v8'), Path('/out'), Path('/depot'), {}),
                             ['V8_COMPRESS_POINTERS', 'V8_ENABLE_SANDBOX', 'NDEBUG', '_LIBCPP_HARDENING_MODE=1'])

    def test_gn_archive_outputs_are_resolved_from_official_metadata(self):
        with patch.object(builder, 'run', return_value=json.dumps({'//:v8_monolith': {'outputs': ['//out/sdk/obj/libv8_monolith.a']}})):
            self.assertEqual(builder.gn_output(Path('/v8'), Path('/out'), Path('/depot'), {}, '//:v8_monolith'),
                             Path('/v8/out/sdk/obj/libv8_monolith.a'))
        with patch.object(builder, 'run', return_value='{}'):
            with self.assertRaises(ValueError): builder.gn_property(Path('/v8'), Path('/out'), Path('/depot'), {}, '//:v8_monolith', 'outputs')

    def test_explicit_pins_cache_and_output_cli_are_threaded_to_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'pins.json'; pins = builder.read_pins()
            path.write_text(json.dumps(pins))
            with patch.object(builder.sys, 'argv', ['build.py', 'build', '--target', 'macos-x64', '--pins-file', str(path),
                                                  '--cache-root', str(root / 'cache'), '--output-root', str(root / 'artifacts'), '--jobs', '2']), \
                 patch.object(builder, 'bootstrap', return_value=(root / 'source', root / 'depot', {})) as bootstrap, \
                 patch.object(builder, 'build') as build:
                builder.main()
                self.assertEqual(bootstrap.call_args.args, ((root / 'cache').resolve(), 'macos-x64', pins))
                self.assertEqual(bootstrap.call_args.kwargs, {'reuse': True})
                self.assertEqual(build.call_args.kwargs, {'output_root': (root / 'artifacts').resolve()})

    def test_mac_profile_includes_vendor_assertion_header_and_actual_frameworks(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory); out = source / 'out/sdk'
            for name in ['third_party/libc++/src/include', 'third_party/libc++abi/src/include', 'buildtools/third_party/libc++']:
                (source / name).mkdir(parents=True)
            vendor = source / 'buildtools/third_party/libc++'
            (vendor / '__config_site').write_text('ABI config')
            (vendor / '__assertion_handler').write_text('vendor assertion handler')
            monolith = source / 'libv8_monolith.a'; monolith.write_text('archive')
            runtime = source / 'libv8_cxx_runtime.a'; runtime.write_text('runtime')
            with patch.object(builder, 'gn_output', side_effect=[monolith, runtime]), \
                 patch.object(builder, 'inspect_apple_android_archive'), \
                 patch.object(builder, 'gn_property', return_value=['Foundation.framework', 'CoreFoundation.framework', 'Security.framework']):
                profile = builder.apple_android_profile(source, out, 'macos-arm64', builder.read_pins(), [], Path('/depot'), {})
            self.assertIn('runtime/include/config/__assertion_handler', profile['runtimeHeaders'])
            self.assertEqual(profile['linking']['linkOptions'][-6:],
                             ['-framework','Foundation','-framework','CoreFoundation','-framework','Security'])

    def test_sdk_abi_options_keep_actual_relative_vtable_flag(self):
        with patch.object(builder, 'gn_property', return_value=['-std=c++20', '-fexperimental-relative-c++-abi-vtables', '--sysroot=/private/cache']):
            self.assertEqual(builder.sdk_abi_options(Path('/v8'), Path('/out'), Path('/depot'), {}),
                             ['-fexperimental-relative-c++-abi-vtables'])

    def test_android_link_smoke_checks_final_elf_and_binds_sdk_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; sdk = root / 'sdk'
            sysroot = source / 'third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot'
            sysroot.mkdir(parents=True)
            sdk.mkdir(); (sdk / 'lib').mkdir()
            monolith = sdk / 'lib/libv8_monolith.a'; monolith.write_bytes(b'static SDK archive')
            contract = {'schemaVersion':1, 'includeDirs':['include'], 'defines':['V8_ENABLE_SANDBOX'],
                        'compileOptions':['-std=c++20', '--target=aarch64-linux-android26'],
                        'libraries':['lib/libv8_monolith.a'], 'linkOptions':['-nostdlib++', '-Wl,-z,max-page-size=16384'],
                        'systemLibraries':['dl', 'm'], 'sysrootRequirement':{'kind':'android-ndk'}}
            (sdk / 'linking.json').write_text(json.dumps(contract))
            def linked_elf(machine=183, alignment=16384):
                data = bytearray(120); data[:6] = b'\x7fELF\x02\x01'
                struct.pack_into('<HH', data, 16, 3, machine)
                struct.pack_into('<Q', data, 32, 64); struct.pack_into('<HH', data, 54, 56, 1)
                struct.pack_into('<I', data, 64, 1); struct.pack_into('<Q', data, 112, alignment)
                return data
            calls = []
            def run(arguments, cwd, env=None, capture=False):
                calls.append(arguments)
                if '--version' in arguments: return 'pinned Chromium clang'
                binary = arguments[arguments.index('-o') + 1]
                binary.write_bytes(linked_elf())
            with patch.object(builder, 'run', side_effect=run):
                report, files = builder.android_link_smoke(source, 'android-arm64', sdk, root / 'clang++', {})
            self.assertTrue(report['passed']); self.assertFalse(report['runtimeExecuted'])
            self.assertEqual(report['binaryInspection']['loadSegmentAlignments'], [16384])
            self.assertEqual(report['monolithSha256'], builder.sha(monolith))
            self.assertEqual(report['linkingSha256'], builder.sha(sdk / 'linking.json'))
            self.assertEqual(len(files), 3)
            self.assertIn('-Wl,--no-undefined', calls[0])
            self.assertIn('--sysroot=' + str(sysroot), calls[0])
            self.assertNotIn('source_v8', (sdk / 'validation/android-sdk-link.cc').read_text())
            binary = sdk / 'validation/libv8_sdk_link_test.so'
            for machine, alignment in [(62,16384), (183,4096)]:
                binary.write_bytes(linked_elf(machine,alignment))
                with self.assertRaises(ValueError): builder.inspect_android_consumer(binary, 'android-arm64')

    def test_ios_delegate_uses_same_publication_contract(self):
        pins = builder.read_pins()
        with patch.object(builder, 'platform_module') as module:
            builder.build(Path('/source'), Path('/depot'), {}, 'ios-arm64', 2, pins, Path('/sdk'))
            module.return_value.build.assert_called_once_with(Path('/source'), Path('/depot'), {}, 'ios-arm64', 2, pins, Path('/sdk'))

    def test_minimal_unix_bootstrap_retains_official_depot_pin(self):
        with patch.object(builder, 'run') as execute:
            builder.initialize_depot(Path('/depot'), {'DEPOT_TOOLS_UPDATE':'0'})
            command = execute.call_args.args[0][2]
            self.assertIn('bootstrap_python3', command)
            self.assertIn('cipd_bin_setup', command)
            self.assertNotIn('gsutil', command)


class BootstrapReuseTests(unittest.TestCase):
    def fixture(self, root):
        pins = builder.read_pins()
        depot = root / 'depot_tools'
        workspace = root / pins['v8']['revision'] / 'macos-arm64'
        source = workspace / 'v8'
        rust = source / 'third_party/rust'
        for path in (depot, source, rust):
            (path / '.git').mkdir(parents=True)
        (source / 'DEPS').write_text('official fixed DEPS')
        (workspace / '.gclient').write_text('official client config')
        state = {'rustDiff': 'intentional official hook change', 'rustRevision': 'a' * 40,
                 'cipd': 'fixed-package', 'origin': pins['v8']['repository'],
                 'sourceDiff': '', 'depotDiff': ''}
        calls = []
        def execute(arguments, cwd, env=None, capture=False):
            calls.append([str(value) for value in arguments])
            cwd = Path(cwd)
            if arguments[:3] == ['git', 'remote', 'get-url']:
                return pins['depotTools']['repository'] if cwd == depot else state['origin']
            if arguments[:2] == ['git', 'rev-parse']:
                return (pins['depotTools']['revision'] if cwd == depot else
                        state['rustRevision'] if cwd == rust else pins['v8']['revision'])
            if arguments[:2] == ['git', 'diff']:
                return state['rustDiff'] if cwd == rust else state['depotDiff'] if cwd == depot else state['sourceDiff']
            if 'revinfo' in arguments:
                return ('v8: ' + pins['v8']['repository'] + '@' + pins['v8']['revision'] + '\n'
                        'v8/third_party/rust: https://official/rust.git@' + 'a' * 40 + '\n'
                        'v8/tools:tools/pkg/${{arch}}: https://cipd/+/package/' + state['cipd'] + '\n')
            return ''
        return pins, source, depot, workspace, state, calls, execute

    def test_post_hook_baseline_is_reused_without_second_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pins, source, depot, workspace, state, calls, execute = self.fixture(root)
            with patch.object(builder, 'require_host'), patch.object(builder, 'initialize_depot'), patch.object(builder, 'run', side_effect=execute):
                builder.bootstrap(root, 'macos-arm64', pins, reuse=True)
                self.assertEqual(sum('sync' in call for call in calls), 1)
                marker = json.loads((workspace / 'bootstrap-state.json').read_text())
                self.assertIn('v8/third_party/rust', marker['trackedDiffSha256'])
                calls.clear()
                self.assertEqual(builder.bootstrap(root, 'macos-arm64', pins, reuse=True)[:2], (source, depot))
                self.assertFalse(any('sync' in call or 'fetch' in call or 'checkout' in call for call in calls))
                state['rustDiff'] += ' user modification'
                with self.assertRaisesRegex(ValueError, 'contents changed'):
                    builder.bootstrap(root, 'macos-arm64', pins, reuse=True)
                self.assertFalse(any('--force' in call or 'reset' in call for call in calls))

    def test_source_deps_cipd_and_client_changes_reject_reuse(self):
        for changed in ('DEPS', 'client', 'cipd', 'sourceDiff', 'depotDiff'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pins, source, depot, workspace, state, calls, execute = self.fixture(root)
                with patch.object(builder, 'require_host'), patch.object(builder, 'initialize_depot'), patch.object(builder, 'run', side_effect=execute):
                    builder.bootstrap(root, 'macos-arm64', pins)
                    if changed == 'DEPS': (source / 'DEPS').write_text('changed DEPS')
                    elif changed == 'client': (workspace / '.gclient').write_text('changed config')
                    elif changed == 'cipd': state['cipd'] = 'changed-package'
                    else: state[changed] = 'modified tracked file'
                    calls.clear()
                    with self.assertRaisesRegex(ValueError, 'contents changed'):
                        builder.bootstrap(root, 'macos-arm64', pins, reuse=True)
                    self.assertFalse(any('sync' in call for call in calls))

    def test_revision_origin_and_missing_dependency_are_rejected(self):
        for changed in ('revision', 'origin', 'missing'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pins, source, depot, workspace, state, calls, execute = self.fixture(root)
                with patch.object(builder, 'run', side_effect=execute):
                    if changed == 'revision': state['rustRevision'] = 'b' * 40
                    elif changed == 'origin': state['origin'] = 'https://unexpected/source.git'
                    else: (source / 'third_party/rust/.git').rmdir()
                    with self.assertRaises(ValueError):
                        builder.bootstrap_snapshot(source, depot, workspace, pins, {})

    def test_marker_pins_and_shape_reject_before_any_checkout_actions(self):
        for value in ([], {'schemaVersion': 1, 'pins': {}}, {'schemaVersion': True}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pins, source, depot, workspace, state, calls, execute = self.fixture(root)
                (workspace / 'bootstrap-state.json').write_text(json.dumps(value))
                with patch.object(builder, 'require_host'), patch.object(builder, 'run') as run:
                    with self.assertRaises(ValueError): builder.bootstrap(root, 'macos-arm64', pins, reuse=True)
                    run.assert_not_called()

    def test_failed_explicit_bootstrap_invalidates_previous_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pins, source, depot, workspace, state, calls, execute = self.fixture(root)
            marker = workspace / 'bootstrap-state.json'; marker.write_text('{}')
            with patch.object(builder, 'require_host'), patch.object(builder, 'run', side_effect=OSError('bootstrap failure')):
                with self.assertRaises(OSError): builder.bootstrap(root, 'macos-arm64', pins)
            self.assertFalse(marker.exists())


if __name__ == '__main__': unittest.main()
