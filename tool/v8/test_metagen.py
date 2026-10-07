"""Reviewed iOS metagen driver selection and source restoration contracts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build


# The exact reviewed upstream decision, isolated from GN to exercise inputs.
FLAGS_READER = ('def detect(cflags):\n' + build.IOS_METAGEN_DRIVER_LINE +
                '\n  return cl_mode\n')


class MetagenDriverTest(unittest.TestCase):
    def fixture(self, directory):
        source = Path(directory) / 'v8'
        path = source / 'tools/metagen/compile_flags.py'
        path.parent.mkdir(parents=True)
        path.write_text(FLAGS_READER)
        return source, path, source / 'out/sdk'

    def test_absolute_apple_sdk_operand_is_not_a_clang_cl_switch(self):
        original = {}
        exec(FLAGS_READER, original)
        self.assertTrue(original['detect'](['-isysroot', '/Applications/Xcode.app/SDKs/iPhoneOS.sdk']))
        with tempfile.TemporaryDirectory() as directory:
            source, path, out = self.fixture(directory)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch('build.IOS_METAGEN_FLAGS_SHA256', digest):
                with build.ios_metagen_flags(source, out) as files:
                    fixed = {}
                    exec(path.read_text(), fixed)
                    for sdk in ['iPhoneOS.sdk', 'iPhoneSimulator.sdk']:
                        self.assertFalse(fixed['detect'](['-isysroot', '/Applications/Xcode/SDKs/' + sdk]))
                    self.assertFalse(fixed['detect'](['-I', '/Users/runner/headers', '-std=c++20']))
                    self.assertTrue(fixed['detect'](['/MT', '/std:c++20']))
                    self.assertTrue(fixed['detect'](['-isysroot', '/Applications/SDK', '/MT']))
                    report = json.loads(files['validation/metagen-driver-fix.json'].read_text())
                    self.assertEqual(report['originalSha256'], digest)
                    self.assertEqual(report['patchedSha256'], build.sha(path))
                    self.assertEqual(files[report['patched']].read_bytes(), path.read_bytes())
            self.assertEqual(path.read_text(), FLAGS_READER)

    def test_unknown_source_hash_fails_without_modifying_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            source, path, out = self.fixture(directory)
            with self.assertRaisesRegex(ValueError, 'Unreviewed upstream'):
                with build.ios_metagen_flags(source, out):
                    self.fail('must not run Ninja')
            self.assertEqual(path.read_text(), FLAGS_READER)

    def test_failure_restores_official_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, path, out = self.fixture(directory)
            with patch('build.IOS_METAGEN_FLAGS_SHA256', build.sha(path)):
                with self.assertRaisesRegex(RuntimeError, 'Ninja failed'):
                    with build.ios_metagen_flags(source, out):
                        raise RuntimeError('Ninja failed')
            self.assertEqual(path.read_text(), FLAGS_READER)

    def test_new_upstream_detection_and_old_no_metagen_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            source, path, out = self.fixture(directory)
            path.write_text('def detect(cflags):\n  return False\n')
            with build.ios_metagen_flags(source, out) as files:
                self.assertEqual(files, {})
            self.assertIn('return False', path.read_text())
            path.unlink()
            with build.ios_metagen_flags(source, out) as files:
                self.assertEqual(files, {})

    def test_concurrent_workaround_mutation_is_rejected_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            source, path, out = self.fixture(directory)
            with patch('build.IOS_METAGEN_FLAGS_SHA256', build.sha(path)):
                with self.assertRaisesRegex(ValueError, 'changed during build'):
                    with build.ios_metagen_flags(source, out):
                        path.write_text('unexpected change')
            self.assertEqual(path.read_text(), FLAGS_READER)
