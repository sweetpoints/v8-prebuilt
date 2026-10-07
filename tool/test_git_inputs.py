"""Real Git checkout tests for recipe identity, not mocked byte normalization."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

import git_inputs


class GitInputsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Recipe Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'true')
        self.path = self.root / 'recipe.py'
        self.original = 'value = "中文"\nprint(value)\n'.encode('utf-8')
        self.path.write_bytes(self.original)
        self.git('add', 'recipe.py')
        self.git('commit', '-qm', 'fixture')
        self.revision = self.git('rev-parse', 'HEAD').decode().strip()
        self.path.unlink()
        self.git('checkout', '--', 'recipe.py')

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.root, check=True,
                              capture_output=True).stdout

    def test_windows_autocrlf_checkout_keeps_identity_and_actual_hash(self):
        self.assertIn('w/crlf', self.git('ls-files', '--eol', 'recipe.py').decode())
        proof = git_inputs.read_working_input(self.root, self.revision, 'recipe.py')
        self.assertEqual(proof['lineEndingConversion'], 'lf-to-crlf')
        self.assertEqual(proof['canonicalSha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(proof['workingTreeSha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotEqual(proof['canonicalSha256'], proof['workingTreeSha256'])
        self.assertEqual(git_inputs.snapshot_inputs(self.root, self.revision, ['recipe.py']),
                         git_inputs.current_recipe(self.root, self.revision, ['recipe.py']))

    def test_lf_attributes_override_autocrlf_for_future_checkout(self):
        (self.root / '.gitattributes').write_bytes(b'*.py text eol=lf\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'force recipe LF')
        self.path.unlink()
        self.git('checkout', '--', 'recipe.py')
        self.assertIn('w/lf', self.git('ls-files', '--eol', 'recipe.py').decode())
        self.assertEqual(self.path.read_bytes(), self.original)
        proof = git_inputs.read_working_input(self.root, self.revision, 'recipe.py')
        self.assertEqual(proof['lineEndingConversion'], 'none')
        self.assertEqual(proof['canonicalSha256'], proof['workingTreeSha256'])

    def test_changed_source_mixed_eol_extra_cr_are_rejected(self):
        for altered in [self.original.replace(b'print', b'raise'),
                        self.original.replace(b'\n', b'\r\n', 1),
                        self.original.replace(b'\n', b'\r\r\n')]:
            with self.subTest(altered=altered):
                self.path.write_bytes(altered)
                with self.assertRaisesRegex(ValueError, 'differs'):
                    git_inputs.current_recipe(self.root, self.revision, ['recipe.py'])

    def test_historical_blob_hash_does_not_relabel_changed_worktree(self):
        expected = git_inputs.snapshot_inputs(self.root, self.revision, ['recipe.py'])
        self.path.write_bytes(b'new producer\n')
        self.assertEqual(git_inputs.snapshot_inputs(self.root, self.revision, ['recipe.py']), expected)
        with self.assertRaises(ValueError):
            git_inputs.current_recipe(self.root, self.revision, ['recipe.py'])

    def test_noncanonical_revision_or_path_rejected(self):
        for revision, name in [('HEAD', 'recipe.py'), (self.revision, '../recipe.py'),
                               (self.revision, '/recipe.py'), (self.revision, 'dir\\recipe.py')]:
            with self.subTest(revision=revision, name=name), self.assertRaises(ValueError):
                git_inputs.snapshot_inputs(self.root, revision, [name])
