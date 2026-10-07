"""Immutable producer recipes with explicit checkout line-ending evidence."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _blob(root, revision, name):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Full producer revision required')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name:
        raise ValueError('Producer input must be a canonical repository-relative path')
    return subprocess.run(['git', 'show', revision + ':' + name], cwd=root,
                          check=True, capture_output=True).stdout


def snapshot_inputs(root, revision, paths):
    """Canonical SHA-256 hashes of the immutable Git blob bytes, never a checkout."""
    return {name: _sha(_blob(root, revision, name)) for name in paths}


def read_working_input(root, revision, name):
    """Verify content and retain both canonical and actual checkout hashes.

    Old Windows checkouts may expand every LF to CRLF. Accept only that exact
    transformation of UTF-8 text; mixed newlines, additional CRs and all other
    edits fail. Do not run Git clean filters, which could hide changed content.
    """
    committed = _blob(root, revision, name)
    root = Path(root).resolve()
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Producer input cannot escape the checkout or be a symlink')
    actual = path.read_bytes()
    conversion = 'none'
    if actual != committed:
        try:
            committed.decode('utf-8', errors='strict')
        except UnicodeDecodeError as error:
            raise ValueError('Current producer input differs from declared commit: ' + name) from error
        if (b'\0' in committed or b'\r' in committed or b'\n' not in committed
                or actual != committed.replace(b'\n', b'\r\n')):
            raise ValueError('Current producer input differs from declared commit: ' + name)
        conversion = 'lf-to-crlf'
    return {'canonicalSha256': _sha(committed), 'workingTreeSha256': _sha(actual),
            'lineEndingConversion': conversion}


def current_recipe(root, revision, paths):
    """Verify each worktree input and return its canonical Git content hash."""
    return {name: read_working_input(root, revision, name)['canonicalSha256'] for name in paths}
