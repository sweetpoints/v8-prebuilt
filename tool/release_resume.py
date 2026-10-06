#!/usr/bin/env python3
"""Resume the reviewed immutable nine-target build without recompiling it."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile
from release_package import TARGETS
from release_publish import GitHub
from release_pins import make_pins

RUN_ID = 37449592798
PRODUCER = 'fe19c5cbcc978b174b4f98959c9e95d0186dc219'
PUBLISH_RUN = 37484248844
ARM_PRODUCER = '9255ce1737836b044a84bd68b8d529ab9f640c86'
PUBLISH_LOCK = {
 'v8-windows-arm64': (11429647799,'c49146eb38dc1a4ceccedbcacd7e5271dd74fb54ee1ce4a63ab6c4e88443984c',64873474),
 'linux-arm-runtime': (11430007112,'b835f6b6b361f500956b6cd68a54fd01bcced160e393c3684096f9a84382dcd5',23281114),
 'windows-arm-runtime': (11429109991,'4ed2babe27604bac662cb17cc7453e0813681f93cc231e7ac165eb9d9e3cb213',16230443),
}
INPUTS = ('tool/v8/build.py', 'tool/v8/desktop.py', 'tool/v8/ios.py', 'tool/sdk_smoke.py', 'tool/v8/pins.json')
LOCK = {
 'v8-android-x64': (11413282146, '120af5b360f2d54a42c8ba7b8e48905b91e881e80c22d1766b5bcddb3df3fc90',64800113),
 'v8-macos-arm64': (11413100558,'8756385202659aa01e7e3270daf200c5987d48a14cc2d3d19847fb8c72831d21',58138529),
 'smoke-probe-linux-arm64': (11412995798,'cb6a4462fc74e9ca614241cd0319b4f5820ccf6b5a8a76470f5b75b0e55287bb',23279318),
 'v8-android-arm64': (11412731477,'df4c2514d4402af6b42dd23cc87d22463b505d1a4305ea0879603c50f68106cf',64302565),
 'v8-macos-x64': (11412544788,'11f2970546dcfd826f04ca6ef2634b2b3659b46b74863fbd0f25f3a58cafa107',58703433),
 'v8-ios-simulator-arm64': (11412298094,'6e5cec8eaab8855068e60aaa56356412539f1df2d4032b7b243a345cf1dc8af6',26279916),
 'v8-windows-x64': (11412251930,'19242d27ded2f1bc21e76753b8233d22709c91777e8a99e92927f21a08beea43',66735040),
 'v8-linux-arm64': (11412181074,'50ab0d5cb2f93014964bf4d64cfccfe55bef579c9810e5b52cce884550e87a89',38265615),
 'v8-ios-arm64': (11411322780,'63432712163cdb1c27844602f9091059d0d01cc402267730399da5b2d013b3b1',25966521),
 'v8-linux-x64': (11410642088,'cdeb6b867e445fbdb8182f81fd366070de1f421fe6023b6f9c19384be14a922c',61687871),
 'stable-pins': (11406980088,'cc6dd554314269c8fd327e0974511d1e815f03dc009a9383f1613624276d8635',544),
}

def sha(data): return hashlib.sha256(data).hexdigest()
def producer_inputs(root,revision=PRODUCER):
    result = {}
    for name in INPUTS:
        old = subprocess.run(['git','show',revision+':'+name],cwd=root,check=True,capture_output=True).stdout
        current = (root/name).read_bytes()
        if old != current: raise ValueError('Reused producer input changed: '+name)
        result[name] = sha(current)
    return result

def pages(gh, url, key):
    values=[]; page=1
    while True:
        payload=gh.request(url+f'?per_page=100&page={page}'); part=payload[key]; values.extend(part)
        if len(part)<100:return values
        page+=1

def check_metadata(item,name,lock=None,run_id=RUN_ID,producer=PRODUCER):
    identifier,checksum,size=(LOCK if lock is None else lock)[name]
    origin=item.get('workflow_run',{})
    if (item.get('id')!=identifier or item.get('name')!=name or item.get('digest')!='sha256:'+checksum
        or item.get('size_in_bytes')!=size or item.get('expired') is not False
        or origin.get('id')!=run_id or origin.get('head_sha')!=producer):
        raise ValueError('Immutable reused artifact identity differs: '+name)
    return {'id':identifier,'name':name,'sha256':checksum,'size':size}

def archive_bytes(gh,item):
    data=gh.request(gh.base+f"/actions/artifacts/{item['id']}/zip",content_type='application/octet-stream',accept='application/vnd.github+json')
    if len(data)!=item['size'] or sha(data)!=item['sha256']:raise ValueError('Reused ZIP digest/size differs')
    return data

def extract(data,destination):
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        seen=set(); total=0
        for item in bundle.infolist():
            name=item.filename; path=PurePosixPath(name.rstrip('/'))
            if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts or not path.parts
                or path.as_posix()!=name.rstrip('/') or name in seen or stat.S_ISLNK(item.external_attr>>16)):
                raise ValueError('Unsafe reused ZIP member')
            seen.add(name); total+=item.file_size
            if total>2_000_000_000:raise ValueError('Reused ZIP payload too large')
            target=destination.joinpath(*path.parts)
            if item.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:stream.write(bundle.read(item))

def plan(gh,root,builder_revision):
    if not re.fullmatch('[0-9a-f]{40}',builder_revision):raise ValueError('Full packaging revision required')
    run=gh.request(gh.base+f'/actions/runs/{RUN_ID}')
    if run['head_sha']!=PRODUCER or run['status']!='completed' or run['run_attempt']!=1:
        raise ValueError('Reviewed reused run identity differs')
    jobs=pages(gh,gh.base+f'/actions/runs/{RUN_ID}/jobs','jobs')
    reused=set(TARGETS)-{'windows-arm64'}
    for target in reused:
        matched=[job for job in jobs if job['name'].startswith('build ('+target+',')]
        if len(matched)!=1 or matched[0]['conclusion']!='success':raise ValueError('Reused target build did not succeed: '+target)
    hashes=producer_inputs(root)
    assets={}
    for name in LOCK:
        item=gh.request(gh.base+f'/actions/artifacts/{LOCK[name][0]}')
        assets[name]=check_metadata(item,name)
    with zipfile.ZipFile(io.BytesIO(archive_bytes(gh,assets['stable-pins']))) as bundle:
        if bundle.namelist()!=['stable-pins.json']:raise ValueError('Ambiguous frozen source pins')
        pins=json.loads(bundle.read('stable-pins.json'))
    detection={'version':'15.4.80.25','revision':'c45871fec706a6e7b715e607065bb4578b23ce9f','tag':'v8-15.4.80.25'}
    if pins!=make_pins(json.loads((root/'tool/v8/pins.json').read_text()),detection):raise ValueError('Frozen pins or target configurations differ')
    result={'schemaVersion':1,'reuseRunId':RUN_ID,'producerRevision':PRODUCER,'packagingRevision':builder_revision,
            'producerInputHashes':hashes,'pins':pins,'assets':assets,'targets':{}}
    for target in TARGETS:
        origin={'producerRevision':builder_revision}
        if target in reused:
            origin={'producerRevision':PRODUCER,'reuseProvenance':{'runId':RUN_ID,'artifactId':assets['v8-'+target]['id'],
                      'artifactSha256':assets['v8-'+target]['sha256'],'producerInputHashes':hashes}}
        result['targets'][target]=origin
    return result

def download(gh,plan_value,output,probe_only=False,only_target=None):
    if plan_value['reuseRunId']!=RUN_ID or plan_value['producerRevision']!=PRODUCER:raise ValueError('Reuse plan identity differs')
    names=['smoke-probe-linux-arm64'] if probe_only else ['v8-'+target for target in TARGETS if target!='windows-arm64']
    if only_target is not None:
        if probe_only or only_target=='windows-arm64' or only_target not in TARGETS:raise ValueError('Target is not reusable')
        names=['v8-'+only_target]
    for name in names:
        item=check_metadata(gh.request(gh.base+f'/actions/artifacts/{LOCK[name][0]}'),name)
        if item!=plan_value['assets'][name]:raise ValueError('Reuse plan artifact changed')
        destination=output if probe_only else output/name
        destination.mkdir(parents=True,exist_ok=False)
        extract(archive_bytes(gh,item),destination)
        if not probe_only:
            target=name.removeprefix('v8-');manifest=json.loads((destination/'manifest.json').read_text())
            if (manifest['v8']!=plan_value['pins']['v8'] or manifest['depotTools']!=plan_value['pins']['depotTools']
                or set(manifest['targets'])!={target}):raise ValueError('Reused SDK provenance differs')
            entry=manifest['targets'][target];config=plan_value['pins']['targets'][target]
            if target.startswith('ios-'):
                if entry['minIOS']!=config['minIOS'] or entry['environment']!=config['environment']:raise ValueError('Reused iOS target config differs')
            elif entry.get('targetConfig')!=config:raise ValueError('Reused target configuration differs')

def validate_plan(value,pins,builder_revision):
    if value.get('publishOnlySourceRunId') not in (None,PUBLISH_RUN):raise ValueError('Unreviewed publish-only run')
    if value.get('publishOnlySourceRunId')==PUBLISH_RUN:
        for name,(identifier,checksum,size) in PUBLISH_LOCK.items():
            if value.get('publishAssets',{}).get(name)!={'id':identifier,'name':name,'sha256':checksum,'size':size}:
                raise ValueError('Publish-only artifact lock differs')
    if (value.get('schemaVersion')!=1 or value.get('reuseRunId')!=RUN_ID or value.get('producerRevision')!=PRODUCER
        or value.get('packagingRevision')!=builder_revision or value.get('pins')!=pins
        or set(value.get('targets',{}))!=set(TARGETS) or set(value.get('producerInputHashes',{}))!=set(INPUTS)):
        raise ValueError('Packaging reuse provenance differs')
    if value['producerInputHashes']!=producer_inputs(Path(__file__).resolve().parents[1]):raise ValueError('Producer inputs no longer match verified reuse plan')
    for target,origin in value['targets'].items():
        if target=='windows-arm64':
            expected={'producerRevision':builder_revision}
            if value.get('publishOnlySourceRunId')==PUBLISH_RUN:
                asset=PUBLISH_LOCK['v8-windows-arm64']
                expected={'producerRevision':ARM_PRODUCER,'reuseProvenance':{'runId':PUBLISH_RUN,'artifactId':asset[0],
                          'artifactSha256':asset[1],'producerInputHashes':value['producerInputHashes']}}
                if producer_inputs(Path(__file__).resolve().parents[1],ARM_PRODUCER)!=value['producerInputHashes']:
                    raise ValueError('Windows producer inputs no longer match')
            if origin!=expected:raise ValueError('New target producer provenance differs')
        else:
            asset=LOCK['v8-'+target]
            expected={'producerRevision':PRODUCER,'reuseProvenance':{'runId':RUN_ID,'artifactId':asset[0],
                      'artifactSha256':asset[1],'producerInputHashes':value['producerInputHashes']}}
            if origin!=expected:raise ValueError('Reused target producer provenance differs')

def publish_plan(gh,root,builder_revision):
    value=plan(gh,root,builder_revision)
    if producer_inputs(root,ARM_PRODUCER)!=value['producerInputHashes']:raise ValueError('Windows producer recipe changed')
    run=gh.request(gh.base+f'/actions/runs/{PUBLISH_RUN}')
    if run['head_sha']!=ARM_PRODUCER or run['status']!='completed' or run['run_attempt']!=1:
        raise ValueError('Publish-only source run identity differs')
    jobs=pages(gh,gh.base+f'/actions/runs/{PUBLISH_RUN}/jobs','jobs')
    for name in ('detect','reuse','windows-arm-runtime','linux-arm-runtime'):
        selected=[job for job in jobs if job['name']==name]
        if len(selected)!=1 or selected[0]['conclusion']!='success':raise ValueError('Reusable runtime stage did not succeed')
    build=[job for job in jobs if job['name'].startswith('build (windows-arm64,')]
    if len(build)!=1 or build[0]['conclusion']!='success':raise ValueError('Reusable Windows SDK did not succeed')
    value['publishOnlySourceRunId']=PUBLISH_RUN;value['publishAssets']={}
    for name,item in PUBLISH_LOCK.items():
        value['publishAssets'][name]=check_metadata(gh.request(gh.base+f'/actions/artifacts/{item[0]}'),name,PUBLISH_LOCK,PUBLISH_RUN,ARM_PRODUCER)
    item=PUBLISH_LOCK['v8-windows-arm64']
    value['targets']['windows-arm64']={'producerRevision':ARM_PRODUCER,'reuseProvenance':{
        'runId':PUBLISH_RUN,'artifactId':item[0],'artifactSha256':item[1],'producerInputHashes':value['producerInputHashes']}}
    return value

def download_publish(gh,value,output):
    validate_plan(value,value['pins'],value['packagingRevision'])
    download(gh,value,output)
    for name,item in PUBLISH_LOCK.items():
        metadata=check_metadata(gh.request(gh.base+f'/actions/artifacts/{item[0]}'),name,PUBLISH_LOCK,PUBLISH_RUN,ARM_PRODUCER)
        if metadata!=value['publishAssets'][name]:raise ValueError('Publish-only artifact differs')
        destination=output/name;destination.mkdir()
        extract(archive_bytes(gh,metadata),destination)
    target='windows-arm64';manifest=json.loads((output/'v8-windows-arm64/manifest.json').read_text())
    if (manifest['v8']!=value['pins']['v8'] or manifest['depotTools']!=value['pins']['depotTools']
        or set(manifest['targets'])!={target} or manifest['targets'][target].get('targetConfig')!=value['pins']['targets'][target]):
        raise ValueError('Published Windows SDK source/config differs')
    import shutil
    for target,runtime in [('linux-arm64','linux-arm-runtime'),('windows-arm64','windows-arm-runtime')]:
        directory=output/('v8-'+target)/target
        shutil.copyfile(output/runtime/'sdk-smoke.json',directory/'sdk-smoke.json')
        (directory/'validation').mkdir(exist_ok=True)
        for path in (output/runtime/'validation').glob('sdk-probe*'):
            shutil.copyfile(path,directory/'validation'/path.name)
        shutil.rmtree(output/runtime)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','download','probe','publish-plan','publish-download'])
    p.add_argument('--repository',required=True);p.add_argument('--run-id',type=int,required=True)
    p.add_argument('--plan',type=Path,default=Path('reuse-plan.json'));p.add_argument('--output',type=Path)
    p.add_argument('--builder-revision');p.add_argument('--github-output',type=Path);p.add_argument('--only-target',choices=TARGETS);a=p.parse_args()
    if a.run_id!=(PUBLISH_RUN if a.action.startswith('publish-') else RUN_ID):raise ValueError('Only the reviewed immutable resume run is supported')
    gh=GitHub(a.repository,os.environ['GH_TOKEN'])
    if a.action in ('plan','publish-plan'):
        value=(publish_plan if a.action=='publish-plan' else plan)(gh,Path('.'),a.builder_revision);a.plan.write_text(json.dumps(value,indent=2)+'\n')
        Path('stable-pins.json').write_text(json.dumps(value['pins'],indent=2)+'\n')
        if a.github_output:
            with a.github_output.open('a') as stream:
                for key,val in {'should_build':'true','version':value['pins']['v8']['version'],'revision':value['pins']['v8']['revision'],'tag':'v8-'+value['pins']['v8']['version']}.items():stream.write(key+'='+val+'\n')
    elif a.action=='publish-download':download_publish(gh,json.loads(a.plan.read_text()),a.output)
    else:download(gh,json.loads(a.plan.read_text()),a.output,a.action=='probe',a.only_target)
