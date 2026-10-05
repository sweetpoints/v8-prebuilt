import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from release_package import TARGETS, package
from sdk_smoke import PROBE
from release_publish import publish

class FakeGitHub:
    base = 'https://api.github.com/repos/example/test'
    def __init__(self):
        self.release = None
        self.payloads = {}
        self.mutations = []
    def by_tag(self, tag):
        return copy.deepcopy(self.release)
    def tag_revision(self, tag):
        return self.release.get('target_commitish') if self.release and not self.release['draft'] else None
    def asset_bytes(self, asset):
        return self.payloads[asset['name']]
    def request(self, url, method='GET', body=None, content_type=None):
        if method == 'POST' and url.endswith('/releases'):
            self.release = dict(body, id=1, assets=[], upload_url='https://uploads.example/assets{?name}')
        elif method == 'POST':
            name = url.split('?name=')[1]
            self.payloads[name] = body
            self.release['assets'].append({'name': name, 'url': url})
        elif method == 'PATCH':
            self.release.update(body)
        elif method != 'GET':
            raise AssertionError(method)
        if method != 'GET':
            self.mutations.append((method, url))
        return copy.deepcopy(self.release)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'
        self.pins = {'schemaVersion': 1, 'v8': {'version': '15.4.80.24', 'revision': 'a' * 40, 'repository': 'official'}, 'depotTools': {'revision': 'b' * 40}}
        self.pins_path = self.root / 'pins.json'
        self.pins_path.write_text(json.dumps(self.pins))
        for target in TARGETS:
            root = self.inputs / target
            out = root / target / 'include'
            out.mkdir(parents=True)
            (out / 'v8.h').write_text('header')
            binary = root / target / 'lib' / ('v8_monolith.lib' if target.startswith('windows-') else 'libv8_monolith.a')
            binary.parent.mkdir()
            binary.write_bytes(b'!<arch>\n' + target.encode())
            license = root / 'licenses' / 'LICENSE'
            license.parent.mkdir()
            license.write_text('official license')
            manifest = {key: self.pins[key] for key in ('schemaVersion', 'v8', 'depotTools')}
            manifest.update(targets={target: {'binary': binary.relative_to(root).as_posix(), 'size': binary.stat().st_size, 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}}, licenses=[{'path': 'licenses/LICENSE', 'sha256': hashlib.sha256(license.read_bytes()).hexdigest()}])
            linking_path = root / target / 'linking.json'
            linking_path.write_text(json.dumps({'schemaVersion': 1, 'libraries': [binary.relative_to(root / target).as_posix()], 'defines': [], 'compileOptions': [], 'linkOptions': [], 'systemLibraries': []}))
            if target.startswith(('macos-', 'linux-', 'windows-')):
                probe = root / target / 'validation' / ('sdk-probe.exe' if target.startswith('windows-') else 'sdk-probe')
                probe.parent.mkdir(); probe.write_bytes(b'compiled-official-api-consumer')
                sysroot = ('/official/debian_bullseye_' + ('arm64' if target == 'linux-arm64' else 'amd64') + '-sysroot') if target.startswith('linux-') else None
                command = ['official-compiler', 'consumer.cpp'] + (['--sysroot=' + sysroot] if sysroot else [])
                os_name = {'macos': 'Darwin', 'linux': 'Linux', 'windows': 'Windows'}[target.split('-')[0]]
                (root / target / 'sdk-smoke.json').write_text(json.dumps({'schemaVersion': 1, 'status': 'passed', 'version': self.pins['v8']['version'], 'librarySha256': manifest['targets'][target]['sha256'], 'linkingSha256': hashlib.sha256(linking_path.read_bytes()).hexdigest(), 'libraries': {binary.relative_to(root / target).as_posix(): hashlib.sha256(binary.read_bytes()).hexdigest()}, 'probe': probe.relative_to(root / target).as_posix(), 'probeSha256': hashlib.sha256(probe.read_bytes()).hexdigest(), 'probeSourceSha256': hashlib.sha256(PROBE.encode()).hexdigest(), 'compiler': 'fixed official clang fixture', 'compileCommand': command, 'sysroot': sysroot, 'host': {'os': os_name, 'machine': 'arm64' if target.endswith('arm64') else 'x86_64'}, 'scope': 'official V8 API SDK consumer', 'nativeConsumerExecuted': True, 'cases': ['official_api_compile', 'official_api_link', 'official_api_execute']}))
            if target == 'linux-arm64':
                path = root / target / 'sdk-smoke.json'
                report = json.loads(path.read_text())
                report['compileHost'] = {'os': 'Linux', 'machine': 'x86_64'}
                path.write_text(json.dumps(report))
                prior = dict(report, status='compiled', nativeConsumerExecuted=False, cases=['official_api_compile', 'official_api_link'], host=report['compileHost'])
                (root / target / 'validation/sdk-probe.json').write_text(json.dumps(prior))
            entry = manifest['targets'][target]
            entry['artifactKind'] = 'v8-static-sdk'
            entry['validation'] = {'built': True, 'runtimeTested': False}
            entry['targetFiles'] = [{'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in (root / target).rglob('*') if p.is_file() and p.name != 'sdk-smoke.json']
            if target.startswith('android-'):
                entry['validation']['linkTested'] = True
                entry['linkSmoke'] = {'passed': True, 'runtimeExecuted': False, 'binaryInspection': {'elfMachine': 183 if target == 'android-arm64' else 62, 'loadSegmentAlignments': [16384]}}
            if target.startswith('ios-'):
                platform = 2 if target == 'ios-arm64' else 7
                entry['validation']['linkTested'] = True
                entry['binaryInspection'] = {'format': 'static-ar', 'architecture': 'arm64', 'platform': platform, 'objectCount': 1}
                entry['linkSmoke'] = {'passed': True, 'runtimeExecuted': False, 'executableInspection': {'platform': platform}}
            (root / 'manifest.json').write_text(json.dumps(manifest))
    def package(self, name='dist'):
        return package(self.inputs, self.pins_path, self.root / name, 'd' * 40)
    def test_complete_deterministic_packages(self):
        self.package()
        self.package('dist2')
        self.assertEqual(13, len(list((self.root / 'dist').iterdir())))
        for path in (self.root / 'dist').iterdir():
            self.assertEqual(path.read_bytes(), (self.root / 'dist2' / path.name).read_bytes())
    def test_incomplete_rejected(self):
        (self.inputs / TARGETS[0] / 'manifest.json').unlink()
        with self.assertRaisesRegex(ValueError, 'ten-target'):
            self.package()
        self.assertFalse((self.root / 'dist').exists())
    def test_corrupt_binary_rejected(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'lib/libv8_monolith.a').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_mixed_provenance_rejected(self):
        path = self.inputs / TARGETS[0] / 'manifest.json'
        m = json.loads(path.read_text()); m['depotTools']['revision'] = 'e' * 40
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, 'provenance differs'):
            self.package()
    def test_header_required(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'include' / 'v8.h').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            self.package()
    def change_manifest(self, target, change):
        path = self.inputs / target / 'manifest.json'
        manifest = json.loads(path.read_text())
        change(manifest['targets'][target])
        path.write_text(json.dumps(manifest))
    def test_header_tamper_rejected_by_inventory(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'include' / 'v8.h').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_unindexed_payload_rejected(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'extra').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'unindexed'):
            self.package()
    def test_wrong_runtime_binary_rejected(self):
        target = 'linux-x64'
        path = self.inputs / target / target / 'sdk-smoke.json'
        report = json.loads(path.read_text()); report['librarySha256'] = '0' * 64
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'runtime smoke'):
            self.package()
    def test_missing_runtime_report_rejected(self):
        target = 'linux-arm64'
        (self.inputs / target / target / 'sdk-smoke.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            self.package()
    def test_android_bad_alignment_rejected(self):
        self.change_manifest('android-x64', lambda e: e['linkSmoke']['binaryInspection'].update(loadSegmentAlignments=[4096]))
        with self.assertRaisesRegex(ValueError, '16KiB'):
            self.package()
    def test_ios_device_simulator_mismatch_rejected(self):
        self.change_manifest('ios-arm64', lambda e: e['linkSmoke']['executableInspection'].update(platform=7))
        with self.assertRaisesRegex(ValueError, 'iOS static'):
            self.package()
    def test_metadata_digest_tamper_rejected(self):
        target = 'android-arm64'; root = self.inputs / target
        path = root / target / 'defines.json'; path.write_text('[]')
        self.change_manifest(target, lambda e: (e['targetFiles'].append({'path': target + '/defines.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}), e.update(definesSha256='0' * 64)))
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_published_tag_revision_conflict_rejected(self):
        self.package(); gh = FakeGitHub()
        publish(self.root / 'dist', 'example/test', '', gh)
        gh.release['target_commitish'] = '0' * 40
        with self.assertRaisesRegex(ValueError, 'tag builder revision'):
            publish(self.root / 'dist', 'example/test', '', gh)
    def test_draft_prerelease_conflict_rejected(self):
        self.package(); gh = FakeGitHub()
        gh.release = {'id': 1, 'draft': True, 'prerelease': True, 'tag_name': 'v8-15.4.80.24', 'target_commitish': 'd' * 40, 'assets': []}
        with self.assertRaisesRegex(ValueError, 'identity'):
            publish(self.root / 'dist', 'example/test', '', gh)
        self.assertEqual([], gh.mutations)
    def test_cross_compile_proof_required(self):
        path = self.inputs / 'linux-arm64/linux-arm64/validation/sdk-probe.json'
        path.unlink()
        self.change_manifest('linux-arm64', lambda e: e.update(targetFiles=[v for v in e['targetFiles'] if v['path'] != 'linux-arm64/validation/sdk-probe.json']))
        with self.assertRaisesRegex(ValueError, 'compile proof required'):
            self.package()
    def test_native_probe_hash_mismatch_rejected(self):
        path = self.inputs / 'linux-x64/linux-x64/sdk-smoke.json'
        report = json.loads(path.read_text()); report['probeSha256'] = '0' * 64
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_linking_proof_hash_mismatch_rejected(self):
        path = self.inputs / 'linux-x64/linux-x64/sdk-smoke.json'
        report = json.loads(path.read_text()); report['linkingSha256'] = '0' * 64
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'linking contract evidence'):
            self.package()
    def test_thin_archive_is_not_self_contained_sdk(self):
        target = 'android-arm64'
        path = self.inputs / target / target / 'lib/libv8_monolith.a'
        path.write_bytes(b'!<thin>\nexternal-object')
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        def change(entry):
            entry.update(sha256=checksum, size=path.stat().st_size)
            for item in entry['targetFiles']:
                if item['path'] == target + '/lib/libv8_monolith.a':
                    item.update(sha256=checksum, size=path.stat().st_size)
        self.change_manifest(target, change)
        with self.assertRaisesRegex(ValueError, 'self-contained'):
            self.package()
    def test_different_consumer_source_proof_rejected(self):
        path = self.inputs / 'linux-x64/linux-x64/sdk-smoke.json'
        report = json.loads(path.read_text()); report['probeSourceSha256'] = '0' * 64
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'compile evidence'):
            self.package()
    def test_project_bridge_manifest_rejected(self):
        path = self.inputs / TARGETS[0] / 'manifest.json'
        manifest = json.loads(path.read_text()); manifest['bridge'] = {'abi': 1}
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'project bridge'):
            self.package()
    def test_wrong_artifact_kind_rejected(self):
        self.change_manifest('linux-x64', lambda e: e.update(artifactKind='shared-bridge'))
        with self.assertRaisesRegex(ValueError, 'pure V8 static SDK'):
            self.package()
    def test_missing_v8_link_contract_rejected(self):
        target = 'android-arm64'
        (self.inputs / target / target / 'linking.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            self.package()
    def test_publish_idempotent_no_overwrite(self):
        self.package(); gh = FakeGitHub()
        self.assertEqual('v8-15.4.80.24', publish(self.root / 'dist', 'example/test', '', gh))
        self.assertFalse(gh.release['draft'])
        mutations = len(gh.mutations)
        publish(self.root / 'dist', 'example/test', '', gh)
        self.assertEqual(mutations, len(gh.mutations))
        gh.payloads['pins.json'] = b'conflict'
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            publish(self.root / 'dist', 'example/test', '', gh)
        self.assertEqual(mutations, len(gh.mutations))
    def test_resume_draft_only_missing_assets(self):
        self.package(); gh = FakeGitHub()
        gh.release = {'id': 1, 'draft': True, 'tag_name': 'v8-15.4.80.24', 'target_commitish': 'd' * 40, 'prerelease': False, 'assets': [], 'upload_url': 'https://uploads.example/assets{?name}'}
        publish(self.root / 'dist', 'example/test', '', gh)
        self.assertFalse(gh.release['draft'])
    def test_published_incomplete_refuses_mutation(self):
        self.package(); gh = FakeGitHub()
        gh.release = {'draft': False, 'assets': [], 'tag_name': 'v8-15.4.80.24', 'target_commitish': 'd' * 40, 'prerelease': False}
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            publish(self.root / 'dist', 'example/test', '', gh)
        self.assertEqual([], gh.mutations)

if __name__ == '__main__':
    unittest.main()
