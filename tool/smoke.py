#!/usr/bin/env python3
"""Load a native source_v8 library and exercise its public C ABI."""
import argparse
import ctypes
import hashlib
import json
import platform
from pathlib import Path
import time


def smoke(library, expected_version):
    library = Path(library).resolve()
    library_digest = hashlib.sha256(library.read_bytes()).hexdigest()
    lib = ctypes.CDLL(str(library))
    lib.sv8_version.argtypes = []
    lib.sv8_version.restype = ctypes.c_char_p
    lib.sv8_create.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.sv8_create.restype = ctypes.c_void_p
    lib.sv8_start.argtypes = [ctypes.c_void_p] + [ctypes.c_char_p] * 3
    lib.sv8_start.restype = None
    lib.sv8_poll.argtypes = [ctypes.c_void_p]
    lib.sv8_poll.restype = ctypes.c_void_p
    for name in ('sv8_cancel', 'sv8_destroy', 'sv8_free'):
        fn = getattr(lib, name)
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = None
    version = lib.sv8_version().decode('utf-8')
    if version != expected_version:
        raise RuntimeError(f'V8 version mismatch: {version} != {expected_version}')

    def execute(script, timeout_ms=2000, cancel=False):
        runtime = lib.sv8_create(timeout_ms, 32)
        if not runtime:
            raise RuntimeError('sv8_create returned null')
        try:
            if cancel:
                lib.sv8_cancel(runtime)
            lib.sv8_start(runtime, script.encode(), b'{}', b'')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pointer = lib.sv8_poll(runtime)
                if not pointer:
                    raise RuntimeError('sv8_poll returned null')
                try:
                    result = json.loads(ctypes.string_at(pointer).decode('utf-8'))
                finally:
                    lib.sv8_free(pointer)
                if result['status'] != 'pending':
                    return result
                time.sleep(0.001)
            raise RuntimeError('native smoke poll deadline exceeded')
        finally:
            lib.sv8_destroy(runtime)

    cases = []
    for name, script, expected in (
        ('arithmetic', '2 + 2', 4),
        ('promise', 'Promise.resolve(42)', 42),
        ('unicode', "'书源 ✓'", '书源 ✓'),
    ):
        result = execute(script)
        if result != {'status': 'done', 'value': expected}:
            raise RuntimeError(f'{name} failed: {result}')
        cases.append(name)
    result = execute("throw new Error('smoke-error')")
    if result.get('status') != 'error' or 'smoke-error' not in result.get('error', ''):
        raise RuntimeError(f'exception failed: {result}')
    cases.append('exception')
    for name, options in (
        ('timeout', {'timeout_ms': 100}),
        ('cancel_before_start', {'cancel': True}),
    ):
        result = execute('while (true) {}', **options)
        if result != {'status': 'error', 'error': 'execution_timeout'}:
            raise RuntimeError(f'{name} failed: {result}')
        cases.append(name)
    if hashlib.sha256(library.read_bytes()).hexdigest() != library_digest:
        raise RuntimeError('native library changed during smoke test')
    return {'schemaVersion': 1, 'status': 'passed', 'version': version,
            'librarySha256': library_digest,
            'host': {'os': platform.system(), 'machine': platform.machine()},
            'cases': cases, 'scope': 'native desktop public C ABI'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # A failed rerun must not leave an earlier success report behind.
    args.output.unlink(missing_ok=True)
    report = smoke(args.library, args.expected_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
