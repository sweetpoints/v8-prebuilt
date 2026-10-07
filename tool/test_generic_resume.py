"""Independent immutable artifact-graph recovery tests; no native builds."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import zipfile

import release_resume as resume
from release_pins import make_pins


def archive(name, value):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr(name, json.dumps(value))
    return stream.getvalue()


class FakeGitHub:
    base = 'https://api.github.com/repos/sweetpoints/v8-prebuilt'

    def __init__(self):
        self.runs = {}
        self.jobs = {}
        self.artifacts = {}
        self.artifact_runs = {}
        self.payloads = {}
        self.requests = []

    def request(self, url, **kwargs):
        self.requests.append(url)
        suffix = url.removeprefix(self.base).split('?', 1)[0]
        if not suffix:
            return {'name': 'v8-prebuilt', 'full_name': 'sweetpoints/v8-prebuilt', 'default_branch': 'main'}
        if suffix == '/actions/workflows/release.yml':
            return {'id': 734, 'path': '.github/workflows/release.yml', 'state': 'active'}
        if suffix == '/actions/workflows/release.yml/runs':
            return {'workflow_runs': copy.deepcopy(list(self.runs.values()))}
        if suffix.startswith('/actions/runs/'):
            parts = suffix.split('/')
            identifier = int(parts[3])
            if len(parts) == 4:
                return copy.deepcopy(self.runs[identifier])
            if parts[4] == 'jobs':
                return {'jobs': copy.deepcopy(self.jobs[identifier])}
            if parts[4] == 'artifacts':
                return {'artifacts': copy.deepcopy([item for item in self.artifacts.values()
                    if self.artifact_runs[item['id']] == identifier])}
        if suffix.startswith('/actions/artifacts/'):
            identifier = int(suffix.split('/')[3])
            if suffix.endswith('/zip'):
                return self.payloads[identifier]
            return copy.deepcopy(self.artifacts[identifier])
        raise AssertionError('Unexpected API request: ' + url)

    def add_run(self, identifier, producer, targets, pins, event='schedule'):
        self.runs[identifier] = {'id': identifier, 'workflow_id': 734,
            'path': '.github/workflows/release.yml', 'head_sha': producer,
            'head_branch': 'main', 'status': 'completed', 'conclusion': 'failure',
            'run_attempt': 1, 'event': event,
            'repository': {'full_name': 'sweetpoints/v8-prebuilt'},
            'head_repository': {'full_name': 'sweetpoints/v8-prebuilt'}, 'pull_requests': []}
        self.jobs[identifier] = [{'name': 'detect', 'conclusion': 'success', 'status': 'completed'}] + [{'name': 'build (' + target + ', runner, 4, x64)',
            'conclusion': 'success', 'status': 'completed'} for target in targets]
        self.add_artifact(identifier, producer, 'stable-pins', archive('stable-pins.json', pins))
        for target in targets:
            self.add_artifact(identifier, producer, 'v8-' + target,
                              archive('manifest.json', {'v8': pins['v8'], 'depotTools': pins['depotTools'],
                                  'targets': {target: {'targetConfig': pins['targets'][target]}}}))

    def add_artifact(self, run_id, producer, name, data):
        identifier = max(self.artifacts, default=8000) + 1
        self.artifact_runs[identifier] = run_id
        self.artifacts[identifier] = {'id': identifier, 'name': name,
            'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
            'size_in_bytes': len(data), 'expired': False,
            'workflow_run': {'id': run_id, 'head_sha': producer, 'head_branch': 'main'}}
        self.payloads[identifier] = data
        return identifier

    def named(self, run_id, name):
        return next(item for item in self.artifacts.values()
                    if item['name'] == name and item['workflow_run']['id'] == run_id)


class GenericResumeTests(unittest.TestCase):
    FIRST = 99123456701
    SECOND = 99123456888

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'core.autocrlf', 'false')
        base = json.loads((Path(__file__).parent / 'v8/pins.json').read_text())
        for name in resume.INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(base) if name.endswith('pins.json') else '# immutable recipe\n')
        self.producer = self.commit('original recipe')
        (self.root / 'unrelated').write_text('second producer\n')
        self.second_producer = self.commit('partial run producer')
        (self.root / 'unrelated').write_text('packaging\n')
        self.builder = self.commit('packaging')
        self.pins = make_pins(base, {'version': '15.5.35.21',
            'revision': '60868fa2040a823150a00ce60a75fc6e0d5ef2f7', 'tag': 'v8-15.5.35.21'})
        self.api = FakeGitHub()
        self.api.add_run(self.FIRST, self.producer, list(resume.TARGETS), self.pins)
        self.api.add_artifact(self.FIRST, self.producer, 'smoke-probe-linux-arm64',
                              archive('sdk-probe.json', {'fixtureProbe': True}))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def plan(self, run_id=None, targets=None):
        return resume.generic_plan(self.api, self.root, self.builder,
                                   run_id or self.FIRST, targets)

    def test_arbitrary_run_all_ten_present_requires_no_native_rebuild(self):
        value = self.plan()
        self.assertEqual([], value['rebuildTargets'])
        self.assertEqual(set(resume.TARGETS), set(value['targets']))
        for target, origin in value['targets'].items():
            self.assertEqual(self.producer, origin['producerRevision'])
            self.assertEqual(self.FIRST, origin['reuseProvenance']['runId'])
        self.assertTrue(any('/zip' in url for url in self.api.requests))

    def test_schedule_and_manual_official_main_runs_supported(self):
        self.api.runs[self.FIRST]['event'] = 'workflow_dispatch'
        self.assertEqual([], self.plan()['rebuildTargets'])

    def test_missing_targets_are_only_rebuilds(self):
        missing = {'ios-arm64', 'ios-simulator-arm64'}
        for target in missing:
            item = self.api.named(self.FIRST, 'v8-' + target)
            del self.api.artifacts[item['id']]
        self.api.jobs[self.FIRST] = [job for job in self.api.jobs[self.FIRST]
            if not any(job['name'].startswith('build (' + target + ',') for target in missing)]
        value = self.plan()
        self.assertEqual(missing, set(value['rebuildTargets']))
        self.assertEqual(self.builder, value['targets']['ios-arm64']['producerRevision'])
        self.assertNotIn('reuseProvenance', value['targets']['ios-arm64'])

    def test_wrong_repository_branch_event_or_workflow_rejected(self):
        bad = [('repository', {'full_name': 'attacker/v8-prebuilt'}),
               ('head_repository', {'full_name': 'attacker/v8-prebuilt'}),
               ('head_branch', 'feature'), ('event', 'pull_request'),
               ('path', '.github/workflows/untrusted.yml'), ('workflow_id', 999)]
        original = copy.deepcopy(self.api.runs[self.FIRST])
        for field, value in bad:
            with self.subTest(field=field):
                self.api.runs[self.FIRST] = dict(original, **{field: value})
                with self.assertRaises(ValueError):
                    self.plan()
        self.api.runs[self.FIRST] = original

    def test_run_head_must_be_known_ancestor_of_packaging_commit(self):
        self.api.runs[self.FIRST]['head_sha'] = 'f' * 40
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.plan()

    def test_known_nonancestor_run_head_is_rejected(self):
        orphan = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}'), '-m', 'untrusted orphan')
        self.api.runs[self.FIRST]['head_sha'] = orphan
        with self.assertRaises(ValueError):
            self.plan()

    def test_incomplete_or_pull_request_associated_runs_rejected(self):
        original = copy.deepcopy(self.api.runs[self.FIRST])
        for fields in [{'status': 'in_progress'}, {'pull_requests': [{'number': 9}]}]:
            self.api.runs[self.FIRST] = dict(original, **fields)
            with self.assertRaises(ValueError):
                self.plan()
        self.api.runs[self.FIRST] = original

    def test_invalid_digest_or_size_artifact_rejected(self):
        item = self.api.named(self.FIRST, 'v8-linux-x64')
        for field, value in [('digest', 'sha256:invalid'),
                             ('size_in_bytes', -1)]:
            original = item[field]
            with self.subTest(field=field):
                item[field] = value
                with self.assertRaises(ValueError):
                    self.plan()
            item[field] = original

    def test_manual_expired_single_sdk_keeps_remaining_nine(self):
        self.api.named(self.FIRST, 'v8-linux-x64')['expired'] = True
        value = self.plan()
        self.assertEqual(['linux-x64'], value['rebuildTargets'])
        self.assertEqual(9, len(value['sdkAssets']))
        self.assertNotIn('linux-x64', value['sdkAssets'])

    def test_artifact_origin_cannot_claim_another_run_or_commit(self):
        item = self.api.named(self.FIRST, 'v8-linux-x64')
        for field, value in [('id', self.SECOND), ('head_sha', self.builder)]:
            original = item['workflow_run'][field]
            with self.subTest(field=field):
                item['workflow_run'][field] = value
                with self.assertRaises(ValueError):
                    self.plan()
            item['workflow_run'][field] = original

    def test_pins_zip_is_verified_against_actual_download_bytes(self):
        identifier = self.api.named(self.FIRST, 'stable-pins')['id']
        self.api.payloads[identifier] += b'tamper'
        with self.assertRaises(ValueError):
            self.plan()

    def test_frozen_source_pins_cannot_change_target_configuration(self):
        altered = copy.deepcopy(self.pins)
        altered['targets']['android-arm64']['minApi'] += 1
        old = self.api.named(self.FIRST, 'stable-pins')['id']
        del self.api.artifacts[old]
        self.api.add_artifact(self.FIRST, self.producer, 'stable-pins',
                              archive('stable-pins.json', altered))
        with self.assertRaises(ValueError):
            self.plan()

    def test_sdk_uploaded_after_failed_target_is_not_success_proof(self):
        next(job for job in self.api.jobs[self.FIRST] if job['name'].startswith('build ('))['conclusion'] = 'failure'
        with self.assertRaises(ValueError):
            self.plan()

    def test_duplicate_sdk_name_is_ambiguous_and_rejected(self):
        self.api.add_artifact(self.FIRST, self.producer, 'v8-linux-x64', b'duplicate')
        with self.assertRaises(ValueError):
            self.plan()


    def test_download_checks_archive_bytes_before_sdk_extraction(self):
        value = self.plan()
        item = self.api.named(self.FIRST, 'v8-linux-x64')
        self.api.payloads[item['id']] += b'changed after planning'
        with self.assertRaises(ValueError):
            resume.download_generic(self.api, value, self.root / 'downloads', only_target='linux-x64', root=self.root)

    def test_download_rechecks_metadata_and_rejects_replaced_artifact(self):
        value = self.plan()
        item = self.api.named(self.FIRST, 'v8-linux-x64')
        item['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(ValueError):
            resume.download_generic(self.api, value, self.root / 'downloads', only_target='linux-x64', root=self.root)

    def test_all_sdk_recovery_requires_only_missing_runtime_proofs(self):
        value = self.plan()
        self.assertEqual([], value['rebuildTargets'])
        self.assertEqual(['linux-arm64', 'windows-arm64'], value['runtimeRequired'])
        self.assertEqual({}, value['runtimeAssets'])

    def test_validated_plan_cannot_relabel_old_sdk_producer(self):
        value = self.plan()
        resume.validate_generic_plan(value, self.pins, self.builder, root=self.root)
        value['targets']['linux-x64']['producerRevision'] = self.builder
        with self.assertRaises(ValueError):
            resume.validate_generic_plan(value, self.pins, self.builder, root=self.root)

    def test_auto_selection_prefers_complete_graph_over_recent_partial_run(self):
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64'], self.pins)
        value = resume.auto_plan(self.api, self.root, self.builder, self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertEqual(self.FIRST, value['sourceRunId'])
        self.assertEqual([], value['rebuildTargets'])

    def test_auto_selection_excludes_pr_runs_even_with_complete_artifacts(self):
        self.api.runs[self.FIRST]['event'] = 'pull_request'
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64'], self.pins)
        value = resume.auto_plan(self.api, self.root, self.builder, self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertEqual(self.SECOND, value['sourceRunId'])
        self.assertEqual(9, len(value['rebuildTargets']))

    def test_actual_matrix_script_handles_ten_two_and_zero_missing_sdks(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/release.yml').read_text()
        body = workflow.split('      - name: Select only genuinely missing SDK targets', 1)[1]
        script = textwrap.dedent(body.split("python - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        original = Path.cwd()
        try:
            os.chdir(self.root)
            for missing in [list(resume.TARGETS), ['ios-arm64', 'ios-simulator-arm64'], []]:
                with self.subTest(missing=missing):
                    sdk = {target: {} for target in resume.TARGETS if target not in missing}
                    (self.root / 'reuse-plan.json').write_text(json.dumps({'rebuildTargets': missing, 'sdkAssets': sdk}))
                    output = self.root / 'matrix-output'
                    output.unlink(missing_ok=True)
                    with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                        exec(compile(script, 'actual-workflow-matrix', 'exec'), {})
                    values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                    matrix = json.loads(values['value'])['include']
                    self.assertEqual(set(missing), {item['target'] for item in matrix})
                    self.assertEqual(str(len(missing)), values['build_count'])
                    self.assertEqual(str(10 - len(missing)), values['reuse_count'])
                    self.assertEqual(str('windows-arm64' in sdk).lower(), values['windows_sdk_reused'])
                    self.assertEqual(str('linux-arm64' in sdk).lower(), values['linux_sdk_reused'])
        finally:
            os.chdir(original)

    def test_workflow_zero_sdk_build_recovery_still_publishes_after_runtime(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/release.yml').read_text()
        build = workflow.split('  build:', 1)[1].split('  linux-arm-runtime:', 1)[0]
        self.assertIn("build_count != '0'", build)
        publish = workflow.split('  publish:', 1)[1]
        self.assertIn("needs.build.result == 'skipped'", publish)
        self.assertIn("needs.windows-arm-runtime.result == 'success'", publish)
        self.assertIn('--runtime-name windows-arm-runtime', publish)
        self.assertIn('--runtime-name linux-arm-runtime', publish)
        self.assertNotIn('publish-only:', workflow)
        for platform in ['linux', 'windows']:
            block = workflow.split('  ' + platform + '-arm-runtime:', 1)[1].split('  publish:', 1)[0]
            if platform == 'linux':
                block = block.split('  windows-arm-runtime:', 1)[0]
            self.assertIn('--execute-only', block)
            self.assertNotIn('tool/v8/build.py', block)
            self.assertNotIn('--compiler', block)
            self.assertIn(platform + "_runtime_required == 'true'", block)

    def test_all_ten_sdk_and_passed_runtime_recovery_needs_only_publish(self):
        for name in ['linux-arm-runtime', 'windows-arm-runtime']:
            self.api.jobs[self.FIRST].append({'name': name, 'conclusion': 'success', 'status': 'completed'})
            self.api.add_artifact(self.FIRST, self.producer, name,
                                  archive('sdk-smoke.json', {'fixtureRuntime': True}))
        value = self.plan()
        self.assertEqual([], value['rebuildTargets'])
        self.assertEqual([], value['runtimeRequired'])
        self.assertEqual({'linux-arm-runtime', 'windows-arm-runtime'}, set(value['runtimeAssets']))
        for name, target in [('linux-arm-runtime', 'linux-arm64'), ('windows-arm-runtime', 'windows-arm64')]:
            self.assertEqual(value['sdkAssets'][target]['id'], value['runtimeAssets'][name]['sdkArtifactId'])
            self.assertEqual(value['sdkAssets'][target]['sha256'], value['runtimeAssets'][name]['sdkArtifactSha256'])

    def test_uploaded_runtime_cannot_claim_failed_execution_success(self):
        self.api.jobs[self.FIRST].append({'name': 'windows-arm-runtime', 'conclusion': 'failure'})
        self.api.add_artifact(self.FIRST, self.producer, 'windows-arm-runtime',
                              archive('sdk-smoke.json', {'fixtureRuntime': True}))
        with self.assertRaises(ValueError):
            self.plan()

    def test_fresh_build_saved_plan_with_no_parent_can_resume_partial_successes(self):
        self.api = FakeGitHub()
        fresh = resume.auto_plan(self.api, self.root, self.second_producer,
                                 self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertIsNone(fresh['sourceRunId'])
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64'], self.pins)
        self.api.add_artifact(self.SECOND, self.second_producer, 'reuse-plan',
                              archive('reuse-plan.json', fresh))
        value = self.plan(self.SECOND)
        self.assertEqual(9, len(value['rebuildTargets']))
        self.assertEqual(self.second_producer, value['targets']['ios-arm64']['producerRevision'])

    def test_auto_selection_excludes_current_run_and_expired_artifacts(self):
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64'], self.pins)
        value = resume.auto_plan(self.api, self.root, self.builder,
                                 self.pins['v8']['version'], self.pins['v8']['revision'], current_run_id=self.FIRST)
        self.assertEqual(self.SECOND, value['sourceRunId'])
        self.api.named(self.FIRST, 'v8-linux-x64')['expired'] = True
        value = resume.auto_plan(self.api, self.root, self.builder,
                                 self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertEqual(self.FIRST, value['sourceRunId'])
        self.assertEqual(['linux-x64'], value['rebuildTargets'])

    def test_runtime_archive_download_verifies_bytes_and_metadata(self):
        name = 'windows-arm-runtime'
        self.api.jobs[self.FIRST].append({'name': name, 'conclusion': 'success'})
        self.api.add_artifact(self.FIRST, self.producer, name,
                              archive('sdk-smoke.json', {'fixtureRuntime': True}))
        value = self.plan()
        item = self.api.named(self.FIRST, name)
        self.api.payloads[item['id']] += b'tamper'
        with self.assertRaises(ValueError):
            resume.download_generic(self.api, value, self.root / 'runtime',
                                    runtime_name=name, root=self.root)

    def test_cli_auto_completed_release_skips_github_and_plan_creation(self):
        detection = self.root / 'detection.json'
        output = self.root / 'github-output'
        plan = self.root / 'no-plan.json'
        detection.write_text(json.dumps({'should_build': False}))
        argv = ['release_resume.py', 'auto', '--repository', 'sweetpoints/v8-prebuilt',
                '--detection', str(detection), '--current-run-id', str(self.SECOND),
                '--builder-revision', self.builder, '--github-output', str(output),
                '--plan', str(plan)]
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('sys.argv', argv), patch.object(resume, 'GitHub') as client, \
                    patch.object(resume, 'auto_plan') as auto, patch.dict(os.environ, {}, clear=True):
                resume.main()
                client.assert_not_called()
                client.return_value.request.assert_not_called()
                auto.assert_not_called()
            self.assertEqual('should_build=false\n', output.read_text())
            self.assertFalse(plan.exists())
            self.assertFalse((self.root / 'stable-pins.json').exists())
        finally:
            os.chdir(original)

    def test_expired_single_sdk_retains_other_nine_successful_artifacts(self):
        self.api.named(self.FIRST, 'v8-linux-x64')['expired'] = True
        value = resume.auto_plan(self.api, self.root, self.builder,
                                 self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertEqual(self.FIRST, value['sourceRunId'])
        self.assertEqual(['linux-x64'], value['rebuildTargets'])
        self.assertEqual(9, len(value['sdkAssets']))
        for target in value['sdkAssets']:
            self.assertEqual(self.producer, value['targets'][target]['producerRevision'])

    def test_auto_metadata_plan_does_not_download_sdks_and_download_tamper_fails_closed(self):
        item = self.api.named(self.FIRST, 'v8-linux-x64')
        self.api.payloads[item['id']] += b'actual downloaded bytes differ'
        value = resume.auto_plan(self.api, self.root, self.builder,
                                 self.pins['v8']['version'], self.pins['v8']['revision'])
        self.assertEqual([], value['rebuildTargets'])
        sdk_zip_urls = {self.api.base + '/actions/artifacts/' + str(record['id']) + '/zip'
                        for record in value['sdkAssets'].values()}
        self.assertTrue(sdk_zip_urls.isdisjoint(self.api.requests))
        with self.assertRaisesRegex(ValueError, 'digest|SHA|size|integrity'):
            resume.download_generic(self.api, value, self.root / 'bad-sdk',
                                    only_target='linux-x64', root=self.root)
        self.assertEqual([], value['rebuildTargets'])

    def test_auto_pins_zip_digest_tamper_cannot_fallback_to_another_build(self):
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64'], self.pins)
        item = self.api.named(self.FIRST, 'stable-pins')
        self.api.payloads[item['id']] += b'actual metadata bytes differ'
        with self.assertRaisesRegex(ValueError, 'digest|SHA|size|integrity'):
            resume.auto_plan(self.api, self.root, self.builder,
                             self.pins['v8']['version'], self.pins['v8']['revision'])

    def mixed_plan(self):
        ancestor = resume.generic_plan(self.api, self.root, self.second_producer, self.FIRST)
        self.api.add_run(self.SECOND, self.second_producer, ['ios-arm64', 'ios-simulator-arm64'],
                         self.pins, event='workflow_dispatch')
        self.api.jobs[self.SECOND].append({'name': 'reuse', 'conclusion': 'success', 'status': 'completed'})
        self.api.add_artifact(self.SECOND, self.second_producer, 'reuse-plan',
                              archive('reuse-plan.json', ancestor))
        return self.plan(self.SECOND)

    def test_mixed_partial_run_recursively_retains_real_original_producers(self):
        value = self.mixed_plan()
        self.assertEqual([], value['rebuildTargets'])
        self.assertEqual(self.producer, value['targets']['linux-x64']['producerRevision'])
        self.assertEqual(self.FIRST, value['targets']['linux-x64']['reuseProvenance']['runId'])
        self.assertEqual(self.second_producer, value['targets']['ios-arm64']['producerRevision'])
        self.assertEqual(self.SECOND, value['targets']['ios-arm64']['reuseProvenance']['runId'])
        self.assertEqual({self.producer, self.second_producer}, set(value['producerRecipes']))

    def test_mixed_graph_inherits_runtime_success_without_reexecution(self):
        for name in ['linux-arm-runtime', 'windows-arm-runtime']:
            self.api.jobs[self.FIRST].append({'name': name, 'conclusion': 'success'})
            self.api.add_artifact(self.FIRST, self.producer, name,
                                  archive('sdk-smoke.json', {'fixtureRuntime': True}))
        value = self.mixed_plan()
        self.assertEqual([], value['runtimeRequired'])
        self.assertEqual([], value['rebuildTargets'])
        for proof in value['runtimeAssets'].values():
            self.assertEqual(self.FIRST, proof['runId'])

    def test_rebuilt_sdk_cannot_inherit_an_old_runtime_success(self):
        name = 'windows-arm-runtime'
        self.api.jobs[self.FIRST].append({'name': name, 'conclusion': 'success'})
        self.api.add_artifact(self.FIRST, self.producer, name,
                              archive('sdk-smoke.json', {'fixtureRuntime': True}))
        self.mixed_plan()
        self.api.jobs[self.SECOND].append({'name': 'build (windows-arm64, runner, 4, x64)',
                                          'conclusion': 'success'})
        self.api.add_artifact(self.SECOND, self.second_producer, 'v8-windows-arm64',
                              archive('manifest.json', {'fixtureTarget': 'windows-arm64'}))
        value = self.plan(self.SECOND)
        self.assertIn('windows-arm64', value['runtimeRequired'])
        self.assertNotIn(name, value['runtimeAssets'])

    def test_recursive_reuse_plan_zip_integrity_is_checked(self):
        self.mixed_plan()
        item = self.api.named(self.SECOND, 'reuse-plan')
        self.api.payloads[item['id']] += b'tamper'
        with self.assertRaises(ValueError):
            self.plan(self.SECOND)

    def test_recursive_plan_cannot_relabel_ancestor_sdk_as_new_producer(self):
        self.mixed_plan()
        item = self.api.named(self.SECOND, 'reuse-plan')
        data = self.api.payloads[item['id']]
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            value = json.loads(bundle.read('reuse-plan.json'))
        value['targets']['linux-x64']['producerRevision'] = self.second_producer
        del self.api.artifacts[item['id']]
        self.api.add_artifact(self.SECOND, self.second_producer, 'reuse-plan',
                              archive('reuse-plan.json', value))
        with self.assertRaises(ValueError):
            self.plan(self.SECOND)

    def test_recursive_source_pins_mismatch_is_rejected(self):
        self.mixed_plan()
        old = self.api.named(self.SECOND, 'stable-pins')
        value = copy.deepcopy(self.pins)
        value['v8']['revision'] = 'a' * 40
        del self.api.artifacts[old['id']]
        self.api.add_artifact(self.SECOND, self.second_producer, 'stable-pins',
                              archive('stable-pins.json', value))
        with self.assertRaises(ValueError):
            self.plan(self.SECOND)

    def test_reuse_graph_cycle_is_rejected(self):
        self.api.add_run(self.SECOND, self.second_producer, [], self.pins)
        for child, ancestor, producer in [(self.FIRST, self.SECOND, self.producer),
                                         (self.SECOND, self.FIRST, self.second_producer)]:
            value = {'schemaVersion': 1, 'reuseRunId': ancestor, 'pins': self.pins, 'packagingRevision': producer,
                     'targets': {}, 'producerInputHashes': {}}
            self.api.add_artifact(child, producer, 'reuse-plan', archive('reuse-plan.json', value))
            self.api.jobs[child].append({'name': 'reuse', 'conclusion': 'success', 'status': 'completed'})
        with self.assertRaises(ValueError):
            self.plan()


if __name__ == '__main__':
    unittest.main()
