import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_run import audit


def test_no_http_pause_is_not_fake_usage_or_file_error(tmp_path):
    (tmp_path / "requests.jsonl").write_text(json.dumps({"event": "call_start"})+'\n')
    (tmp_path / "manifest.json").write_text('{}')
    result = audit(tmp_path)
    assert result["errors"] == ["no_http_attempts"]
    assert not result["quality_passed"]
    assert result["http_attempts"] == 0
    assert result["token_weighted_cache_hit_rate"] is None
    assert not (tmp_path / "usage.csv").exists()


def test_lost_usage_is_explicit_collection_error(tmp_path):
    (tmp_path / "requests.jsonl").write_text(json.dumps({"event": "wire_request", "attempt_id": "test"})+'\n')
    (tmp_path / "manifest.json").write_text('{}')
    result = audit(tmp_path)
    assert "missing_usage_csv" in result["errors"]
    assert "attempt_coverage" in result["errors"]
    assert not result["quality_passed"]
