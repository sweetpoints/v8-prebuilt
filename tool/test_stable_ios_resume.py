"""Reviewed eight-target reuse plus exactly two new iOS producers; no native build."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import zipfile
import release_resume as resume


class StableIosResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = copy.deepcopy(resume.REVIEWED_STABLE)
        self.old = {name: 'a' * 64 for name in resume.INPUTS}
        self.new = dict(self.old, **{'tool/v8/build.py': 'b' * 64, 'tool/v8/ios.py': 'c' * 64})
        self.lock['producerInputHashes'] = self.old
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('stable-pins.json', json.dumps(self.lock['pins']))
        self.pins_zip = data.getvalue()
        self.lock['assets']['stable-pins'].update(size=len(self.pins_zip), sha256=hashlib.sha256(self.pins_zip).hexdigest())
        path = self.root / 'tool/v8/pins.json'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.lock['pins']))
        self.run = {'head_sha': self.lock['producerRevision'], 'status': 'completed', 'event': 'schedule', 'run_attempt': 1}
        self.jobs = [{'name': 'build (' + t + ', fixture)', 'conclusion': 'failure' if t in self.lock['rebuildTargets'] else 'success'} for t in resume.TARGETS]
        self.metadata = {a['id']: dict(a, digest='sha256:' + a['sha256'], size_in_bytes=a['size'], expired=False,
                          workflow_run={'id': self.lock['runId'], 'head_sha': self.lock['producerRevision']}) for a in self.lock['assets'].values()}
        self.api = type('Api', (), {'base': 'https://api.github.com/repos/sweetpoints/v8-prebuilt'})()
        self.api.request = self.request

    def request(self, url, **kwargs):
        if '/jobs?' in url: return {'jobs': self.jobs}
        if '/actions/runs/' in url: return self.run
        if url.endswith('/zip'): return self.pins_zip
        return self.metadata[int(url.rsplit('/', 1)[1])]

    def contexts(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(resume, 'REVIEWED_STABLE', self.lock))
        stack.enter_context(patch.object(resume, 'snapshot_inputs', return_value=self.old))
        stack.enter_context(patch.object(resume, 'current_recipe', return_value=self.new))
        return stack

    def plan(self):
        return resume.stable_plan(self.api, self.root, 'd' * 40, self.lock['rebuildTargets'])

    def test_exact_two_ios_builds_keep_eight_actual_old_producers_and_both_recipe_hashes(self):
        with self.contexts():
            value = self.plan()
            resume.validate_stable_plan(value, value['pins'], 'd' * 40, self.root)
        self.assertEqual(8, sum('reuseProvenance' in origin for origin in value['targets'].values()))
        for target, origin in value['targets'].items():
            if target.startswith('ios-'):
                self.assertEqual(origin, {'producerRevision': 'd' * 40, 'producerInputHashes': self.new})
            else:
                self.assertEqual(origin['producerRevision'], self.lock['producerRevision'])
                self.assertEqual(origin['reuseProvenance']['producerInputHashes'], self.old)

    def test_missing_duplicate_or_successful_target_rebuild_request_is_rejected(self):
        with self.contexts():
            for targets in [['ios-arm64'], ['ios-arm64'] * 2, list(resume.TARGETS), ['windows-arm64'], list(reversed(self.lock['rebuildTargets']))]:
                with self.assertRaisesRegex(ValueError, 'exactly'):
                    resume.stable_plan(self.api, self.root, 'd' * 40, targets)

    def test_changed_artifact_or_source_run_outcome_is_rejected(self):
        with self.contexts():
            self.run['run_attempt'] = 2
            with self.assertRaisesRegex(ValueError, 'source run identity'): self.plan()
            self.run['run_attempt'] = 1
            self.jobs[0]['conclusion'] = 'failure'
            with self.assertRaisesRegex(ValueError, 'target outcome'): self.plan()
            self.jobs[0]['conclusion'] = 'success'
            item = self.metadata[self.lock['assets']['v8-linux-x64']['id']]
            item['digest'] = 'sha256:' + '0' * 64
            with self.assertRaisesRegex(ValueError, 'artifact identity'): self.plan()

    def test_historical_recipe_and_native_probe_source_must_remain_authentic(self):
        with self.contexts(), patch.object(resume, 'snapshot_inputs', return_value=dict(self.old, **{'tool/v8/desktop.py': 'e' * 64})):
            with self.assertRaisesRegex(ValueError, 'Historical producer'): self.plan()
        self.new['tool/sdk_smoke.py'] = 'f' * 64
        with self.contexts():
            with self.assertRaisesRegex(ValueError, 'original SDK consumer'): self.plan()

    def test_packaging_rejects_relabeling_reused_sdk_or_modified_new_ios_recipe(self):
        with self.contexts():
            value = self.plan()
            value['targets']['linux-x64']['producerRevision'] = 'd' * 40
            with self.assertRaisesRegex(ValueError, 'Per-target'):
                resume.validate_stable_plan(value, value['pins'], 'd' * 40, self.root)
            value = self.plan(); value['rebuiltProducerInputHashes']['tool/v8/ios.py'] = '0' * 64
            with patch.object(resume, 'current_recipe', return_value=dict(self.new, **{'tool/v8/ios.py': 'c' * 64})):
                with self.assertRaisesRegex(ValueError, 'source recipes'):
                    resume.validate_stable_plan(value, value['pins'], 'd' * 40, self.root)

    def test_workflow_uses_verified_plan_to_build_only_ios_and_executes_reused_arm_probes(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/release.yml').read_text()
        body = workflow.split('      - name: Select only missing targets on resume', 1)[1]
        script = textwrap.dedent(body.split("python - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        with self.contexts(): value = self.plan()
        (self.root / 'reuse-plan.json').write_text(json.dumps(value))
        output = self.root / 'output'; previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {'REUSE_RUN_ID': str(resume.STABLE_RUN_ID), 'REBUILD_TARGETS': ','.join(self.lock['rebuildTargets']), 'GITHUB_OUTPUT': str(output)}):
                exec(compile(script, 'workflow-matrix', 'exec'), {})
        finally: os.chdir(previous)
        entries = json.loads(output.read_text().split('=', 1)[1])['include']
        self.assertEqual(self.lock['rebuildTargets'], [e['target'] for e in entries])
        windows = workflow.split('  windows-arm-runtime:', 1)[1].split('  publish:', 1)[0]
        self.assertIn('--only-target windows-arm64', windows)
        self.assertIn('--execute-only', windows)
        self.assertNotIn('tool/v8/build.py', windows)
