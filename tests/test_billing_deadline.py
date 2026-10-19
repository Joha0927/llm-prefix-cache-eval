from datetime import datetime, timedelta, timezone

import pytest
from minisweagent.exceptions import LimitsExceeded
from test_collector import trace, model, MESSAGES


def test_deadline_blocks_before_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_EVAL_DEADLINE_UTC", (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    t = trace(tmp_path)
    with pytest.raises(LimitsExceeded) as caught:
        model(t, lambda request: pytest.fail("Expired request reached transport")).query(MESSAGES)
    assert caught.value.messages[0]["extra"]["exit_status"] == "BillingDeadline"
    assert not t.rows


def test_deadline_checkpoint_is_pausable():
    from glm_trace.checkpoint import PAUSABLE
    assert "BillingDeadline" in PAUSABLE


def test_future_deadline_allows_request(tmp_path, monkeypatch):
    import httpx
    from test_collector import response
    monkeypatch.setenv("GLM_EVAL_DEADLINE_UTC", (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
    t = trace(tmp_path)
    model(t, lambda request: httpx.Response(200, json=response())).query(MESSAGES)
    assert len(t.rows) == 1
