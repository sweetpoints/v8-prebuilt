#!/usr/bin/env python3
"""Authenticate trusted workflow artifact graphs and build only missing SDKs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import stat
import zipfile
from release_package import TARGETS
from release_publish import GitHub
from release_pins import make_pins, release_tag
import git_inputs

INPUTS = ('tool/v8/build.py', 'tool/v8/desktop.py', 'tool/v8/ios.py', 'tool/sdk_smoke.py', 'tool/v8/pins.json')
WORKFLOW = '.github/workflows/release.yml'
RUNTIME = {'linux-arm-runtime': 'linux-arm64', 'windows-arm-runtime': 'windows-arm64'}
MAX_DEPTH = 32
MAC_REPLACEMENTS = ('macos-arm64', 'macos-x64')


def revision_identity(value):
    revision = value.get('sdkArtifactRevision', 0)
    tag = release_tag(value['pins']['v8']['version'], revision)
    if value.get('releaseTag', tag) != tag or (revision and 'releaseTag' not in value):
        raise ValueError('SDK artifact revision tag differs')
    replacements = value.get('replacesTargets', [])
    if replacements != (list(MAC_REPLACEMENTS) if revision else []):
        raise ValueError('SDK revisions must explicitly replace exactly both Mac targets')
    records = value.get('replacementAssets', {})
    if not isinstance(records, dict) or set(records) != set(replacements):
        raise ValueError('SDK replacement provenance inventory differs')
    if revision and (type(value.get('sourceRunId')) is not int or value['sourceRunId'] <= 0):
        raise ValueError('SDK revision requires a fixed trusted source run')
    if any(not isinstance(record, dict) for record in records.values()):
        raise ValueError('SDK replacement provenance records required')
    return revision, tag, replacements, records

class ArtifactIntegrityError(ValueError):
    pass



def sha(data): return hashlib.sha256(data).hexdigest()
def snapshot_inputs(root, revision): return git_inputs.snapshot_inputs(root, revision, INPUTS)
def current_recipe(root, revision): return git_inputs.current_recipe(root, revision, INPUTS)


def pages(gh, url, key):
    result = []; page = 1
    separator = '&' if '?' in url else '?'
    while True:
        values = gh.request(url + f'{separator}per_page=100&page={page}')[key]
        result.extend(values)
        if len(values) < 100: return result
        page += 1


def ancestor(root, older, newer):
    if not all(re.fullmatch('[0-9a-f]{40}', value or '') for value in (older, newer)):
        raise ValueError('Full Git revisions required')
    return subprocess.run(['git', 'merge-base', '--is-ancestor', older, newer], cwd=root,
                          capture_output=True).returncode == 0


def repo_identity(gh):
    name = gh.base.removeprefix('https://api.github.com/repos/')
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
        raise ValueError('Same-repository GitHub API required')
    repo = gh.request(gh.base)
    workflow = gh.request(gh.base + '/actions/workflows/release.yml')
    if repo.get('full_name') != name or workflow.get('path') != WORKFLOW:
        raise ValueError('Official repository/workflow identity differs')
    return {'fullName': name, 'defaultBranch': repo['default_branch'],
            'workflowPath': WORKFLOW, 'workflowId': workflow.get('id')}


def trusted_run(gh, root, run_id, builder_revision, repository):
    if type(run_id) is not int or run_id <= 0: raise ValueError('Positive source run ID required')
    run = gh.request(gh.base + f'/actions/runs/{run_id}')
    if (run.get('status') != 'completed' or run.get('event') not in ('schedule', 'workflow_dispatch')
            or bool(run.get('pull_requests')) or run.get('head_branch') != repository['defaultBranch']
            or run.get('path', '').split('@')[0] != WORKFLOW
            or run.get('repository', {}).get('full_name') != repository['fullName']
            or run.get('head_repository', {}).get('full_name') != repository['fullName']
            or (run.get('workflow_id') is not None and run['workflow_id'] != repository['workflowId'])
            or not ancestor(root, run.get('head_sha'), builder_revision)):
        raise ValueError('Untrusted source run: require same-repository official main workflow ancestor, never PR/fork')
    return {'id': run_id, 'head_sha': run['head_sha'], 'event': run['event'], 'path': WORKFLOW,
            'head_branch': run['head_branch'], 'run_attempt': run['run_attempt'],
            'repository': repository['fullName']}


def artifact_record(item, run):
    checksum = item.get('digest', '')
    origin = item.get('workflow_run', {})
    if item.get('expired') is True: raise ValueError('Source artifact expired and unavailable')
    if (not re.fullmatch('sha256:[0-9a-f]{64}', checksum) or type(item.get('id')) is not int
            or type(item.get('size_in_bytes')) is not int or item['size_in_bytes'] <= 0
            or item.get('expired') is not False or origin.get('id') != run['id']
            or origin.get('head_sha') != run['head_sha']):
        raise ArtifactIntegrityError('Immutable artifact identity/digest/origin unavailable')
    return {'id': item['id'], 'name': item['name'], 'sha256': checksum[7:],
            'size': item['size_in_bytes'], 'runId': run['id'],
            'producerRevision': run['head_sha'], 'runAttempt': run['run_attempt']}


def archive_bytes(gh, item):
    data = gh.request(gh.base + f"/actions/artifacts/{item['id']}/zip",
                      content_type='application/octet-stream', accept='application/vnd.github+json')
    if len(data) != item['size'] or sha(data) != item['sha256']:
        raise ArtifactIntegrityError('Artifact ZIP digest/size differs')
    return data


def extract(data, destination):
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        seen = set(); total = 0
        for item in bundle.infolist():
            name = item.filename; path = PurePosixPath(name.rstrip('/'))
            if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts or not path.parts
                    or path.as_posix() != name.rstrip('/') or name in seen or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('Unsafe artifact ZIP member')
            seen.add(name); total += item.file_size
            if total > 2_000_000_000: raise ValueError('Artifact ZIP payload too large')
            target = destination.joinpath(*path.parts)
            if item.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(bundle.read(item))


def json_artifact(gh, record, filename):
    if record['size'] > 8 * 1024 * 1024: raise ValueError('Source metadata artifact too large')
    with zipfile.ZipFile(io.BytesIO(archive_bytes(gh, record))) as bundle:
        if bundle.namelist() != [filename]: raise ValueError('Ambiguous source metadata artifact')
        data = bundle.read(filename)
        if len(data) > 8 * 1024 * 1024: raise ValueError('Source metadata payload too large')
        return json.loads(data)


def validate_pins(pins):
    official = {'v8': 'https://chromium.googlesource.com/v8/v8.git',
                'depotTools': 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'}
    if pins.get('schemaVersion') != 1 or set(pins.get('targets', {})) != set(TARGETS):
        raise ValueError('Complete official target source pins required')
    for key, url in official.items():
        item = pins.get(key, {})
        if item.get('repository') != url or not re.fullmatch('[0-9a-f]{40}', item.get('revision', '')):
            raise ValueError('Official fixed source revision required')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', pins['v8'].get('version', '')):
        raise ValueError('Fixed source version required')


def successful(jobs, name, target=False):
    matched = [j for j in jobs if (j['name'].startswith('build (' + name + ',') if target else j['name'] == name)]
    return any(j.get('conclusion') == 'success' for j in matched)


class Graph:
    def __init__(self, gh, root, builder_revision):
        self.gh, self.root, self.builder = gh, root, builder_revision
        self.repository = repo_identity(gh); self.nodes = {}; self.active = set()

    def resolve(self, run_id):
        if run_id in self.active or len(self.active) >= MAX_DEPTH: raise ValueError('Cyclic or excessive reuse graph')
        if run_id in self.nodes: return self.nodes[run_id]
        self.active.add(run_id)
        try:
            run = trusted_run(self.gh, self.root, run_id, self.builder, self.repository)
            jobs = pages(self.gh, self.gh.base + f'/actions/runs/{run_id}/jobs?filter=all', 'jobs')
            values = pages(self.gh, self.gh.base + f'/actions/runs/{run_id}/artifacts', 'artifacts')
            index = {}
            for item in values:
                name = item['name']
                if name in index: raise ValueError('Duplicate artifact name in source run')
                index[name] = item
            if not successful(jobs, 'detect'): raise ValueError('Frozen source detection did not succeed')
            pin_record = artifact_record(index['stable-pins'], run)
            pins = json_artifact(self.gh, pin_record, 'stable-pins.json'); validate_pins(pins)
            committed_base = json.loads(subprocess.run(['git', 'show', run['head_sha'] + ':tool/v8/pins.json'],
                                        cwd=self.root, check=True, capture_output=True).stdout)
            expected_pins = make_pins(committed_base, {'version': pins['v8']['version'],
                         'revision': pins['v8']['revision'], 'tag': 'v8-' + pins['v8']['version']})
            if pins != expected_pins: raise ValueError('Frozen pins differ from source producer recipe')
            node = {'run': run, 'pins': pins, 'sdkArtifactRevision': 0, 'sdks': {}, 'runtimes': {}, 'probes': {}, 'metadata': {'stable-pins': pin_record}}
            if 'reuse-plan' in index:
                plan_record = artifact_record(index['reuse-plan'], run)
                saved = json_artifact(self.gh, plan_record, 'reuse-plan.json')
                if saved.get('pins') != pins or saved.get('packagingRevision') != run['head_sha']:
                    raise ValueError('Archived reuse plan frozen pins/producer differs')
                parent_id = saved.get('sourceRunId') or saved.get('reuseRunId')
                parents = {parent_id} if parent_id is not None else set()
                parents.update(o['reuseProvenance']['runId'] for o in saved.get('targets', {}).values() if 'reuseProvenance' in o)
                revision, tag, replacements, records = revision_identity(saved)
                parents.update(r['runId'] for r in records.values())
                parent_nodes = {identifier: self.resolve(identifier) for identifier in parents}
                if revision:
                    if (set(saved.get('targets', {})) != set(TARGETS)
                            or not set(saved.get('rebuildTargets', [])).issubset(replacements)
                            or any('reuseProvenance' not in saved['targets'][target]
                                   for target in TARGETS if target not in replacements)):
                        raise ValueError('Archived SDK revision cannot rebuild non-Mac targets')
                    for target, record in records.items():
                        parent = parent_nodes.get(record.get('runId'))
                        if parent is None or parent['sdks'].get(target) != record:
                            raise ValueError('Archived SDK replacement proof differs from authenticated origin')
                    node.update(sdkArtifactRevision=revision, releaseTag=tag,
                                replacesTargets=replacements, replacementAssets=records)
                for parent in parent_nodes.values():
                    if parent['pins'] != pins: raise ValueError('Reuse graph source pins differ')
                    node['runtimes'].update(parent['runtimes']); node['probes'].update(parent['probes'])
                for target, origin in saved.get('targets', {}).items():
                    if target not in TARGETS: raise ValueError('Unknown archived reuse target')
                    if 'reuseProvenance' in origin:
                        claim = origin['reuseProvenance']; parent = parent_nodes[claim['runId']]
                        record = parent['sdks'].get(target)
                        if (record is None or origin['producerRevision'] != record['producerRevision']
                                or claim['artifactId'] != record['id'] or claim['artifactSha256'] != record['sha256']
                                or claim.get('producerInputHashes') != snapshot_inputs(self.root, record['producerRevision'])):
                            raise ValueError('Archived reuse origin/producer input proof differs')
                        node['sdks'][target] = record
                    elif origin.get('producerRevision') != run['head_sha']:
                        raise ValueError('Archived new target producer differs')
                node['metadata']['reuse-plan'] = plan_record
            for target in TARGETS:
                name = 'v8-' + target
                if name in index:
                    if index[name].get('expired') is True: continue
                    if not successful(jobs, target, target=True): raise ValueError('SDK artifact from unsuccessful build')
                    node['sdks'][target] = artifact_record(index[name], run)
            for name, target in RUNTIME.items():
                if name in index:
                    if index[name].get('expired') is True: continue
                    if not successful(jobs, name): raise ValueError('Runtime artifact from unsuccessful execution')
                    record = artifact_record(index[name], run)
                    sdk = node['sdks'].get(target)
                    if not sdk: raise ValueError('Runtime lacks authenticated SDK origin')
                    record = dict(record, sdkArtifactId=sdk['id'], sdkArtifactSha256=sdk['sha256'])
                    node['runtimes'][name] = record
            for name in list(node['runtimes']):
                sdk = node['sdks'].get(RUNTIME[name]); proof = node['runtimes'][name]
                if not sdk or (proof['sdkArtifactId'], proof['sdkArtifactSha256']) != (sdk['id'], sdk['sha256']):
                    del node['runtimes'][name]
            if 'smoke-probe-linux-arm64' in index and index['smoke-probe-linux-arm64'].get('expired') is not True:
                if not successful(jobs, 'linux-arm64', target=True): raise ValueError('Compiled probe from unsuccessful build')
                sdk = node['sdks'].get('linux-arm64')
                if not sdk: raise ValueError('Compiled probe lacks SDK origin')
                node['probes']['smoke-probe-linux-arm64'] = dict(artifact_record(index['smoke-probe-linux-arm64'], run),
                                sdkArtifactId=sdk['id'], sdkArtifactSha256=sdk['sha256'])
            for name in list(node['probes']):
                sdk=node['sdks'].get('linux-arm64'); probe=node['probes'][name]
                if not sdk or (probe.get('sdkArtifactId'), probe.get('sdkArtifactSha256')) != (sdk['id'],sdk['sha256']):
                    del node['probes'][name]
            self.nodes[run_id] = node
            return node
        finally: self.active.remove(run_id)


def compose_plan(graph, node, root, builder_revision, targets=None, sdk_artifact_revision=0, replaces_targets=None):
    base=json.loads((Path(root)/'tool/v8/pins.json').read_text())
    expected=make_pins(base,{'version':node['pins']['v8']['version'],'revision':node['pins']['v8']['revision'],'tag':'v8-'+node['pins']['v8']['version']})
    if expected != node['pins']: raise ValueError('Frozen source pins differ from current target/depot contracts')
    tag = release_tag(node['pins']['v8']['version'], sdk_artifact_revision)
    replacements = sorted(replaces_targets or [])
    if len(replacements) != len(set(replacements)) or replacements != (list(MAC_REPLACEMENTS) if sdk_artifact_revision else []):
        raise ValueError('SDK revisions must explicitly replace exactly both Mac targets')
    previous_revision = node.get('sdkArtifactRevision', 0)
    if previous_revision > sdk_artifact_revision:
        raise ValueError('Cannot resume an SDK revision as an older release identity')
    sdks = dict(node['sdks'])
    replacement_assets = {}
    if sdk_artifact_revision:
        if previous_revision == sdk_artifact_revision:
            if node.get('replacesTargets') != replacements:
                raise ValueError('Resumed SDK replacement targets differ')
            replacement_assets = node['replacementAssets']
        else:
            if set(sdks) != set(TARGETS):
                raise ValueError('SDK revision requires all ten authenticated baseline SDKs; expired or missing artifacts cannot rebuild silently')
            replacement_assets = {target: sdks.pop(target) for target in replacements}
    missing = [t for t in TARGETS if t not in sdks]
    if sdk_artifact_revision and not set(missing).issubset(replacements):
        raise ValueError('SDK revision cannot rebuild missing non-Mac baseline artifacts')
    if targets is not None and (len(targets)!=len(set(targets)) or set(targets) != set(missing)): raise ValueError('Requested targets must equal missing targets; successful SDKs cannot rebuild')
    recipes = {r['producerRevision']: snapshot_inputs(root, r['producerRevision']) for r in [*sdks.values(), *replacement_assets.values()]}
    packaging = current_recipe(root, builder_revision)
    origins = {}
    for target in TARGETS:
        if target in sdks:
            r = sdks[target]
            origins[target] = {'producerRevision': r['producerRevision'], 'reuseProvenance': {
                'runId': r['runId'], 'artifactId': r['id'], 'artifactSha256': r['sha256'],
                'producerInputHashes': recipes[r['producerRevision']]}}
        else: origins[target] = {'producerRevision': builder_revision, 'producerInputHashes': packaging}
    value = {'schemaVersion': 2, 'sourceRunId': node['run']['id'], 'repository': graph.repository,
            'packagingRevision': builder_revision, 'packagingInputHashes': packaging, 'producerRecipes': recipes,
            'sourceRuns': {str(k): v['run'] for k, v in graph.nodes.items()}, 'pins': node['pins'],
            'metadataAssets': {str(k): v['metadata'] for k, v in graph.nodes.items()},
            'sdkAssets': sdks, 'assets': {'v8-' + t: r for t, r in sdks.items()} | node['probes'],
            'runtimeAssets': node['runtimes'], 'runtimeRequired': [t for n,t in RUNTIME.items() if n not in node['runtimes']],
            'rebuildTargets': missing, 'targets': origins}
    if sdk_artifact_revision:
        value.update(sdkArtifactRevision=sdk_artifact_revision, releaseTag=tag,
                     replacesTargets=replacements, replacementAssets=replacement_assets)
    return value


def generic_plan(gh, root, builder_revision, run_id, targets=None, sdk_artifact_revision=0, replaces_targets=None):
    release_tag('0.0.0', sdk_artifact_revision)
    if run_id == 'auto':
        if sdk_artifact_revision or replaces_targets:
            raise ValueError('SDK revision requires a fixed trusted source run; automatic selection is forbidden')
        base = json.loads((Path(root) / 'tool/v8/pins.json').read_text())
        return auto_plan(gh, root, builder_revision, base['v8']['version'], base['v8']['revision'])
    if isinstance(run_id, str) and re.fullmatch('[1-9][0-9]*', run_id):
        run_id = int(run_id)
    if type(run_id) is not int or run_id <= 0:
        raise ValueError('Positive fixed source run ID required')
    graph = Graph(gh, root, builder_revision)
    return compose_plan(graph, graph.resolve(run_id), root, builder_revision, targets,
                        sdk_artifact_revision, replaces_targets)


def validate_generic_plan(value, pins, builder_revision, root=None):
    root = root or Path(__file__).resolve().parents[1]; validate_pins(pins)
    base=json.loads((Path(root)/'tool/v8/pins.json').read_text())
    if pins != make_pins(base,{'version':pins['v8']['version'],'revision':pins['v8']['revision'],'tag':'v8-'+pins['v8']['version']}):
        raise ValueError('Plan target/depot contracts differ from current recipe')
    if (value.get('schemaVersion') != 2 or value.get('pins') != pins or value.get('packagingRevision') != builder_revision
            or value.get('packagingInputHashes') != current_recipe(root, builder_revision)
            or set(value.get('targets', {})) != set(TARGETS)):
        raise ValueError('Generic plan source/packaging recipe differs')
    revision, tag, replacements, records = revision_identity(value)
    if revision and str(value['sourceRunId']) not in value.get('sourceRuns', {}):
        raise ValueError('SDK revision source run lacks authenticated provenance')
    for target, record in records.items():
        run = value['sourceRuns'].get(str(record.get('runId')))
        if (not run or run['head_sha'] != record.get('producerRevision')
                or not ancestor(root, record['producerRevision'], builder_revision)
                or value['producerRecipes'].get(record['producerRevision']) != snapshot_inputs(root, record['producerRevision'])
                or record.get('name') != 'v8-' + target or type(record.get('id')) is not int
                or not re.fullmatch('[0-9a-f]{64}', record.get('sha256', ''))
                or type(record.get('size')) is not int or record['size'] <= 0):
            raise ValueError('Immutable replacement SDK producer proof differs')
        if value.get('metadataAssets', {}).get(str(record['runId'])) is None:
            raise ValueError('Replacement SDK lacks authenticated source metadata')
    expected_missing = [t for t in TARGETS if t not in value.get('sdkAssets', {})]
    if value.get('rebuildTargets') != expected_missing: raise ValueError('Missing target inventory differs')
    if revision and not set(expected_missing).issubset(replacements):
        raise ValueError('SDK revision cannot rebuild missing non-Mac baseline artifacts')
    for target, origin in value['targets'].items():
        if target in expected_missing:
            if origin != {'producerRevision': builder_revision, 'producerInputHashes': value['packagingInputHashes']}:
                raise ValueError('New SDK producer differs')
        else:
            r = value['sdkAssets'][target]; run = value['sourceRuns'].get(str(r['runId']))
            recipe = snapshot_inputs(root, r['producerRevision'])
            if not run or run['head_sha'] != r['producerRevision'] or not ancestor(root, r['producerRevision'], builder_revision):
                raise ValueError('Immutable SDK source run differs')
            expected = {'producerRevision': r['producerRevision'], 'reuseProvenance': {
                'runId': r['runId'], 'artifactId': r['id'], 'artifactSha256': r['sha256'], 'producerInputHashes': recipe}}
            if origin != expected or value['producerRecipes'].get(r['producerRevision']) != recipe:
                raise ValueError('Actual SDK producer/recipe relabeled')
    for name, proof in value.get('runtimeAssets', {}).items():
        sdk = value['sdkAssets'].get(RUNTIME.get(name))
        if not sdk or (proof.get('sdkArtifactId'), proof.get('sdkArtifactSha256')) != (sdk['id'], sdk['sha256']):
            raise ValueError('Runtime/SDK origin differs')


def check_record(gh, record, value, root):
    run = trusted_run(gh, root, record['runId'], value['packagingRevision'], value['repository'])
    if value['sourceRuns'].get(str(record['runId'])) != run: raise ValueError('Source run changed since planning')
    item = artifact_record(gh.request(gh.base + f"/actions/artifacts/{record['id']}"), run)
    if item != {k:record[k] for k in item}: raise ValueError('Artifact metadata changed since planning')
    return item


def download_generic(gh, value, output, probe_only=False, only_target=None, runtime_name=None, root=None):
    root = root or Path(__file__).resolve().parents[1]
    validate_generic_plan(value, value['pins'], value['packagingRevision'], root)
    if runtime_name:
        if runtime_name not in RUNTIME: raise ValueError('Unknown runtime artifact')
        records = [(runtime_name, value['runtimeAssets'][runtime_name], False)]
    elif probe_only:
        records = [('probe', value['assets']['smoke-probe-linux-arm64'], False)]
    else:
        targets = [only_target] if only_target else list(value['sdkAssets'])
        if any(t not in value['sdkAssets'] for t in targets): raise ValueError('SDK is not reusable')
        records = [('v8-'+t,value['sdkAssets'][t],True) for t in targets]
    for name, record, sdk in records:
        item = check_record(gh, record, value, root)
        destination = Path(output) / name if sdk else Path(output)
        destination.mkdir(parents=True, exist_ok=False)
        extract(archive_bytes(gh,item), destination)
        if sdk:
            target = name[3:]; manifest = json.loads((destination/'manifest.json').read_text())
            if (manifest.get('v8') != value['pins']['v8'] or manifest.get('depotTools') != value['pins']['depotTools']
                    or set(manifest.get('targets',{})) != {target}): raise ValueError('SDK source manifest differs')
            config = value['pins']['targets'][target]; entry=manifest['targets'][target]
            if entry.get('targetConfig') != config and not (target.startswith('ios-') and entry.get('minIOS')==config.get('minIOS') and entry.get('environment')==config.get('environment')):
                raise ValueError('SDK target configuration differs')


def auto_plan(gh, root, builder_revision, version, revision, current_run_id=None):
    graph = Graph(gh, root, builder_revision); best=None; score=(-1,-1)
    expected_pins=make_pins(json.loads((Path(root)/'tool/v8/pins.json').read_text()),{'version':version,'revision':revision,'tag':'v8-'+version})
    runs = pages(gh, gh.base+'/actions/workflows/release.yml/runs?status=completed', 'workflow_runs')
    for run in runs:
        if run['id'] == current_run_id: continue
        if run.get('event') not in ('schedule','workflow_dispatch') or run.get('head_branch') != graph.repository['defaultBranch']: continue
        try: node=graph.resolve(run['id'])
        except ArtifactIntegrityError: raise
        except (ValueError,KeyError,subprocess.CalledProcessError): continue
        if node['pins'] != expected_pins or node.get('sdkArtifactRevision', 0): continue
        candidate=(len(node['sdks']),len(node['runtimes']))
        if candidate>score: best=node;score=candidate
        if score==(len(TARGETS),len(RUNTIME)): break
    if best and score[0]>0: return compose_plan(graph,best,root,builder_revision)
    base=json.loads((Path(root)/'tool/v8/pins.json').read_text())
    pins=make_pins(base,{'version':version,'revision':revision,'tag':'v8-'+version})
    packaging=current_recipe(root,builder_revision)
    return {'schemaVersion':2,'sourceRunId':None,'repository':graph.repository,'packagingRevision':builder_revision,
            'packagingInputHashes':packaging,'producerRecipes':{},'sourceRuns':{},'metadataAssets':{},'pins':pins,
            'sdkAssets':{},'assets':{},'runtimeAssets':{},'runtimeRequired':list(RUNTIME.values()),
            'rebuildTargets':list(TARGETS),'targets':{t:{'producerRevision':builder_revision,'producerInputHashes':packaging} for t in TARGETS}}


validate_plan = validate_generic_plan
plan = generic_plan
download = download_generic


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','auto','download','probe'])
    p.add_argument('--repository',required=True);p.add_argument('--source-run-id','--run-id',dest='run_id',type=int)
    p.add_argument('--plan',type=Path,default=Path('reuse-plan.json'));p.add_argument('--output',type=Path)
    p.add_argument('--builder-revision');p.add_argument('--github-output',type=Path)
    p.add_argument('--sdk-artifact-revision', type=int, default=0);p.add_argument('--replaces-targets', default='');
    p.add_argument('--targets',default='');p.add_argument('--only-target',choices=TARGETS)
    p.add_argument('--runtime-name',choices=RUNTIME);p.add_argument('--probe-only',action='store_true');p.add_argument('--detection',type=Path);p.add_argument('--current-run-id',type=int)
    a=p.parse_args()
    release_tag('0.0.0', a.sdk_artifact_revision)
    if a.action == 'auto' and (a.sdk_artifact_revision or a.replaces_targets):
        raise ValueError('SDK revision requires a fixed trusted source run; automatic selection is forbidden')
    if a.action=='auto':
        detection=json.loads(a.detection.read_text())
        if type(detection.get('should_build')) is not bool: raise ValueError('Explicit detector build decision required')
        if detection['should_build'] is False:
            if a.github_output:
                with a.github_output.open('a') as stream: stream.write('should_build=false\n')
            return
    gh=GitHub(a.repository,os.environ['GH_TOKEN'])
    if a.action in ('plan','auto'):
        if a.action=='auto':
            detection=json.loads(a.detection.read_text());value=auto_plan(gh,Path('.'),a.builder_revision,detection['version'],detection['revision'],a.current_run_id)
        else: value=generic_plan(gh,Path('.'),a.builder_revision,a.run_id,a.targets.split(',') if a.targets else None,a.sdk_artifact_revision,a.replaces_targets.split(',') if a.replaces_targets else None)
        if a.targets and (len(a.targets.split(',')) != len(set(a.targets.split(','))) or set(a.targets.split(',')) != set(value['rebuildTargets'])):
            raise ValueError('Requested targets must equal missing SDKs')
        validate_generic_plan(value,value['pins'],a.builder_revision,root=Path('.'))
        a.plan.write_text(json.dumps(value,indent=2)+'\n');Path('stable-pins.json').write_text(json.dumps(value['pins'],indent=2)+'\n')
        if a.github_output:
            outputs={'should_build':'true','version':value['pins']['v8']['version'],'revision':value['pins']['v8']['revision'],'tag':release_tag(value['pins']['v8']['version'],value.get('sdkArtifactRevision',0)),
                     'sdk_artifact_revision':str(value.get('sdkArtifactRevision',0)),
                     'replaces_targets':','.join(value.get('replacesTargets',[])),
                     'build_count':str(len(value['rebuildTargets'])),'reuse_count':str(len(value['sdkAssets'])),
                     'linux_runtime_required':str('linux-arm64' in value['runtimeRequired']).lower(),
                     'windows_runtime_required':str('windows-arm64' in value['runtimeRequired']).lower(),
                     'linux_sdk_reused':str('linux-arm64' in value['sdkAssets']).lower(),
                     'windows_sdk_reused':str('windows-arm64' in value['sdkAssets']).lower()}
            with a.github_output.open('a') as stream:
                for key,val in outputs.items():stream.write(key+'='+val+'\n')
    else:
        value=json.loads(a.plan.read_text());download_generic(gh,value,a.output,a.action=='probe' or a.probe_only,a.only_target,a.runtime_name)


if __name__=='__main__': main()
