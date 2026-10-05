import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('detect_stable', Path(__file__).parents[1] / 'tool/detect_stable.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
REV = 'a' * 40
VERSION = '15.4.80.24'


def header(candidate=0, major=15):
    text = '\n'.join(f'#define V8_{name} {value}' for name, value in zip(
        ('MAJOR_VERSION', 'MINOR_VERSION', 'BUILD_NUMBER', 'PATCH_LEVEL', 'IS_CANDIDATE_VERSION'),
        (major, 4, 80, 24, candidate)))
    return base64.b64encode(text.encode())


def stable(channel='Stable', milestone=154):
    return [{'channel': channel, 'platform': 'Linux', 'milestone': milestone,
             'version': f'{milestone}.0.8037.97'}]


def release(draft=False):
    names = [f'v8-{VERSION}-{target}.tar.gz' for target in d.TARGETS]
    names += ['release-manifest.json', 'pins.json', 'SHA256SUMS']
    return {'tag_name': f'v8-{VERSION}', 'draft': draft, 'prerelease': False,
            'assets': [{'name': name} for name in names]}


def manifest():
    return {'schemaVersion': 1, 'v8': {'version': VERSION, 'revision': REV,
            'repository': d.V8_REPOSITORY}, 'targets': {
                target: {'artifactKind': 'v8-static-sdk'} for target in d.TARGETS}}


class StableDetectorTest(unittest.TestCase):
    def test_cross_origin_redirect_strips_token_and_insecure_redirect_denied(self):
        request = d.urllib.request.Request('https://api.github.com/asset',
                                         headers={'Authorization': 'Bearer secret'})
        handler = d.SafeRedirect()
        redirected = handler.redirect_request(request, None, 302, 'Found', {},
                                               'https://release-assets.githubusercontent.com/file')
        self.assertIsNone(redirected.get_header('Authorization'))
        same = handler.redirect_request(request, None, 302, 'Found', {}, 'https://api.github.com/other')
        self.assertEqual(same.get_header('Authorization'), 'Bearer secret')
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'http://api.github.com/other')

    def test_actual_stable_not_maximum_milestone(self):
        self.assertEqual(d.stable_release(stable())['milestone'], 154)
        self.assertEqual(d.milestone_branch([{'milestone': 154, 'v8_branch': '15.4'}], 154), '15.4')

    def test_beta_canary_wrong_platform_and_ambiguous_rejected(self):
        for value in (stable('Beta'), stable('Canary'), [], stable() * 2):
            with self.assertRaises(ValueError):
                d.stable_release(value)
        value = stable()
        value[0]['platform'] = 'Mac'
        with self.assertRaises(ValueError):
            d.stable_release(value)

    def test_milestone_mapping_must_match(self):
        for value in ([{'milestone': 154, 'v8_branch': '15.5'}],
                      [{'milestone': 155, 'v8_branch': '15.4'}], []):
            with self.assertRaises(ValueError):
                d.milestone_branch(value, 154)

    def test_full_revision_ref_only(self):
        with patch.object(d.subprocess, 'run') as run:
            run.return_value.stdout = f'{REV}\trefs/branch-heads/15.4\n'
            self.assertEqual(d.branch_revision(d.V8_REPOSITORY, '15.4'), REV)
            self.assertEqual(run.call_args.args[0][-1], 'refs/branch-heads/15.4')
            for text in (f'{REV}\trefs/tags/15.4.80.24', 'abc\trefs/branch-heads/15.4', '',
                         f'{REV}\trefs/branch-heads/15.4\n{REV}\trefs/branch-heads/15.4'):
                run.return_value.stdout = text
                with self.assertRaises(ValueError):
                    d.branch_revision(d.V8_REPOSITORY, '15.4')

    def test_pinned_header_candidate_mismatch_duplicate_rejected(self):
        self.assertEqual(d.header_version(header(), '15.4'), VERSION)
        zero_patch = base64.b64encode(base64.b64decode(header()).replace(
            b'V8_PATCH_LEVEL 24', b'V8_PATCH_LEVEL 0'))
        self.assertEqual(d.header_version(zero_patch, '15.4'), '15.4.80')
        for data in (header(candidate=1), header(major=16),
                     base64.b64encode(base64.b64decode(header()) + b'\n#define V8_PATCH_LEVEL 25'),
                     b'bad base64'):
            with self.assertRaises(ValueError):
                d.header_version(data, '15.4')

    def test_new_and_draft_build_matching_published_skip(self):
        self.assertEqual(d.release_decision(None, None, VERSION, REV), (True, 'new'))
        self.assertEqual(d.release_decision(release(True), None, VERSION, REV), (True, 'draft'))
        self.assertEqual(d.release_decision(release(), manifest(), VERSION, REV), (False, 'published'))

    def test_tag_revision_collision_rejects_even_draft(self):
        value = manifest()
        value['v8']['revision'] = 'b' * 40
        for draft in (False, True):
            with self.assertRaises(ValueError):
                d.release_decision(release(draft), value, VERSION, REV)

    def test_old_bridge_or_non_sdk_release_rejected_even_draft(self):
        bridged = manifest()
        bridged['bridge'] = {'abi': 1}
        shared = manifest()
        shared['targets']['macos-arm64']['artifactKind'] = 'shared-bridge'
        absent_kind = manifest()
        absent_kind['targets']['linux-x64'] = {}
        for value in (bridged, shared, absent_kind):
            for draft in (False, True):
                with self.assertRaisesRegex(ValueError, 'pure V8 SDK'):
                    d.release_decision(release(draft), value, VERSION, REV)

    def test_published_incomplete_manifest_or_assets_rejected(self):
        m = manifest()
        del m['targets']['windows-arm64']
        r = release()
        r['assets'].pop()
        for rel, man in ((release(), None), (release(), m), (r, manifest())):
            with self.assertRaises(ValueError):
                d.release_decision(rel, man, VERSION, REV)

    def test_prerelease_tag_collision_rejected(self):
        value = release()
        value['prerelease'] = True
        with self.assertRaises(ValueError):
            d.release_decision(value, manifest(), VERSION, REV)

    def test_github_not_found_new_but_permission_error_propagates(self):
        for code in (404, 403, 500):
            with patch.object(d, 'read_json', side_effect=urllib.error.HTTPError('u', code, 'error', {}, None)):
                if code == 404:
                    self.assertEqual(d.published_release('owner/repo', 'v8-' + VERSION, None), (None, None))
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        d.published_release('owner/repo', 'v8-' + VERSION, None)

    def test_end_to_end_pins_immutable_header_and_race_rejects(self):
        rows = [stable(), [{'milestone': 154, 'v8_branch': '15.4'}], stable()]
        with patch.object(d, 'read_json', side_effect=rows), patch.object(d, 'branch_revision', return_value=REV), \
                patch.object(d, 'fetch', return_value=header()) as fetch, \
                patch.object(d, 'published_release', return_value=(None, None)):
            result = d.detect('owner/repo')
            self.assertEqual(result['revision'], REV)
            self.assertTrue(result['should_build'])
            self.assertIn('/+/' + REV + '/include/v8-version.h', fetch.call_args.args[0])
        rows[-1] = stable(milestone=155)
        with patch.object(d, 'read_json', side_effect=rows), patch.object(d, 'branch_revision', return_value=REV), \
                patch.object(d, 'fetch', return_value=header()):
            with self.assertRaises(ValueError):
                d.detect('owner/repo')

    def test_outputs_only_after_success_and_valid_actions_boolean(self):
        result = {'version': VERSION, 'revision': REV, 'tag': 'v8-' + VERSION, 'should_build': False}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'stable.json'
            actions = Path(folder) / 'actions'
            args = ['--repository', 'owner/repo', '--output', str(output), '--github-output', str(actions)]
            with patch.object(d, 'detect', return_value=result), patch.object(d.sys.stdout, 'write'):
                self.assertEqual(d.main(args), 0)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertIn('should_build=false\n', actions.read_text())
            prior = actions.read_text()
            with patch.object(d, 'detect', side_effect=ValueError('failure')), patch.object(d.sys.stderr, 'write'):
                self.assertEqual(d.main(args), 1)
            self.assertEqual(actions.read_text(), prior)


if __name__ == '__main__':
    unittest.main()
