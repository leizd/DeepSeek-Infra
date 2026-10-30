"""Read the completed failed creation job; transport never means qualification."""
from __future__ import annotations
import hashlib,io,json,os,zipfile
from typing import Any
import relay_native_reports as common

SOURCE='d94c057ea457d2d3bf920c894bdc7309088b4ad1'
TREE='6aad00fe5791a5123298c810bda030ace931b69b'
RUN=40978927600

def go_events(raw:bytes)->dict[str,Any]:
    assert len(raw)<=16*1024**2
    terminal:dict[str,str]={};runs:dict[str,int]={};packages:dict[str,str]={};non_events=0
    for line in raw.decode('utf8').splitlines():
        try:row=json.loads(line)
        except ValueError:non_events+=1;continue
        assert isinstance(row,dict)
        name,action=row.get('Test'),row.get('Action')
        if isinstance(name,str) and '/' not in name:
            if action=='run':runs[name]=runs.get(name,0)+1
            if action in {'pass','fail','skip'}:terminal[name]=action
        if not name and action in {'pass','fail','skip'}:packages[row['Package']]=action
    return dict(topLevelRuns=runs,topLevelTerminal=terminal,packages=packages,nonJSONLines=non_events,
        rawSha256=hashlib.sha256(raw).hexdigest(),projection='GO_ACTION_COUNTS_ONLY')

def main()->int:
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ['GITHUB_REPOSITORY']==common.REPO
    run=common.api(f'actions/runs/{RUN}')
    assert run['head_sha']==SOURCE and run['path']=='.github/workflows/native-media-create.yml'
    assert run['event']=='push' and run['run_attempt']==1 and run['status']=='completed' and run['conclusion']=='failure'
    assert common.api('git/commits/'+SOURCE)['tree']['sha']==TREE
    listed=common.api(f'actions/runs/{RUN}/artifacts?per_page=100');assert listed['total_count']<=100
    selected=[a for a in listed['artifacts'] if a['name']=='native-media-create-'+SOURCE];assert len(selected)==1
    artifact=selected[0]
    assert not artifact['expired'] and artifact['workflow_run']['head_sha']==SOURCE and artifact['workflow_run']['id']==RUN
    assert 0<artifact['size_in_bytes']<=8*1024**2
    raw=common.fetch(f'https://api.github.com/repos/{common.REPO}/actions/artifacts/{artifact["id"]}/zip',artifact['size_in_bytes'],True)
    assert len(raw)==artifact['size_in_bytes'] and 'sha256:'+hashlib.sha256(raw).hexdigest()==artifact['digest']
    reports={};hashes={}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        common.archive_members(archive)
        for path in ['result.json','process/result.json']:
            entry=archive.getinfo(path);assert 0<entry.file_size<=1024**2
            data=archive.read(entry);value=json.loads(data)
            assert value['sourceCommit']==SOURCE and str(value['runId'])==str(RUN) and value['event']=='push'
            if path=='result.json':assert value['sourceTree']==TREE
            reports[path]=common.sanitize(value);hashes[path]=hashlib.sha256(data).hexdigest()
        entry=archive.getinfo('go-tests.jsonl');assert 0<entry.file_size<=16*1024**2
        reports['go-tests.jsonl.summary']=go_events(archive.read(entry))
    envelope=dict(kind='native-create-report-transport-v1',carrierCommit=os.environ['GITHUB_SHA'],carrierRunId=os.environ['GITHUB_RUN_ID'],
        sourceCommit=SOURCE,sourceTree=TREE,originRunId=RUN,originRunConclusion='failure',artifactId=artifact['id'],artifactDigest=artifact['digest'],
        originalArchiveVerifiedInCloud=True,originalArchiveDownloadedLocally=False,rawReportSha256=hashes,reports=reports,
        runtimeReexecuted=False,runtimeQualificationClaimed=False,compilerCachesOrBinariesRelayed=False)
    pieces=common.notice_chunks(envelope)
    for i,piece in enumerate(pieces,1):
        print(f'::notice file=.github/workflows/native-media-create-report.yml,line=1,title=native-create-report-v1-part-{i}-of-{len(pieces)}::{piece}')
    return 0
if __name__=='__main__':raise SystemExit(main())
