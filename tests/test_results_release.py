"""Offline export preserves full payloads, provenance and failure exclusions."""
import csv
import json
from pathlib import Path

import pytest
from build_results_release import export_requests, sha, summary


def segment(root,run,status=200):
    path=root/'data/raw/experiments/Full/tasks/task/runs'/run
    path.mkdir(parents=True)
    content='unicode separators: \u2028 and \u2029'
    events=[{'event':'wire_request','attempt_id':'attempt','call_id':'call','turn_id':1,
             'time_utc':'2026-10-17T00:00:00Z','url':'https://example.invalid',
             'body':{'messages':[{'role':'user','content':content}]},'request_sha256':'recorded-input-hash'},
            {'event':'wire_response','attempt_id':'attempt','call_id':'call','time_utc':'2026-10-17T00:00:01Z',
             'http_status':status,'body':{'usage':{'prompt_tokens':10,'prompt_tokens_details':{'cached_tokens':5}}},
             'response_complete':True},
            {'event':'native_response','call_id':'call','message':{'role':'assistant','content':'answer'}},
            {'event':'call_end','call_id':'call'}]
    (path/'requests.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events),encoding='utf8')
    with (path/'usage.csv').open('w',newline='') as handle:
        w=csv.DictWriter(handle,fieldnames=['attempt_id','http_status','usage_valid'])
        w.writeheader();w.writerow({'attempt_id':'attempt','http_status':status,'usage_valid':status==200})
    (path/'manifest.json').write_text(json.dumps({n:sha(path/n) for n in ['requests.jsonl','usage.csv']}))
    return {'task_id':'task','run_id':run,'sha256':sha(path/'requests.jsonl')},path


def test_export_retains_payload_and_namespaces_attempts_by_source(tmp_path):
    first,p1=segment(tmp_path,'first');second,p2=segment(tmp_path,'second')
    out=tmp_path/'output';out.mkdir()
    original=[p.read_bytes() for p in [p1/'requests.jsonl',p2/'requests.jsonl']]
    provenance,n=export_requests(out,'task',[first,second],
        [{'task_id':'task','run_id':r,'attempt_id':'attempt'} for r in ['first','second']],tmp_path,{})
    records=[json.loads(line) for line in (out/'requests.jsonl').open(encoding='utf8')]
    assert n==2 and len(provenance)==2
    assert [r['source_run_id'] for r in records]==['first','second']
    assert records[0]['request']['body']['messages'][0]['content']=='unicode separators: \u2028 and \u2029'
    assert records[0]['agent_response']['content']=='answer'
    assert original==[p.read_bytes() for p in [p1/'requests.jsonl',p2/'requests.jsonl']]


def test_failed_attempt_retained_but_not_in_valid_population(tmp_path):
    source,_=segment(tmp_path,'failed',429);out=tmp_path/'output';out.mkdir()
    _,n=export_requests(out,'task',[source],[],tmp_path,{})
    assert n==1
    record=json.loads((out/'requests.jsonl').read_text(encoding='utf8'))
    assert record['response']['http_status']==429 and 'agent_response' not in record


def test_changed_source_hash_blocks_export(tmp_path):
    source,path=segment(tmp_path,'run');source['sha256']='0'*64
    out=tmp_path/'output';out.mkdir()
    with pytest.raises(ValueError,match='hash mismatch'):
        export_requests(out,'task',[source],[{'task_id':'task','run_id':'run','attempt_id':'attempt'}],tmp_path,{})


def test_no_http_parent_needs_no_invented_usage(tmp_path):
    parent=tmp_path/'data/raw/experiments/Full/tasks/task/runs/paused';parent.mkdir(parents=True)
    (parent/'requests.jsonl').write_text('{"event":"call_start","call_id":"blocked"}\n')
    digest=sha(parent/'requests.jsonl')
    (parent/'manifest.json').write_text(json.dumps({'requests.jsonl':digest}))
    child,_=segment(tmp_path,'child');out=tmp_path/'output';out.mkdir()
    sources=[{'task_id':'task','run_id':'paused','sha256':digest},child]
    provenance,n=export_requests(out,'task',sources,[{'task_id':'task','run_id':'child','attempt_id':'attempt'}],tmp_path,{})
    assert n==1 and provenance[0]['usage_sha256'] is None
    assert provenance[0]['http_attempts']==0
