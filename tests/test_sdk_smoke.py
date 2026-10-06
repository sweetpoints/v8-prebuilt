"""SDK consumer failure evidence must never turn into release acceptance."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sdk_smoke', Path(__file__).resolve().parents[1] / 'tool/sdk_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class ConsumerDiagnosticsTests(unittest.TestCase):
    def exercise(self, runtime):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / 'sdk-smoke.json'
            identity = {'linkingSha256': 'fixed-linking', 'librarySha256': 'fixed-monolith',
                        'libraries': {'lib/monolith.lib': 'fixed-monolith'}}
            def compile_probe(root, contract, compiler, probe, source, sysroot):
                probe.write_bytes(b'compiled-executable')
                return {'compiler': 'fixed official Clang', 'compileCommand': ['fixed-command']}
            arguments = ['sdk_smoke.py', '--sdk-root', str(root), '--expected-version', '15.4.80.25',
                         '--compiler', str(root / 'clang-cl.exe'), '--output', str(output)]
            with patch.object(smoke.sys, 'argv', arguments), \
                 patch.object(smoke, 'inputs', return_value=({'runtimeFlags': []}, identity)), \
                 patch.object(smoke, 'compile_probe', side_effect=compile_probe), \
                 patch.object(smoke.subprocess, 'run', side_effect=runtime), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                try:
                    smoke.main()
                    raised = None
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                    raised = error
            return json.loads(output.read_text()), raised, stderr.getvalue()

    def test_access_violation_records_the_last_stage_and_still_raises(self):
        failure = subprocess.CalledProcessError(0xC0000005, 'sdk-probe.exe',
                                                output='', stderr='sdk-stage: isolate-create\n')
        evidence, raised, stderr = self.exercise(failure)
        self.assertIs(raised, failure)
        self.assertEqual(evidence['status'], 'failed')
        self.assertEqual(evidence['returnCode'], 0xC0000005)
        self.assertEqual(evidence['runtimeStderr'], 'sdk-stage: isolate-create\n')
        self.assertEqual(evidence['linkingSha256'], 'fixed-linking')
        self.assertNotIn('official_api_execute', evidence['cases'])
        self.assertIn('isolate-create', stderr)

    def test_timeout_retains_bytes_diagnostics_without_claiming_success(self):
        failure = subprocess.TimeoutExpired('sdk-probe', 30, output=b'',
                                            stderr=b'sdk-stage: platform-create\n')
        evidence, raised, stderr = self.exercise(failure)
        self.assertIs(raised, failure)
        self.assertEqual(evidence['status'], 'failed')
        self.assertIsNone(evidence['returnCode'])
        self.assertIn('platform-create', stderr)

    def test_success_still_requires_the_exact_stdout_confirmation(self):
        success = subprocess.CompletedProcess('sdk-probe', 0,
                                              stdout='official-v8-api-sdk-consumer-passed\n',
                                              stderr='sdk-stage: platform-dispose\n')
        evidence, raised, _ = self.exercise(lambda *args, **kwargs: success)
        self.assertIsNone(raised)
        self.assertEqual(evidence['status'], 'passed')
        self.assertIn('official_api_execute', evidence['cases'])

    def test_windows_import_library_side_products_stay_in_the_temporary_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); temp = root / 'temporary'; temp.mkdir()
            source = temp / 'consumer.cpp'; source.write_text('int main(){return 0;}')
            probe = root / 'sdk/validation/sdk-probe.exe'; probe.parent.mkdir(parents=True)
            contract = {'compilerStyle': 'clang-cl', 'includeDirs': [], 'defines': [],
                        'compileOptions': ['/MT'], 'libraries': ['sdk/lib/monolith.lib'],
                        'linkOptions': ['/machine:x64'], 'systemLibraries': []}
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if '--version' not in args:
                    probe.write_bytes(b'executable')
                return subprocess.CompletedProcess(args, 0, stdout='official Clang')
            with patch.object(smoke.subprocess, 'run', side_effect=run):
                smoke.compile_probe(root, contract, root / 'clang-cl.exe', probe, source)
            self.assertIn('/IMPLIB:' + str(temp / 'consumer-import.lib'), calls[0])
            self.assertEqual(list(probe.parent.iterdir()), [probe])


if __name__ == '__main__':
    unittest.main()
