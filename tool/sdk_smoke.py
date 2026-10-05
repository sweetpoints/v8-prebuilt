#!/usr/bin/env python3
"""Compile, link and execute a consumer of the packaged official V8 C++ API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

# A build-time consumer fixture, not an application bridge or distributed API.
PROBE = r'''#include <cstdio>
#include <cstring>
#include <memory>
#include "libplatform/libplatform.h"
#include "v8.h"
int main(int argc, char** argv) {
  if (argc < 2 || std::strcmp(v8::V8::GetVersion(), argv[1])) return 2;
  if (argc > 2 && argv[2][0]) v8::V8::SetFlagsFromString(argv[2]);
  auto platform = v8::platform::NewDefaultPlatform();
  v8::V8::InitializePlatform(platform.get());
  if (!v8::V8::Initialize()) return 3;
  std::unique_ptr<v8::ArrayBuffer::Allocator> allocator(
      v8::ArrayBuffer::Allocator::NewDefaultAllocator());
  v8::Isolate::CreateParams params;
  params.array_buffer_allocator = allocator.get();
  auto* isolate = v8::Isolate::New(params);
  bool passed = false;
  {
    v8::Isolate::Scope isolate_scope(isolate);
    v8::HandleScope handles(isolate);
    auto context = v8::Context::New(isolate);
    v8::Context::Scope context_scope(context);
    v8::TryCatch caught(isolate);
    auto code = v8::String::NewFromUtf8Literal(isolate,
        "(()=>{ let n=0; for(let i=0;i<10000;i++) n+=i; "
        "if(n!==49995000) throw Error('arithmetic'); "
        "return Promise.resolve('V8 SDK'); })()");
    v8::Local<v8::Script> script;
    v8::Local<v8::Value> value;
    if (v8::Script::Compile(context, code).ToLocal(&script) &&
        script->Run(context).ToLocal(&value) && value->IsPromise()) {
      isolate->PerformMicrotaskCheckpoint();
      auto promise = value.As<v8::Promise>();
      if (promise->State() == v8::Promise::kFulfilled) {
        v8::String::Utf8Value text(isolate, promise->Result());
        passed = *text && !std::strcmp(*text, "V8 SDK");
      }
    }
  }
  isolate->Dispose();
  v8::V8::Dispose();
  v8::V8::ShutdownPlatform();
  if (!passed) return 4;
  std::puts("official-v8-api-sdk-consumer-passed");
  return 0;
}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sdk_path(root, relative):
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts or not value.parts:
        raise ValueError('SDK path must be relative and contained')
    path = root / value
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('SDK path escapes root')
    return path


def inputs(root):
    contract_path = root / 'linking.json'
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    if contract.get('schemaVersion') != 1:
        raise ValueError('unsupported SDK linking contract')
    for key in ('includeDirs', 'defines', 'compileOptions', 'libraries', 'linkOptions', 'systemLibraries'):
        if not isinstance(contract.get(key), list) or any(not isinstance(v, str) for v in contract[key]):
            raise ValueError('invalid SDK linking field: ' + key)
    if not contract['libraries']:
        raise ValueError('SDK library list must contain the primary monolith first')
    libraries = [sdk_path(root, relative) for relative in contract['libraries']]
    if any(not path.is_file() for path in libraries):
        raise ValueError('SDK library missing')
    for relative in contract['includeDirs']:
        if not sdk_path(root, relative).is_dir():
            raise ValueError('SDK include directory missing')
    identity = {'linkingSha256': sha(contract_path),
                'librarySha256': sha(libraries[0]),
                'libraries': {name: sha(path) for name, path in zip(contract['libraries'], libraries)}}
    return contract, identity


def compile_probe(root, contract, compiler, output, source):
    style = contract.get('compilerStyle', 'clang-cl' if os.name == 'nt' else 'clang++')
    include = [sdk_path(root, item) for item in contract['includeDirs']]
    libraries = [sdk_path(root, item) for item in contract['libraries']]
    if style == 'clang-cl':
        args = [str(compiler), '/nologo', '/std:c++20']
        args += ['/I' + str(path) for path in include]
        args += ['/D' + value for value in contract['defines']]
        args += contract['compileOptions'] + [str(source), '/Fe:' + str(output)]
        args += ['/link'] + [str(path) for path in libraries]
        args += contract['linkOptions'] + contract['systemLibraries']
    elif style == 'clang++':
        args = [str(compiler), '-std=c++20']
        args += ['-I' + str(path) for path in include]
        args += ['-D' + value for value in contract['defines']]
        args += contract['compileOptions'] + [str(source)]
        args += [str(path) for path in libraries] + contract['linkOptions']
        args += ['-l' + name for name in contract['systemLibraries']]
        args += ['-o', str(output)]
    else:
        raise ValueError('unsupported compiler style')
    subprocess.run(args, check=True, timeout=300)
    if not output.is_file():
        raise ValueError('consumer link did not produce an executable')
    return subprocess.run([str(compiler), '--version'], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--probe-output', type=Path)
    parser.add_argument('--probe-path', type=Path)
    parser.add_argument('--compile-report', type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--compile-only', action='store_true')
    modes.add_argument('--execute-only', action='store_true')
    args = parser.parse_args()
    if args.execute_only and args.probe_path is None:
        parser.error('--execute-only requires --probe-path')
    if not args.execute_only and args.compiler is None:
        parser.error('compilation requires --compiler for the actual toolchain')
    if args.compile_only and args.probe_output is None:
        parser.error('--compile-only requires --probe-output')
    args.output.unlink(missing_ok=True)
    root = args.sdk_root.resolve()
    contract, identity = inputs(root)
    with tempfile.TemporaryDirectory(prefix='v8-sdk-consumer-') as temporary:
        directory = Path(temporary)
        source = directory / 'consumer.cpp'
        source.write_text(PROBE, encoding='utf-8')
        probe = (args.probe_path if args.execute_only else args.probe_output) or root / 'validation' / ('sdk-probe.exe' if os.name == 'nt' else 'sdk-probe')
        probe = probe.resolve()
        compiler_version = None
        if not args.execute_only:
            probe.parent.mkdir(parents=True, exist_ok=True)
            compiler_version = compile_probe(root, contract, args.compiler.resolve(), probe, source)
        evidence = {'schemaVersion': 1, 'scope': 'official V8 API SDK consumer',
                    'version': args.expected_version, **identity, 'probeSha256': sha(probe),
                    'probeSourceSha256': hashlib.sha256(PROBE.encode()).hexdigest(),
                    'host': {'os': platform.system(), 'machine': platform.machine()},
                    'nativeConsumerExecuted': False,
                    'cases': ['official_api_compile', 'official_api_link']}
        if probe.is_relative_to(root):
            evidence['probe'] = probe.relative_to(root).as_posix()
        if compiler_version is not None:
            evidence['compiler'] = compiler_version
        if args.execute_only:
            # The cross-built executable needs prior compile/link evidence.
            prior_path = args.compile_report or probe.with_name(probe.name + '.json')
            prior = json.loads(prior_path.read_text(encoding='utf-8'))
            for key in ('version', 'linkingSha256', 'librarySha256', 'libraries', 'probeSha256', 'probeSourceSha256'):
                if prior.get(key) != evidence[key]:
                    raise ValueError('cross-built consumer evidence differs: ' + key)
            if prior.get('status') != 'compiled' or prior.get('cases') != ['official_api_compile', 'official_api_link']:
                raise ValueError('missing cross-built compile/link proof')
            evidence['compiler'] = prior['compiler']
            evidence['compileHost'] = prior['host']
        if args.compile_only:
            evidence['status'] = 'compiled'
            probe.with_name(probe.name + '.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        else:
            flags = contract.get('runtimeFlags', [])
            if not isinstance(flags, list) or any(not isinstance(value, str) for value in flags):
                raise ValueError('invalid V8 runtime flags')
            result = subprocess.run([str(probe), args.expected_version, ' '.join(flags)],
                                    check=True, capture_output=True, text=True, timeout=30)
            if result.stdout.strip() != 'official-v8-api-sdk-consumer-passed':
                raise ValueError('native V8 API consumer did not confirm success')
            evidence.update(status='passed', nativeConsumerExecuted=True)
            evidence['cases'].append('official_api_execute')
        if inputs(root)[1] != identity:
            raise ValueError('SDK inputs changed during consumer test')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(evidence))


if __name__ == '__main__':
    main()
