import csv
import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cache_distribution import summarize
from run_eval import main, select_tasks


def test_preview_never_requires_credentials(tmp_path,capsys):
    task=tmp_path/'example';task.mkdir();(task/'task.toml').write_text('schema_version="1.3"')
    assert main(['--tasks-root',str(tmp_path),'--task','example'])==0
    assert 'preview_no_model_calls' in capsys.readouterr().out


def test_execution_requires_allowance_before_credentials(tmp_path):
    task=tmp_path/'example';task.mkdir();(task/'task.toml').write_text('')
    with pytest.raises(SystemExit):main(['--tasks-root',str(tmp_path),'--task','example','--execute'])


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):select_tasks(tmp_path,['../outside'])


def make_csv(path,rows):
    fields=['attempt_id','task_id','run_id','prompt_tokens','cached_tokens','usage_valid','http_status']
    with path.open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def test_bins_boundaries_dedup_and_missing(tmp_path):
    source=tmp_path/'usage.csv'
    rows=[{'attempt_id':str(i),'task_id':'test','run_id':'r','prompt_tokens':1000,
        'cached_tokens':c,'usage_valid':'True','http_status':'200'} for i,c in enumerate((0,100,900,990,992,994,996,998,1000))]
    rows.append({'attempt_id':'error','task_id':'test','run_id':'r','prompt_tokens':'',
        'cached_tokens':'','usage_valid':'False','http_status':'429'})
    make_csv(source,rows)
    result=summarize([source,source])
    assert result['included_requests']==9 and result['unique_attempts']==10
    assert result['duplicate_copies_skipped']==10 and result['usage_coverage']==.9
    by_lower={b['lower']:b['count'] for b in result['bins']}
    assert by_lower[.996]==1 and by_lower[.998]==2
    assert sum(b['count'] for b in result['bins'])==9


def test_conflicting_duplicate_rejected(tmp_path):
    first=tmp_path/'first.csv';second=tmp_path/'second.csv'
    row={'attempt_id':'a','task_id':'t','run_id':'r','prompt_tokens':1000,'cached_tokens':990,
         'usage_valid':'True','http_status':'200'}
    make_csv(first,[row]);make_csv(second,[{**row,'cached_tokens':995}])
    with pytest.raises(ValueError,match='Conflicting'):summarize([first,second])
