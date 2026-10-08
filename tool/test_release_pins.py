"""Artifact revisions change release identity without changing source pins."""
import copy
import unittest
from release_pins import make_pins, release_tag, release_metadata


class ReleasePinsTests(unittest.TestCase):
    def test_revision_tag_and_source_pin_identity_are_independent(self):
        version = '15.4.80.25'
        base = {'schemaVersion': 1, 'v8': {'version': version, 'revision': 'c' * 40},
                'depotTools': {'revision': 'd' * 40}, 'targets': {'macos-arm64': {'cpu': 'arm64'}}}
        before = copy.deepcopy(base)
        self.assertEqual(release_tag(version), 'v8-' + version)
        self.assertEqual(release_tag(version, 1), 'v8-' + version + '-sdk.1')
        self.assertEqual(make_pins(base, {'version': version, 'revision': 'c' * 40,
            'sdkArtifactRevision': 1, 'tag': release_tag(version, 1)}), before)
        self.assertEqual(base, before)

    def test_invalid_revision_and_conflicting_tag_fail_closed(self):
        for revision in (True, False, -1, 1.0, '1', None):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                release_tag('15.4.80.25', revision)
        for version in ('15.04.80.25', '15.4.80.0', 'latest', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release_tag(version)
        for revision, tag in ((1, 'v8-15.4.80.25'), (0, 'v8-15.4.80.25-sdk.1')):
            with self.assertRaises(ValueError):
                make_pins({'v8': {}}, {'version': '15.4.80.25', 'revision': 'c' * 40,
                    'sdkArtifactRevision': revision, 'tag': tag})

    def test_optional_metadata_is_backward_compatible_and_explicit(self):
        self.assertEqual(release_metadata({}, '15.4.80.25'), {})
        expected = {'sdkArtifactRevision': 1, 'releaseTag': 'v8-15.4.80.25-sdk.1',
                    'replacesTargets': ['macos-arm64', 'macos-x64']}
        self.assertEqual(release_metadata(expected, '15.4.80.25'), expected)
        for change in ({'releaseTag': 'v8-15.4.80.25'}, {'replacesTargets': []},
                       {'replacesTargets': ['android-arm64']}, {'sdkArtifactRevision': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                release_metadata(expected | change, '15.4.80.25')
