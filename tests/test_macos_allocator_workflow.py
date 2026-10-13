"""Prevent built SDK loss and smoke CLI drift in the macOS PR gate."""
from pathlib import Path
import re
import json
import os
import tempfile
import textwrap
from unittest.mock import patch
import shlex
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/macos-allocator-validation.yml'


class MacAllocatorWorkflowTests(unittest.TestCase):
    def test_native_smoke_requires_fixed_compiler_and_sysroot(self):
        text = WORKFLOW.read_text()
        command, = re.findall(r'run: (python tool/sdk_smoke.py .+)', text)
        arguments = shlex.split(command)
        self.assertEqual(arguments[arguments.index('--compiler') + 1], '$V8_SDK_COMPILER')
        self.assertEqual(arguments[arguments.index('--sysroot') + 1], '$V8_SDK_SYSROOT')
        self.assertNotIn('--compile-only', arguments)
        self.assertNotIn('--execute-only', arguments)
        selector = text.split('      - name: Select the fixed official consumer compiler\n')[1].split('      - name:')[0]
        self.assertIn("source = pathlib.Path('.cache/v8-source') / revision / target / 'v8'", selector)
        self.assertIn("third_party/llvm-build/Release+Asserts/bin/clang++", selector)
        self.assertIn("actual != revision", selector)
        self.assertIn("version != manifest['targets'][target]['toolchain']['clang']", selector)

    def test_selector_exports_only_the_pinned_package_compiler(self):
        text = WORKFLOW.read_text()
        selector = text.split('      - name: Select the fixed official consumer compiler\n')[1].split('      - name:')[0]
        script = textwrap.dedent(selector.split("python - <<'PYTHON'\n")[1].rsplit('          PYTHON', 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision, target = 'a' * 40, 'macos-arm64'
            source = root / '.cache/v8-source' / revision / target / 'v8'
            compiler = source / 'third_party/llvm-build/Release+Asserts/bin/clang++'
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b'fixed compiler')
            (source / 'include').mkdir()
            (source / 'include/v8-version.h').write_text('fixed version')
            artifact = root / 'artifacts' / revision
            artifact.mkdir(parents=True)
            (artifact / 'manifest.json').write_text(json.dumps({
                'targets': {target: {'toolchain': {'clang': 'fixed official Clang'}}}}))
            sysroot = root / 'MacOSX.sdk'
            sysroot.mkdir()
            environment = root / 'github-env'
            original = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, V8_REVISION=revision, SDK_TARGET=target, GITHUB_ENV=str(environment)), \
                     patch('subprocess.check_output', side_effect=[revision, 'fixed official Clang', str(sysroot)]) as call:
                    exec(compile(script, str(WORKFLOW), 'exec'), {})
                    self.assertEqual(call.call_args_list[1].args[0], [str(compiler.relative_to(root)), '--version'])
                self.assertEqual(environment.read_text(),
                    'V8_SDK_COMPILER=' + str(compiler.resolve()) + '\nV8_SDK_SYSROOT=' + str(sysroot) + '\n')
                environment.unlink()
                with patch.dict(os.environ, V8_REVISION=revision, SDK_TARGET=target, GITHUB_ENV=str(environment)), \
                     patch('subprocess.check_output', side_effect=[revision, 'other compiler']):
                    with self.assertRaisesRegex(ValueError, 'differs from the packaged SDK toolchain'):
                        exec(compile(script, str(WORKFLOW), 'exec'), {})
                self.assertFalse(environment.exists())
            finally:
                os.chdir(original)

    def test_built_package_is_saved_before_any_acceptance_check(self):
        text = WORKFLOW.read_text()
        names = re.findall(r'      - name: (.+)', text)
        built = names.index('Build only the affected macOS SDK')
        self.assertEqual(names[built + 1], 'Preserve the built candidate before validation')
        self.assertLess(names.index('Preserve the built candidate before validation'),
                        names.index('Verify SDK does not replace the host malloc zone'))
        candidate = text.split('      - name: Preserve the built candidate before validation\n')[1].split('      - name:')[0]
        self.assertIn('name: candidate-built-${{ matrix.target }}', candidate)
        self.assertIn('artifacts/${{ env.V8_REVISION }}/', candidate)
        self.assertIn('candidate-pins.json', candidate)
        proof = text.split('      - name: Preserve validation proof and failure diagnostics\n')[1]
        self.assertIn('if: always()', proof)
        self.assertIn('name: candidate-proof-${{ matrix.target }}', proof)
        self.assertIn('/sdk-smoke.json', proof)
        self.assertIn('/allocator-isolation.json', proof)


if __name__ == '__main__':
    unittest.main()
