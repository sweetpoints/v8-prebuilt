"""Generic zero-build packaging preserves independently authenticated producers."""
import copy
import json
import unittest
from unittest.mock import patch

import release_resume
from release_package import package
from release_pins import make_pins
import test_generic_resume
import test_release


class GenericPackageTests(unittest.TestCase):
    def setUp(self):
        self.graph = test_generic_resume.GenericResumeTests('test_arbitrary_run_all_ten_present_requires_no_native_rebuild')
        self.graph.setUp()
        self.addCleanup(self.graph.doCleanups)
        self.fixture = test_release.ReleaseTests('test_complete_deterministic_packages')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        base = json.loads((self.graph.root / 'tool/v8/pins.json').read_text())
        self.graph.pins = make_pins(base, {'version': self.fixture.pins['v8']['version'],
            'revision': self.fixture.pins['v8']['revision'], 'tag': 'v8-' + self.fixture.pins['v8']['version']})
        self.graph.api = test_generic_resume.FakeGitHub()
        self.graph.api.add_run(self.graph.FIRST, self.graph.producer,
                              list(release_resume.TARGETS), self.graph.pins)
        self.plan = self.graph.plan()
        self.fixture.pins_path.write_text(json.dumps(self.graph.pins))
        for path in self.fixture.inputs.rglob('manifest.json'):
            manifest = json.loads(path.read_text())
            manifest['v8'] = self.graph.pins['v8']
            manifest['depotTools'] = self.graph.pins['depotTools']
            path.write_text(json.dumps(manifest))

    def package(self, plan=None):
        validate = release_resume.validate_generic_plan
        with patch.object(release_resume, 'validate_generic_plan',
                          side_effect=lambda value, pins, sha: validate(value, pins, sha, self.graph.root)):
            return package(self.fixture.inputs, self.fixture.pins_path,
                           self.fixture.root / 'generic-dist', self.graph.builder,
                           self.plan if plan is None else plan)

    def test_zero_build_keeps_original_recipe_and_separate_packaging_identity(self):
        self.assertEqual([], self.plan['rebuildTargets'])
        result = self.package()
        self.assertEqual(self.graph.builder, result['packagingRevision'])
        for target, entry in result['targets'].items():
            self.assertEqual(self.graph.producer, entry['producerRevision'])
            self.assertEqual(self.plan['producerRecipes'][self.graph.producer], entry['producerInputHashes'])
            self.assertEqual(self.graph.FIRST, entry['reuseProvenance']['runId'])
            self.assertEqual('git-blob-sha256', entry['producerInputHashSemantics'])

    def test_relabelled_original_producer_is_rejected_before_output(self):
        plan = copy.deepcopy(self.plan)
        plan['targets']['ios-arm64']['producerRevision'] = self.graph.builder
        with self.assertRaisesRegex(ValueError, 'relabeled'):
            self.package(plan)
        self.assertFalse((self.fixture.root / 'generic-dist').exists())

    def test_indexed_ios_patch_evidence_tampering_rejected(self):
        root = self.fixture.inputs / 'ios-arm64'
        manifest_path = root / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        entry = manifest['targets']['ios-arm64']
        evidence = root / 'ios-arm64/validation/ios-patch.json'
        evidence.write_text('{"fixed":true}')
        import hashlib
        entry['targetFiles'].append({'path': 'ios-arm64/validation/ios-patch.json',
            'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(), 'size': evidence.stat().st_size})
        manifest_path.write_text(json.dumps(manifest))
        evidence.write_text('{"fixed":false}')
        with self.assertRaises(ValueError):
            self.package()

    def test_zero_build_reuses_authenticated_arm_runtime_identity(self):
        sdk = self.plan['sdkAssets']['linux-arm64']
        runtime = dict(sdk, id=991001, sdkArtifactId=sdk['id'], sdkArtifactSha256=sdk['sha256'])
        self.plan['runtimeAssets']['linux-arm-runtime'] = runtime
        self.plan['runtimeRequired'] = [t for t in self.plan['runtimeRequired'] if t != 'linux-arm64']
        result = self.package()
        identity = result['targets']['linux-arm64']['runtimeReuseProvenance']
        self.assertEqual(runtime['id'], identity['id'])
        self.assertEqual(sdk['id'], identity['sdkArtifactId'])
        self.assertEqual(self.graph.producer, identity['producerRevision'])
