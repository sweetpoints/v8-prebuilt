import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from release_package import EXPORTS, TARGETS, package
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
            (out / 'source_v8.h').write_text('header')
            binary = root / target / 'library'
            binary.write_bytes(target.encode())
            license = root / 'licenses' / 'LICENSE'
            license.parent.mkdir()
            license.write_text('official license')
            manifest = {key: self.pins[key] for key in ('schemaVersion', 'v8', 'depotTools')}
            manifest.update(bridge={'abi': 1, 'sourceSha256': 'c' * 64}, targets={target: {'binary': f'{target}/library', 'size': binary.stat().st_size, 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}}, licenses=[{'path': 'licenses/LICENSE', 'sha256': hashlib.sha256(license.read_bytes()).hexdigest()}])
            if target.startswith(('macos-', 'linux-', 'windows-')):
                os_name = {'macos': 'Darwin', 'linux': 'Linux', 'windows': 'Windows'}[target.split('-')[0]]
                (root / target / 'runtime-smoke.json').write_text(json.dumps({'schemaVersion': 1, 'status': 'passed', 'version': self.pins['v8']['version'], 'librarySha256': manifest['targets'][target]['sha256'], 'host': {'os': os_name, 'machine': 'arm64' if target.endswith('arm64') else 'x86_64'}, 'cases': ['arithmetic', 'promise', 'unicode', 'exception', 'timeout', 'cancel_before_start']}))
            entry = manifest['targets'][target]
            entry['validation'] = {'built': True, 'runtimeTested': False}
            entry['targetFiles'] = [{'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in (root / target).rglob('*') if p.is_file() and p.name != 'runtime-smoke.json']
            if target.startswith('android-'):
                entry['binaryInspection'] = {'elfMachine': 183 if target == 'android-arm64' else 62, 'loadSegmentAlignments': [16384], 'exports': sorted(EXPORTS)}
            if target.startswith('ios-'):
                platform = 2 if target == 'ios-arm64' else 7
                entry['validation']['linkTested'] = True
                entry['binaryInspection'] = {'format': 'static-ar', 'architecture': 'arm64', 'platform': platform, 'objectCount': 1, 'bridgeExports': sorted(EXPORTS)}
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
        (self.inputs / TARGETS[0] / TARGETS[0] / 'library').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_mixed_provenance_rejected(self):
        path = self.inputs / TARGETS[0] / 'manifest.json'
        m = json.loads(path.read_text()); m['bridge']['sourceSha256'] = 'e' * 64
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, 'provenance differs'):
            self.package()
    def test_header_required(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'include' / 'source_v8.h').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            self.package()
    def change_manifest(self, target, change):
        path = self.inputs / target / 'manifest.json'
        manifest = json.loads(path.read_text())
        change(manifest['targets'][target])
        path.write_text(json.dumps(manifest))
    def test_header_tamper_rejected_by_inventory(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'include' / 'source_v8.h').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            self.package()
    def test_unindexed_payload_rejected(self):
        (self.inputs / TARGETS[0] / TARGETS[0] / 'extra').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'unindexed'):
            self.package()
    def test_wrong_runtime_binary_rejected(self):
        target = 'linux-x64'
        path = self.inputs / target / target / 'runtime-smoke.json'
        report = json.loads(path.read_text()); report['librarySha256'] = '0' * 64
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'runtime smoke'):
            self.package()
    def test_missing_runtime_report_rejected(self):
        target = 'linux-arm64'
        (self.inputs / target / target / 'runtime-smoke.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            self.package()
    def test_android_bad_alignment_rejected(self):
        self.change_manifest('android-x64', lambda e: e['binaryInspection'].update(loadSegmentAlignments=[4096]))
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
