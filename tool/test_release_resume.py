import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile
import os
import textwrap
import warnings
import release_resume as resume
from release_pins import make_pins
from release_publish import GitHub

class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        base=json.loads((Path(__file__).parent/'v8/pins.json').read_text())
        (self.root/'tool/v8').mkdir(parents=True);(self.root/'tool/v8/pins.json').write_text(json.dumps(base))
        self.pins=make_pins(base,{'version':'15.4.80.25','revision':'c45871fec706a6e7b715e607065bb4578b23ce9f','tag':'v8-15.4.80.25'})
        self.hashes={name:'a'*64 for name in resume.INPUTS}
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:bundle.writestr('stable-pins.json',json.dumps(self.pins))
        self.zip=stream.getvalue();self.lock=copy.deepcopy(resume.LOCK)
        self.lock['stable-pins']=(resume.LOCK['stable-pins'][0],hashlib.sha256(self.zip).hexdigest(),len(self.zip))
        self.jobs=[{'name':'build ('+target+', runner, 4, x64)','conclusion':'success'} for target in resume.TARGETS if target!='windows-arm64']
        self.run={'head_sha':resume.PRODUCER,'status':'completed','run_attempt':1,'conclusion':'cancelled'}
        self.publish_run={'head_sha':resume.ARM_PRODUCER,'status':'completed','run_attempt':1,'conclusion':'failure'}
        self.publish_jobs=[{'name':name,'conclusion':'success'} for name in ('detect','reuse','windows-arm-runtime','linux-arm-runtime','build (windows-arm64, windows-2025, 4, x64)')]
        self.items={}
        for name,(identifier,sha,size) in self.lock.items():
            self.items[identifier]={'id':identifier,'name':name,'digest':'sha256:'+sha,'size_in_bytes':size,'expired':False,'workflow_run':{'id':resume.RUN_ID,'head_sha':resume.PRODUCER}}
        for name,(identifier,sha,size) in resume.PUBLISH_LOCK.items():
            self.items[identifier]={'id':identifier,'name':name,'digest':'sha256:'+sha,'size_in_bytes':size,'expired':False,'workflow_run':{'id':resume.PUBLISH_RUN,'head_sha':resume.ARM_PRODUCER}}
        class API:
            base='https://api.github.com/repos/example/sdk'
            def request(inner,url,content_type=None,accept=None):
                if url.endswith('/zip'):return self.zip
                if f'/actions/runs/{resume.PUBLISH_RUN}' in url:
                    return {'jobs':self.publish_jobs} if '/jobs?' in url else self.publish_run
                if '/jobs?' in url:return {'jobs':self.jobs}
                if '/actions/runs/' in url:return self.run
                return self.items[int(url.rsplit('/',1)[1])]
        self.api=API()
    def plan(self):
        with patch.object(resume,'LOCK',self.lock),patch.object(resume,'producer_inputs',return_value=self.hashes):
            return resume.plan(self.api,self.root,'b'*40)
    def test_cancelled_run_reuses_nine_successes_but_never_cancelled_arm(self):
        value=self.plan()
        self.assertEqual({'producerRevision':'b'*40},value['targets']['windows-arm64'])
        self.assertEqual(9,sum('reuseProvenance' in value for value in value['targets'].values()))
        self.assertEqual(resume.PRODUCER,value['targets']['linux-x64']['producerRevision'])
    def test_failed_target_rejected_even_if_sdk_exists(self):
        self.jobs[0]['conclusion']='failure'
        with self.assertRaisesRegex(ValueError,'did not succeed'):self.plan()
    def test_other_producer_commit_rejected(self):
        self.run['head_sha']='c'*40
        with self.assertRaisesRegex(ValueError,'run identity'):self.plan()
    def test_expired_or_replaced_artifact_rejected(self):
        identifier=self.lock['v8-linux-x64'][0];self.items[identifier]['expired']=True
        with self.assertRaisesRegex(ValueError,'artifact identity'):self.plan()
        self.items[identifier]['expired']=False;self.items[identifier]['digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'artifact identity'):self.plan()
    def test_zip_actual_digest_not_only_api_metadata(self):
        self.zip+=b'corrupt'
        with self.assertRaisesRegex(ValueError,'ZIP digest/size'):self.plan()
    def test_actions_zip_requests_json_accept_but_preserves_binary_response(self):
        response=Mock();response.read.return_value=self.zip;response.headers={'Content-Type':'application/zip'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        item={'id':1,'size':len(self.zip),'sha256':hashlib.sha256(self.zip).hexdigest()}
        with patch('urllib.request.build_opener',return_value=opener):
            actual=resume.archive_bytes(GitHub('example/sdk','fixture-token'),item)
        self.assertEqual(self.zip,actual)
        request=opener.open.call_args.args[0]
        self.assertEqual('application/vnd.github+json',request.get_header('Accept'))
        self.assertEqual('application/octet-stream',request.get_header('Content-type'))
    def test_target_config_changed_rejected(self):
        path=self.root/'tool/v8/pins.json';base=json.loads(path.read_text());base['targets']['android-arm64']['minApi']=27;path.write_text(json.dumps(base))
        with self.assertRaisesRegex(ValueError,'target configurations'):self.plan()
    def test_full_producer_file_change_is_rejected_without_ast_exceptions(self):
        for name in resume.INPUTS:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'old source')
        with patch('release_resume.subprocess.run',return_value=Mock(stdout=b'old source')):
            self.assertEqual(len(resume.INPUTS),len(resume.producer_inputs(self.root)))
            (self.root/resume.INPUTS[1]).write_bytes(b'changed preflight or recipe')
            with self.assertRaisesRegex(ValueError,'input changed'):resume.producer_inputs(self.root)
    def test_reuse_plan_does_not_claim_packaging_commit_built_old_targets(self):
        value=self.plan()
        with patch.object(resume,'LOCK',self.lock),patch.object(resume,'producer_inputs',return_value=self.hashes):
            resume.validate_plan(value,self.pins,'b'*40)
            value['targets']['linux-x64']['producerRevision']='b'*40
            with self.assertRaisesRegex(ValueError,'producer provenance'):resume.validate_plan(value,self.pins,'b'*40)
    def test_zip_traversal_and_duplicate_entries_rejected(self):
        for names in [['../escape'],['safe','safe']]:
            stream=io.BytesIO()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with zipfile.ZipFile(stream,'w') as bundle:
                    for name in names:bundle.writestr(name,b'data')
            with self.assertRaisesRegex(ValueError,'Unsafe'):
                resume.extract(stream.getvalue(),self.root/'out')
    def test_sdk_resume_download_is_digest_bound(self):
        value=self.plan();identifier=self.lock['v8-linux-x64'][0]
        with patch.object(resume,'LOCK',self.lock):
            with self.assertRaisesRegex(ValueError,'ZIP digest/size'):
                resume.download(self.api,value,self.root/'sdk',only_target='linux-x64')
    def test_actual_workflow_matrix_resume_compiles_only_windows_arm_on_x64(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/release.yml').read_text()
        body=workflow.split('      - name: Select only missing targets on resume',1)[1]
        script=textwrap.dedent(body.split("python - <<'PYTHON'\n",1)[1].split('          PYTHON',1)[0])
        for reuse,expected in [('',10),(str(resume.RUN_ID),1)]:
            output=self.root/('output-'+str(expected))
            with patch.dict(os.environ,{'REUSE_RUN_ID':reuse,'GITHUB_OUTPUT':str(output)}):
                exec(compile(script,'workflow-matrix','exec'),{})
            matrix=json.loads(output.read_text().split('=',1)[1])['include']
            self.assertEqual(expected,len(matrix))
            arm=[item for item in matrix if item['target']=='windows-arm64']
            self.assertEqual([{'target':'windows-arm64','runner':'windows-2025','jobs':4,'python_arch':'x64'}],arm)
    def test_arm_runtime_job_only_executes_and_uses_native_python(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/release.yml').read_text()
        windows=workflow.split('  windows-arm-runtime:',1)[1].split('  publish:',1)[0]
        self.assertIn('architecture: arm64',windows)
        self.assertIn('--execute-only',windows)
        self.assertNotIn('tool/v8/build.py',windows)
        self.assertNotIn('--compiler',windows)
        linux=workflow.split('  linux-arm-runtime:',1)[1].split('  windows-arm-runtime:',1)[0]
        self.assertIn('--execute-only',linux)
        self.assertNotIn('tool/v8/build.py',linux)
    def test_recipe_validation_jobs_fetch_full_history(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/release.yml').read_text()
        detect=workflow.split('  detect:',1)[1].split('  reuse:',1)[0]
        publish=workflow.split('  publish:',1)[1]
        self.assertIn('fetch-depth: 0',detect)
        self.assertIn('fetch-depth: 0',publish)
        self.assertIn('actions: read',workflow)
    def test_single_new_sdk_download_has_explicit_artifact_parent(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/release.yml').read_text()
        publish=workflow.split('  publish:',1)[1]
        self.assertIn('name: v8-windows-arm64\n          path: inputs/v8-windows-arm64',publish)
        self.assertIn("if: needs.detect.outputs.reuse_run_id == ''\n        with:\n          pattern: v8-*",publish)
    def publish_plan(self):
        with patch.object(resume,'LOCK',self.lock),patch.object(resume,'producer_inputs',return_value=self.hashes):
            return resume.publish_plan(self.api,self.root,'b'*40)
    def test_publish_only_reuses_tenth_sdk_and_passed_runtime_proof(self):
        value=self.publish_plan()
        self.assertEqual(resume.ARM_PRODUCER,value['targets']['windows-arm64']['producerRevision'])
        self.assertEqual(resume.PRODUCER,value['targets']['linux-arm64']['producerRevision'])
        self.assertEqual(resume.PUBLISH_RUN,value['publishOnlySourceRunId'])
        with patch.object(resume,'LOCK',self.lock),patch.object(resume,'producer_inputs',return_value=self.hashes):
            resume.validate_plan(value,self.pins,'b'*40)
    def test_publish_only_never_accepts_failed_arm_runtime(self):
        self.publish_jobs[2]['conclusion']='failure'
        with self.assertRaisesRegex(ValueError,'runtime stage'):self.publish_plan()
    def test_publish_only_changed_source_revision_or_artifact_rejected(self):
        self.publish_run['head_sha']='0'*40
        with self.assertRaisesRegex(ValueError,'source run'):self.publish_plan()
        self.publish_run['head_sha']=resume.ARM_PRODUCER
        self.items[resume.PUBLISH_LOCK['windows-arm-runtime'][0]]['digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'artifact identity'):self.publish_plan()
    def test_publish_only_workflow_has_no_compile_or_runtime_steps(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/release.yml').read_text()
        job=workflow.split('  publish-only:',1)[1]
        self.assertNotIn('tool/v8/build.py',job)
        self.assertNotIn('tool/sdk_smoke.py',job)
        self.assertIn('publish-plan',job);self.assertIn('publish-download',job)
        self.assertIn("if: inputs.publish_only_source_run == ''",workflow)
