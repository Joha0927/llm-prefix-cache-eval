"""A mirror failure must not abandon supervision of a paid child process."""
import csv
import errno
import json
from pathlib import Path
from types import SimpleNamespace

import audit_run
import dotenv
import live_mirror
import run_eval


def test_runner_finishes_child_after_transient_mirror_failure(tmp_path,monkeypatch):
    polls=[]
    class Process:
        returncode=0
        def __init__(self,command,**kwargs):
            output=Path(command[command.index('--jobs-dir')+1])
            trial=output/'native/mock-trial';requests=trial/'requests'
            requests.mkdir(parents=True)
            (requests/'requests.jsonl').write_text('{"event":"mock"}\n')
            with (requests/'usage.csv').open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['call_id','http_status','usage_valid','estimated_points'])
                writer.writeheader();writer.writerow(dict(call_id='mock',http_status=200,usage_valid=True,estimated_points=1))
            (trial/'result.json').write_text(json.dumps({'trial_name':'mock','verifier_result':{'rewards':{'reward':0}}}))
            (trial/'trace_summary.json').write_text(json.dumps({'exit_status':'Submitted'}))
        def poll(self):
            polls.append(True)
            return None if len(polls)==1 else 0
    original=live_mirror.atomic_copy
    copies=[]
    def copy(source,target):
        copies.append(target)
        if len(copies)==1:
            raise OSError(errno.EIO,'mock transient mirror failure')
        original(source,target)
    monkeypatch.setattr(live_mirror,'atomic_copy',copy)
    monkeypatch.setattr(run_eval.subprocess,'Popen',Process)
    monkeypatch.setattr(run_eval.shutil,'which',lambda name:'/mock/pier')
    monkeypatch.setattr(run_eval.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(dotenv,'dotenv_values',lambda path:{})
    monkeypatch.setattr(audit_run,'audit',lambda output:{'quality_passed':True,'errors':[]})
    task=tmp_path/'example';task.mkdir();(task/'task.toml').write_text('schema_version="1.3"')
    args=SimpleNamespace(output_dir=tmp_path/'runs',experiment_id='mock',credential_file=tmp_path/'unused.env',resume=None,total_points_limit=10)
    result=run_eval.run_one(task,args,10)
    output=Path(result['directory'])
    assert len(polls)==2 and result['status']=='validated'
    assert result['verifier_rewards']=={'reward':0}
    assert json.loads((output/'mirror_errors.jsonl').read_text())['errno']==errno.EIO
    assert json.loads((output/'ARCHIVE_COMPLETE.json').read_text())['hash_verified']
