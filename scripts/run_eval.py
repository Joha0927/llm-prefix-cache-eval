"""Portable sequential Pier runner. Preview by default; model calls require --execute."""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from live_mirror import atomic_copy, mirror_progress

PROJECT = Path(__file__).resolve().parents[1]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def select_tasks(root, names):
    root = root.resolve()
    tasks = [root/name for name in names] if names else sorted(p.parent for p in root.glob('*/task.toml'))
    if not tasks or len(set(tasks)) != len(tasks):
        raise ValueError('Select at least one distinct Pier task')
    for task in tasks:
        if task.parent != root or not re.fullmatch(r'[A-Za-z0-9_-]+', task.name) or not (task/'task.toml').is_file():
            raise ValueError('Task must be an immediate child containing task.toml')
    return tasks


def run_one(task, args, budget):
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = args.output_dir/args.experiment_id/'tasks'/task.name/'runs'/run_id
    output.mkdir(parents=True, exist_ok=False)
    job = output/'native'
    pier = shutil.which('pier')
    if not pier:
        raise RuntimeError('pier executable missing; activate the installed environment')
    command = [pier, 'run', '-p', str(task), '--env', 'docker', '--n-concurrent', '1',
        '--n-attempts', '1', '--max-retries', '0', '--agent-import-path', 'glm_trace.host:HostMiniAgent',
        '--model', 'openai/glm-5.3', '--jobs-dir', str(output), '--job-name', 'native', '--no-delete',
        '--ek', 'keep_containers=true', '--ak', 'credential_file='+str(args.credential_file),
        '--ak', 'experiment_id='+args.experiment_id, '--ak', 'task_id='+task.name,
        '--ak', 'run_id='+run_id, '--ak', 'points_limit='+str(budget)]
    if args.resume:
        command += ['--ak', 'resume_directory='+str(args.resume), '--ak', 'total_points_limit='+str(args.total_points_limit)]
    source = PROJECT/'src/glm_trace'
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.py')}
    shutil.copytree(source, output/'source_snapshot/glm_trace', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(__file__, output/'source_snapshot/run_eval.py')
    atomic_json(output/'config.json', {'command': command, 'source_hashes': hashes,
        'task_toml_sha256':hashlib.sha256((task/'task.toml').read_bytes()).hexdigest(),
        'task_id': task.name, 'run_id': run_id, 'segment_points_limit': budget,
        'total_points_limit': args.total_points_limit, 'resumed': bool(args.resume)})
    atomic_json(output/'status.json', {'status': 'running', 'run_id': run_id})
    environment = {**os.environ, 'PYTHONPATH': str(PROJECT/'src')+os.pathsep+os.environ.get('PYTHONPATH',''),
        'MSWEA_SILENT_STARTUP':'true', 'MSWEA_CONFIGURED':'true',
        'MSWEA_GLOBAL_CONFIG_DIR':str(PROJECT/'.runtime-mini-config'), 'LITELLM_LOCAL_MODEL_COST_MAP':'true'}
    with (output/'console.log').open('w', encoding='utf-8') as console:
        process = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT, env=environment)
        while process.poll() is None:
            # Mirrors are convenience views; failures must not detach the child.
            mirror_progress(job, output, output)
            time.sleep(2)
    for directory in job.glob('*/requests'):
        for name in ('requests.jsonl','usage.csv','checkpoint.json','checkpoint_manifest.json','worktree.tar.gz'):
            if (directory/name).exists():
                atomic_copy(directory/name,output/name)
    trials = []
    for path in job.rglob('result.json'):
        result = json.loads(path.read_text())
        if 'trial_name' in result:
            trials.append(result)
    rows = list(csv.DictReader((output/'usage.csv').open(encoding='utf-8'))) if (output/'usage.csv').exists() else []
    summaries = list(job.rglob('trace_summary.json'))
    exit_status = json.loads(summaries[0].read_text())['exit_status'] if len(summaries)==1 else None
    reward = (trials[0].get('verifier_result') or {}).get('rewards') if len(trials)==1 else None
    errors = []
    if process.returncode or len(trials)!=1 or any(t.get('exception_info') for t in trials):
        errors.append('PierOrEnvironmentFailure')
    if not rows or any(r['http_status']!='200' or r['usage_valid']!='True' for r in rows):
        errors.append('RequestFailureOrMissingUsage')
    if reward is None:
        errors.append('MissingOfficialScore')
    if exit_status!='Submitted':
        errors.append('AgentExit:'+str(exit_status))
    # Keep native and convenience copies sanitized before archiving. Never print the value.
    from dotenv import dotenv_values
    secret = dotenv_values(args.credential_file).get('GLM_API_KEY') or ''
    for path in output.rglob('*'):
        if path.is_file() and secret and secret.encode() in path.read_bytes():
            path.write_bytes(path.read_bytes().replace(secret.encode(),b'[REDACTED]'))
            errors.append('CredentialRedacted')
    status = {'task_id':task.name,'run_id':run_id,'directory':str(output),
        'status':'validated' if not errors else 'attention_required','exit_status':exit_status,
        'model_calls':len({r['call_id'] for r in rows}),
        'estimated_points':sum(float(r['estimated_points'] or 0) for r in rows),
        'verifier_rewards':reward,'errors':errors}
    atomic_json(output/'status.json',status)
    atomic_json(output/'manifest.json',{str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.rglob('*') if p.is_file() and p.name!='manifest.json'})
    if not errors:
        from audit_run import audit
        checked = audit(output)
        status['trace_audit'] = checked
        if not checked['quality_passed']:
            status['status']='attention_required'
            status['errors'].extend(checked['errors'])
    atomic_json(output/'status.json',status)
    final_hashes={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.rglob('*') if p.is_file() and p.name!='manifest.json'}
    atomic_json(output/'manifest.json',final_hashes)
    if any(hashlib.sha256((output/name).read_bytes()).hexdigest()!=digest for name,digest in final_hashes.items()):
        raise RuntimeError('Archive hash verification failed')
    atomic_json(output/'ARCHIVE_COMPLETE.json',{'hash_verified':True})
    return status


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks-root',type=Path,default=Path('third_party/deep-swe/tasks'))
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--task',action='append',help='Repeat for explicitly selected tasks')
    selection.add_argument('--all',action='store_true')
    parser.add_argument('--output-dir',type=Path,default=Path('data/experiments'))
    parser.add_argument('--credential-file',type=Path,default=Path('secrets/glm.env'))
    parser.add_argument('--experiment-id',default='eval')
    parser.add_argument('--points-limit',type=float,default=1500)
    parser.add_argument('--available-points',type=float,help='Confirmed usable allowance; required for execution')
    parser.add_argument('--reserve-points',type=float,default=100)
    parser.add_argument('--total-points-limit',type=float,default=1500)
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    numbers=(args.points_limit,args.total_points_limit,args.reserve_points)
    if any(not math.isfinite(v) for v in numbers) or not re.fullmatch(r'[A-Za-z0-9_-]+',args.experiment_id) or args.points_limit<=0 or args.total_points_limit<=0 or args.reserve_points<0:
        parser.error('Invalid experiment identifier or budget')
    tasks=select_tasks(args.tasks_root,args.task)
    if args.resume and len(tasks)!=1:
        parser.error('Resume requires exactly one task')
    if not args.execute:
        print(json.dumps({'status':'preview_no_model_calls','tasks':[p.name for p in tasks], 'model':'glm-5.3'}))
        return 0
    if args.available_points is None or not math.isfinite(args.available_points) or args.available_points<=args.reserve_points:
        parser.error('Execution requires positive --available-points above the reserve')
    args.credential_file=args.credential_file.resolve()
    if not args.credential_file.is_file():
        parser.error('Credential file missing')
    args.output_dir=args.output_dir.resolve()
    if args.resume:args.resume=args.resume.resolve()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    lock=args.output_dir/(args.experiment_id+'.lock')
    with lock.open('x') as handle:handle.write(str(os.getpid()))
    history=[]
    try:
        remaining=args.available_points
        for task in tasks:
            if remaining-args.reserve_points<args.points_limit:
                print('Stopped between tasks: insufficient estimated reserve.');break
            status=run_one(task,args,args.points_limit)
            history.append(status)
            remaining-=status['estimated_points']
            atomic_json(args.output_dir/(args.experiment_id+'-progress.json'),history)
            print(json.dumps(status,ensure_ascii=False),flush=True)
            if status['status']!='validated':return 1
    finally:
        lock.unlink()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
