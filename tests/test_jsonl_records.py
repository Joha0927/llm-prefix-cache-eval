import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_run import iter_events, audit


def test_unicode_separators_are_content_not_records(tmp_path):
    path = tmp_path/'requests.jsonl'
    content = 'before\u0085middle\u2028after\u2029end'
    events = [{'event': 'call_start', 'content': content}, {'event': 'call_end'}]
    path.write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in events), encoding='utf-8')
    assert list(iter_events(path)) == events
    (tmp_path/'manifest.json').write_text('{}')
    assert audit(tmp_path)['errors'] == ['no_http_attempts']


def test_real_truncated_record_remains_a_quality_error(tmp_path):
    path = tmp_path/'requests.jsonl'
    path.write_text('{"event":"broken\n', encoding='utf-8')
    with pytest.raises(ValueError, match='requests.jsonl:1'):
        list(iter_events(path))
