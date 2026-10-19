"""Offline, read-only export of accepted task chains into a final results release.

No model, Docker, credential or collector imports. Original archives are immutable.
Run with --help for explicit input paths; output directories must be new.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import zipfile


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def final_directory(root,entry):
    if entry.get('source_directory'):
        return root/entry['source_directory']
    return root/'data/raw/experiments'/entry['stage']/'tasks'/entry['task_id']/'runs'/entry['run_id']


def segment_directory(root,entry,source):
    path=root/'data/raw/experiments/Full/tasks'/source['task_id']/'runs'/source['run_id']
    if path.is_dir():
        return path
    path=final_directory(root,entry)
    if path.name!=source['run_id']:
        raise ValueError('Cannot resolve source segment')
    return path


def one(paths,label):
    paths=list(paths)
    if len(paths)!=1:
        raise ValueError(f'Expected one {label}, found {len(paths)}')
    return paths[0]


def export_requests(directory,task,sources,expected_rows,root,entry):
    """One record per actual HTTP attempt, retaining exact JSON request/response.

    call_start/body_raw/native duplicates are not duplicated in the export.
    Corresponding normalized mini response is retained for successful attempts.
    """
    usage=[];fields=[];provenance=[];seen=set();valid=set();attempt_count=0
    with (directory/'requests.jsonl').open('w',encoding='utf8',newline='\n') as output:
        for source in sources:
            segment=segment_directory(root,entry,source)
            manifest=load(segment/'manifest.json');raw=segment/'requests.jsonl'
            usage_path=segment/'usage.csv'
            usage_sha=None;segment_rows=[]
            if usage_path.exists():
                usage_sha=sha(usage_path)
                if usage_sha!=manifest.get('usage.csv'):
                    raise ValueError('Source usage manifest mismatch')
                with usage_path.open(encoding='utf8',newline='') as handle:
                    reader=csv.DictReader(handle);segment_rows=list(reader)
                    fields.extend(f for f in reader.fieldnames if f not in fields)
            elif 'usage.csv' in manifest:
                raise ValueError('Archived Usage file is missing')
            by_id={r['attempt_id']:r for r in segment_rows}
            if len(by_id)!=len(segment_rows):
                raise ValueError('Duplicate source attempt')
            digest=hashlib.sha256();pending={};completed=defaultdict(list);segment_seen=set()
            def flush(call_id):
                nonlocal attempt_count
                for record in completed.pop(call_id,[]):
                    row=by_id[record['attempt_id']]
                    record['usage']=row
                    output.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n')
                    usage.append(row);attempt_count+=1
                    if row['http_status']=='200' and row['usage_valid']=='True':
                        valid.add((task,source['run_id'],record['attempt_id']))
            with raw.open('rb') as handle:
                for number,line in enumerate(handle,1):
                    digest.update(line);event=json.loads(line)
                    kind=event.get('event');aid=event.get('attempt_id');call=event.get('call_id')
                    if kind=='wire_request':
                        identity=(task,source['run_id'],aid)
                        if identity in seen:
                            raise ValueError('Repeated actual request')
                        seen.add(identity);segment_seen.add(aid)
                        pending[aid]={'task_id':task,'source_run_id':source['run_id'],'call_id':call,
                            'attempt_id':aid,'turn_id':event.get('turn_id'),
                            'request':{'time_utc':event['time_utc'],'url':event['url'],
                                       'body':event['body'],'sha256':event['request_sha256']},
                            'source_record_lines':{'request':number}}
                    elif kind in ('wire_response','wire_error'):
                        record=pending.pop(aid)
                        record['response']={'time_utc':event['time_utc'],'http_status':event.get('http_status'),
                                            'body':event.get('body'),'complete':event.get('response_complete',False)}
                        if kind=='wire_error':
                            record['response'].update(error_type=event.get('error_type'),error=event.get('error'))
                        record['source_record_lines']['response']=number
                        completed[call].append(record)
                    elif kind=='native_response':
                        for record in completed.get(call,[]):
                            if record['response']['http_status']==200:
                                record['agent_response']=event['message']
                    elif kind=='call_end':
                        flush(call)
            for call in list(completed):
                flush(call)
            if pending or segment_seen!=set(by_id):
                raise ValueError('Incomplete original request coverage')
            if digest.hexdigest()!=source['sha256'] or digest.hexdigest()!=manifest['requests.jsonl']:
                raise ValueError('Source request hash mismatch')
            provenance.append({'run_id':source['run_id'],'original_directory':str(segment.relative_to(root)),
                'requests_sha256':digest.hexdigest(),'usage_sha256':usage_sha,
                'http_attempts':len(segment_rows),'cohort':source.get('cohort')})
    if valid!={(r['task_id'],r['run_id'],r['attempt_id']) for r in expected_rows}:
        raise ValueError('Export does not match frozen valid request population')
    with (directory/'usage.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(usage)
    return provenance,attempt_count


def export_task(root,release,entry,sources,rows,condition):
    task=entry['task_id'];directory=release/'tasks'/task;directory.mkdir(parents=True,exist_ok=True)
    final=final_directory(root,entry)
    trajectory=one((final/'native').glob('*/agent/mini-swe-agent.trajectory.json'),'native trajectory')
    native=load(trajectory);messages=native['messages']
    checkpoint=final/'checkpoint.json'
    if checkpoint.exists():
        state=load(checkpoint)
        if state['messages']!=messages[:len(state['messages'])]:
            raise ValueError('Final conversation differs from complete checkpoint history')
        logical_calls=state['n_calls']
    else:
        logical_calls=entry['model_calls']
    save(directory/'conversation.json',{'task_id':task,'messages':messages,
        'source_format':native.get('trajectory_format'),'source_sha256':sha(trajectory),
        'note':'Exact final native mini-SWE-agent message list including inherited history; no concatenation or inferred messages.'})
    provenance,attempts=export_requests(directory,task,sources,rows,root,entry)
    scoring=final
    if entry.get('verifier_recovery_run_id'):
        scoring=root/'data/raw/experiments/VerifierRecovery/tasks'/task/'runs'/entry['verifier_recovery_run_id']
    patch=one((scoring/'native').glob('*/artifacts/model.patch'),'final submitted patch')
    score_manifest=load(scoring/'manifest.json')
    patch_key=str(patch.relative_to(scoring)).replace('\\','/')
    expected_patch={k.replace('\\','/'):v for k,v in score_manifest.items()}.get(patch_key)
    if expected_patch!=sha(patch):
        raise ValueError('Final patch differs from its accepted scoring archive')
    shutil.copyfile(patch,directory/'submission.patch')
    n=len(rows);prompt=sum(r['api_prompt_tokens'] for r in rows)
    result={'task_id':task,'final_run_id':entry['run_id'],'status':entry['status'],
        'reward':entry['verifier_rewards']['reward'],'verifier_rewards':entry['verifier_rewards'],
        'task_condition':condition,'first_submission_comparable':entry.get('first_submission_comparable',True),
        'successful_requests':n,'http_attempts':attempts,'logical_calls':logical_calls,
        'prompt_tokens':prompt,'cached_tokens':sum(r['api_hit_tokens'] for r in rows),
        'prompt_median':statistics.median(r['api_prompt_tokens'] for r in rows),
        'api_request_mean':statistics.mean(r['api_rate'] for r in rows),
        'api_token_weighted':sum(r['api_hit_tokens'] for r in rows)/prompt,
        'block64_request_mean':statistics.mean(r['prompt_only_64_rate'] for r in rows),
        'block64_token_weighted':sum(r['prompt_only_64_hits'] for r in rows)/prompt,
        'message_count':len(messages),'source_cohorts':list(dict.fromkeys(r['source_cohort'] for r in rows)),
        'conversation':'conversation.json','requests':'requests.jsonl','usage':'usage.csv','patch':'submission.patch'}
    save(directory/'result.json',result)
    save(directory/'provenance.json',{'task_id':task,'final_index_entry':entry,
        'conversation_source':str(trajectory.relative_to(root)), 'conversation_source_sha256':sha(trajectory),
        'patch_source':str(patch.relative_to(root)),'patch_sha256':sha(patch),'segments':provenance,
        'scoring_archive':str(scoring.relative_to(root)),'scoring_manifest_sha256':sha(scoring/'manifest.json'),
        'source_unchanged':True,'source_of_official_score':'Frozen accepted index; scoring recovery, when used, is identified by its entry.'})
    return result


def summary(data,tasks):
    rows=data['detail'];prompt=sum(r['api_prompt_tokens'] for r in rows)
    edges=data['edges'];bins={key:[0]*(len(edges)-1) for key in ['api_rate','prompt_only_64_rate']}
    for r in rows:
        for key,counts in bins.items():
            index=next((i for i in range(len(edges)-1) if r[key]<edges[i+1]),len(edges)-2)
            counts[index]+=1
    groups=[]
    for k in [8,16,32,64,128,256]:
        lo=int(k*1024*.8+0.999999);hi=int(k*1024*1.2)
        subset=[r for r in rows if lo<=r['api_prompt_tokens']<=hi]
        counts={key:[0]*(len(edges)-1) for key in bins}
        for r in subset:
            for key,values in counts.items():
                i=next((i for i in range(len(edges)-1) if r[key]<edges[i+1]),len(edges)-2);values[i]+=1
        groups.append({'label':f'{k}K','lower':lo,'upper':hi,'n':len(subset),'counts':counts})
    n=len(rows)
    return {'tasks':len(tasks),'successful_requests':n,'http_attempts':sum(t['http_attempts'] for t in tasks),
        'reward_one':sum(t['reward']==1 for t in tasks),'reward_zero':sum(t['reward']==0 for t in tasks),
        'pass_rate':sum(t['reward']==1 for t in tasks)/len(tasks),
        'prompt_median':statistics.median(r['api_prompt_tokens'] for r in rows),
        'api_request_mean':statistics.mean(r['api_rate'] for r in rows),
        'api_token_weighted':sum(r['api_hit_tokens'] for r in rows)/prompt,
        'block64_request_mean':statistics.mean(r['prompt_only_64_rate'] for r in rows),
        'block64_token_weighted':sum(r['prompt_only_64_hits'] for r in rows)/prompt,
        'mean_logical_calls':statistics.mean(t['logical_calls'] for t in tasks),
        'conditions':dict(Counter(t['task_condition'] for t in tasks)),
        'as_of':data['as_of'],'edges':edges,'bins':bins,'length_groups':groups}


def manifest(directory):
    items={}
    for p in sorted(directory.rglob('*')):
        if p.is_file() and p.name not in {'manifest.json','BUILDING.json'}:
            items[str(p.relative_to(directory)).replace('\\','/')]={'sha256':sha(p),'bytes':p.stat().st_size}
    save(directory/'manifest.json',{'schema_version':1,'files':items})
    return items


def make_readme(release,count,summary_data):
    (release/'README.md').write_text(f'''# GLM-5.3 正式结果：{count}项

本目录为冻结实验结果的离线整理副本，不启动模型、Docker或评分。
完整任务{count}项，有效请求{summary_data['successful_requests']}次。
任务目录以task_id组织，不需要处理Run或checkpoint。

- result.json：官方评分、统计指标、实验条件。
- conversation.json：最终原生mini-SWE-agent轨迹中的完整messages，保留真实历史与修复反馈。
- requests.jsonl：每个实际HTTP Attempt一行，包含完整request/response、Usage和可获得的原生Agent响应。
- usage.csv：同一有效链中全部Attempt的原Usage行，含失败；统计时只选HTTP200且usage_valid=True。
- submission.patch：最终提交补丁。
- provenance.json：旧Run和原始Hash映射，仅用于审计，不是日常使用入口。

请求日志中的body_raw、call_start等重复副本不重复导出；可从provenance定位不可变原档。
conversation不是逐请求messages直接拼接；它来自最终累计轨迹，并与可用checkpoint逐项校验。
{'本113项版本的最后两项含公开诊断/架构辅助，不与首次独立提交成绩混称。' if count==113 else '本111项冻结版本不包含最后两项公开辅助修复任务。'}旧扩预算条件保留。
正常reward0仍是完整评测，完整不表示每项功能测试都通过。
不包含凭据、任务容器、模型权重或可执行checkpoint。文件仅本地使用，默认不上传GitHub。
''',encoding='utf8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--legacy-data',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--legacy-output',type=Path,required=True)
    parser.add_argument('--artifacts-dir',type=Path,required=True)
    parser.add_argument('--resume',action='store_true',help='Continue an unfinished export, never a completed release')
    parser.add_argument('--dashboard-template',type=Path,default=Path(__file__).with_name('templates')/'results_dashboard.html')
    args=parser.parse_args();root=args.root.resolve();release=args.output.resolve();legacy=args.legacy_output.resolve()
    data=load(args.data);entries=load(args.index);old=load(args.legacy_data)
    if data['tasks']!=113 or old['tasks']!=111 or len(entries)!=113:
        raise ValueError('Unexpected frozen population')
    if sha(args.index)!=data['population_sha256']:
        raise ValueError('Frozen index identity mismatch')
    if release.exists() or legacy.exists():
        if not args.resume or not (release/'BUILDING.json').exists():
            raise FileExistsError('Refuse to overwrite an existing result release')
    release.mkdir(parents=True,exist_ok=True);legacy.mkdir(parents=True,exist_ok=True)
    progress=root/'reports/results_release_progress.json'
    save(release/'BUILDING.json',{'status':'building','population_sha256':data['population_sha256']})
    by_task=defaultdict(list);sources=defaultdict(list)
    for r in data['detail']:by_task[r['task_id']].append(r)
    for s in data['sources']:sources[s['task_id']].append(s)
    conditions={t['task']:t['task_condition'] for t in data['task_rows']}
    tasks=[]
    for entry in sorted(entries,key=lambda e:e['task_id']):
        if entry['status'] not in ('validated','validated_recovered') or entry['stop_sequence']:
            raise ValueError('Incomplete task in formal index')
        result=export_task(root,release,entry,sources[entry['task_id']],by_task[entry['task_id']],conditions[entry['task_id']])
        tasks.append(result)
        save(progress,{'status':'exporting','tasks':len(tasks),'task':entry['task_id'],
            'successful_requests':sum(t['successful_requests'] for t in tasks)})
        print(json.dumps({'exported':len(tasks),'task':entry['task_id'],'requests':result['successful_requests']}),flush=True)
    overview=summary(data,tasks)
    assert overview['successful_requests']==13723 and overview['reward_one']==81 and overview['reward_zero']==32
    save(release/'summary.json',overview)
    save(release/'experiment.json',{'model':'GLM-5.3','agent':'mini-SWE-agent 2.4.6','harness':'Pier 0.3.1 (host-side adapter)',
        'dataset':'DeepSWE v1.1, fixed 113 tasks','frozen_population_sha256':data['population_sha256'],
        'tokenizer_revision':data['tokenizer_revision'],'exported_at_utc':datetime.now(timezone.utc).isoformat(),
        'statistics_scope':'All successful attempts in accepted task chains, source cohorts retained; no repeated SDK/native Usage.',
        'api_metric':'cached_tokens / prompt_tokens from Provider HTTP Usage',
        'theoretical_metric':'Per-task historical input cache, unlimited capacity and TTL; not observed provider cache.',
        'conditions':overview['conditions'],'source_code_sha256':sha(Path(__file__))})
    fields=['task_id','reward','task_condition','first_submission_comparable','successful_requests','http_attempts','logical_calls',
            'prompt_median','api_request_mean','api_token_weighted','block64_token_weighted','message_count']
    with (release/'task_index.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        w=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(tasks)
    make_readme(release,113,overview)
    # Freeze all original 111-task products independently of the formal release.
    old_ids={t['task'] for t in old['task_rows']}
    old_tasks=[t for t in tasks if t['task_id'] in old_ids]
    for task in old_tasks:
        source=release/'tasks'/task['task_id'];target=legacy/'tasks'/task['task_id'];target.mkdir(parents=True)
        for p in source.iterdir():
            # Hard links are between derived read-only exports, never raw archives.
            try:os.link(p,target/p.name)
            except OSError:shutil.copyfile(p,target/p.name)
    old_overview=summary(old,old_tasks);assert old_overview['successful_requests']==13188
    save(legacy/'summary.json',old_overview)
    save(legacy/'formal_index_snapshot.json',load(Path(args.legacy_data).with_name('formal_index_snapshot.json')))
    make_readme(legacy,111,old_overview)
    artifacts=args.artifacts_dir.resolve()
    for count,target in [(111,legacy),(113,release)]:
        folder=target/'statistics';folder.mkdir()
        for p in sorted(artifacts.glob(f'GLM53_{count}任务_*.xlsx')):shutil.copyfile(p,folder/p.name)
        source_data=args.legacy_data if count==111 else args.data
        shutil.copyfile(source_data,folder/'workbook_data.json')
    plots=legacy/'figures';plots.mkdir()
    for p in (artifacts/'cache_111_3d').iterdir():
        if p.is_file() and p.suffix in {'.png','.pdf','.svg','.py','.csv'}:shutil.copyfile(p,plots/p.name)
    figures=release/'figures';figures.mkdir()
    qa=Path(args.data).parent/'qa'
    for p in qa.glob('*.png'):shutil.copyfile(p,figures/p.name)
    # Self-contained HTML uses metadata only. Conversations stay in local files.
    length_edges=[0,8192,16384,32768,65536,131072,262144,float('inf')]
    length_histogram=[sum(lo<=r['api_prompt_tokens']<hi for r in data['detail'])
                      for lo,hi in zip(length_edges,length_edges[1:])]
    payload={'summary':overview,'tasks':tasks,'release':release.name,'length_histogram':length_histogram}
    encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
    template=args.dashboard_template.read_text(encoding='utf8')
    dashboard=template.replace('__RESULTS_DATA__',encoded)
    (release.parent/'index.html').write_text(dashboard,encoding='utf8')
    save(progress,{'status':'hashing_release','tasks':113,'successful_requests':13723})
    release_manifest=manifest(release);legacy_manifest=manifest(legacy)
    # Hash manifests include content files; BUILDING marker is removed only at success.
    (release/'BUILDING.json').unlink()
    archive=legacy.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
        for p in sorted(legacy.rglob('*')):
            if p.is_file():z.write(p,str(p.relative_to(legacy.parent)))
    save(root/'reports/results_release_receipt.json',{'status':'finished','formal_tasks':113,'legacy_tasks':111,
        'formal_requests':13723,'legacy_requests':13188,'formal_files':len(release_manifest),
        'legacy_files':len(legacy_manifest),'legacy_zip':str(archive.relative_to(root)),
        'legacy_zip_sha256':sha(archive),'legacy_zip_bytes':archive.stat().st_size,
        'dashboard':str((release.parent/'index.html').relative_to(root)),
        'original_archives_modified':False,'new_model_calls':0})
    save(progress,{'status':'finished','tasks':113,'successful_requests':13723})
    print('Release, legacy directory, ZIP and dashboard complete.',flush=True)


if __name__=='__main__':
    main()
