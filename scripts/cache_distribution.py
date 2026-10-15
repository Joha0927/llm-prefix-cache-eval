"""Analyze request-level cache hit rates without loading credentials or calling a model."""
import argparse
import csv
import hashlib
import json
import statistics
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path

DEFAULT_EDGES = [Fraction(i,10) for i in range(10)]+[Fraction(i,100) for i in range(91,100)]+[Fraction(i,1000) for i in (992,994,996,998,1000)]


def summarize(paths, edges=None):
    edges = DEFAULT_EDGES if edges is None else [Fraction(str(x)) for x in edges]
    if len(edges)<2 or edges[0]!=0 or edges[-1]!=1 or any(a>=b for a,b in zip(edges,edges[1:])):
        raise ValueError('Edges must increase strictly from 0 to 1')
    bins=[{'lower':float(a),'upper':float(b),'count':0,'by_task':{}} for a,b in zip(edges,edges[1:])]
    seen={};valid=[];excluded=[];duplicates=0;sources=[]
    for path in map(Path,paths):
        sources.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        with path.open(encoding='utf-8-sig',newline='') as handle:
            for line,row in enumerate(csv.DictReader(handle),2):
                identifier=row.get('attempt_id')
                if not identifier:
                    raise ValueError(f'Missing attempt_id at {path}:{line}')
                if identifier in seen:
                    if seen[identifier]!=row:raise ValueError('Conflicting repeated attempt_id')
                    duplicates+=1;continue
                seen[identifier]=row
                try:
                    p=int(row['prompt_tokens']);c=int(row['cached_tokens'])
                    good=p>0 and 0<=c<=p and row.get('usage_valid')=='True' and row.get('http_status')=='200'
                except (ValueError,KeyError,TypeError):good=False
                if not good:
                    excluded.append({'attempt_id':identifier,'reason':'failed_request_or_invalid_usage'});continue
                rate=Fraction(c,p)
                index=min(bisect_right(edges,rate)-1,len(bins)-1)
                task=row.get('task_id','unknown')
                bins[index]['count']+=1
                bins[index]['by_task'][task]=bins[index]['by_task'].get(task,0)+1
                valid.append({'task_id':task,'run_id':row.get('run_id'),'call_id':row.get('call_id'),
                    'attempt_id':identifier,'prompt_tokens':p,'cached_tokens':c,
                    'api_cache_hit_rate':float(rate),'bin_index':index})
    for bucket in bins:bucket['request_share']=bucket['count']/len(valid) if valid else None
    prompt=sum(r['prompt_tokens'] for r in valid)
    return {'included_requests':len(valid),'unique_attempts':len(seen),'duplicate_copies_skipped':duplicates,
        'excluded_requests':excluded,'usage_coverage':len(valid)/len(seen) if seen else None,
        'bin_rule':'[lower, upper), last bin includes 1; exact rational comparisons',
        'bins':bins,'request_mean_hit_rate':statistics.mean(r['api_cache_hit_rate'] for r in valid) if valid else None,
        'median_hit_rate':statistics.median(r['api_cache_hit_rate'] for r in valid) if valid else None,
        'token_weighted_hit_rate':sum(r['cached_tokens'] for r in valid)/prompt if prompt else None,
        'sources':sources,'requests':valid}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('usage_csv',nargs='+',type=Path)
    parser.add_argument('--edges',nargs='+',help='Optional decimal endpoints from 0 to 1')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    result=summarize(args.usage_csv,args.edges)
    encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded+'\n',encoding='utf-8')
    else:print(encoded)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
