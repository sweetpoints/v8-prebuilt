"""Generic zero-build packaging preserves independently authenticated producers."""
import copy
import json
import unittest
from unittest.mock import patch

import release_resume
from release_package import package
from release_pins import make_pins, release_tag
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

    def revision_plan(self):
        return release_resume.generic_plan(self.graph.api, self.graph.root,
            self.graph.builder, self.graph.FIRST, sdk_artifact_revision=1,
            replaces_targets=['macos-arm64', 'macos-x64'])

    def isolate_mac_malloc(self):
        for target in ('macos-arm64', 'macos-x64'):
            path = self.fixture.inputs / target / target / 'args.gn'
            path.write_text(path.read_text() + 'use_allocator_shim = false\nuse_partition_alloc_as_malloc = false\n')
            self.fixture.reindex_file(target, path)

    def test_sdk_revision_changes_release_identity_but_preserves_source_and_other_producers(self):
        self.isolate_mac_malloc()
        result = self.package(self.revision_plan())
        tag = release_tag(self.graph.pins['v8']['version'], 1)
        self.assertEqual(result['releaseTag'], tag)
        self.assertEqual(result['sdkArtifactRevision'], 1)
        self.assertEqual(result['replacesTargets'], ['macos-arm64', 'macos-x64'])
        self.assertEqual(result['v8'], self.graph.pins['v8'])
        self.assertEqual(set(result['targets']), set(release_resume.TARGETS))
        self.assertEqual(json.loads((self.fixture.root / 'generic-dist/pins.json').read_text()), self.graph.pins)
        for target, entry in result['targets'].items():
            self.assertEqual(entry['producerRevision'], self.graph.builder if target.startswith('macos-') else self.graph.producer)
        self.assertEqual({a['name'] for a in result['assets']},
            {f"v8-{result['v8']['version']}-{target}.tar.gz" for target in release_resume.TARGETS} | {'pins.json'})
        gh = test_release.FakeGitHub()
        from release_publish import publish
        self.assertEqual(publish(self.fixture.root / 'generic-dist', 'example/test', '', gh), tag)
        self.assertEqual(gh.release['tag_name'], tag)

    def test_sdk_revision_rejects_missing_true_commented_or_duplicate_mac_allocator_flags(self):
        self.isolate_mac_malloc()
        target = 'macos-arm64'
        path = self.fixture.inputs / target / target / 'args.gn'
        original = path.read_text()
        for text in (original.replace('use_allocator_shim = false\n', ''),
                     original.replace('use_partition_alloc_as_malloc = false', 'use_partition_alloc_as_malloc = true'),
                     original.replace('use_allocator_shim = false', '# use_allocator_shim = false'),
                     original + 'use_allocator_shim = false\n'):
            with self.subTest(args=text):
                path.write_text(text); self.fixture.reindex_file(target, path)
                with self.assertRaisesRegex(ValueError, 'GN feature configuration'):
                    self.package(self.revision_plan())
                self.assertFalse((self.fixture.root / 'generic-dist').exists())

    def test_sdk_revision_metadata_cannot_relabel_original_tag_or_invalid_revision(self):
        plan = self.revision_plan()
        for change in ({'sdkArtifactRevision': True}, {'sdkArtifactRevision': -1},
                       {'releaseTag': release_tag(self.graph.pins['v8']['version'])}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.package(plan | change)
        self.assertFalse((self.fixture.root / 'generic-dist').exists())

    def test_sdk_revision_rejects_reduced_mac_features_even_with_full_manifest_profile(self):
        self.isolate_mac_malloc()
        target = 'macos-x64'
        path = self.fixture.inputs / target / target / 'args.gn'
        original = path.read_text()
        for flag, value in (('v8_jitless', 'true'), ('v8_enable_webassembly', 'false'),
                            ('v8_enable_sandbox', 'false'), ('v8_enable_pointer_compression', 'false'),
                            ('use_partition_alloc', 'false')):
            with self.subTest(flag=flag):
                path.write_text(original + f'{flag} = {value}\n'); self.fixture.reindex_file(target, path)
                with self.assertRaisesRegex(ValueError, 'GN feature configuration'):
                    self.package(self.revision_plan())
                self.assertFalse((self.fixture.root / 'generic-dist').exists())

    def test_publisher_keeps_original_tag_immutable_and_refuses_revision_asset_conflicts(self):
        self.isolate_mac_malloc()
        result = self.package(self.revision_plan())
        directory = self.fixture.root / 'generic-dist'
        from release_publish import publish
        gh = test_release.FakeGitHub()
        gh.release = {'draft': False, 'tag_name': release_tag(result['v8']['version']),
            'target_commitish': self.graph.producer, 'assets': [], 'prerelease': False}
        with self.assertRaises(ValueError):
            publish(directory, 'example/test', '', gh)
        self.assertEqual([], gh.mutations)
        gh = test_release.FakeGitHub()
        publish(directory, 'example/test', '', gh)
        prior = list(gh.mutations)
        gh.payloads['pins.json'] = b'conflicting original asset'
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            publish(directory, 'example/test', '', gh)
        self.assertEqual(gh.mutations, prior)
        manifest_path = directory / 'release-manifest.json'
        result['releaseTag'] = release_tag(result['v8']['version'])
        manifest_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'release tag'):
            publish(directory, 'example/test', '', gh)
        self.assertEqual(gh.mutations, prior)
