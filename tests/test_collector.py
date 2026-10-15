import json
import os

import httpx
import openai
import pytest
from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel

from glm_trace.model import TracedModel
from glm_trace.trace import CaptureTransport, Trace, TraceWriteError


@pytest.mark.parametrize("provider_error", [
    {"code": "insufficient_quota", "message": "Quota exhausted"},
    {"code": "1308", "message": "已达到 5 小时的使用上限。您的限额将在 2026-10-14 23:30:50 重置。"},
])
def test_quota_error_stops_without_retry(tmp_path, provider_error):
    from minisweagent.exceptions import LimitsExceeded
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(429, json={"error": provider_error})
    t = trace(tmp_path)
    with pytest.raises(LimitsExceeded):
        model(t, handler).query(MESSAGES)
    assert len(seen) == 1 and t.fatal_reason == "QuotaExhausted"
    assert len(t.rows) == 1


def test_cancelled_model_never_calls_transport(tmp_path):
    from minisweagent.exceptions import LimitsExceeded
    t = trace(tmp_path)
    t.stop_event.set()
    with pytest.raises(LimitsExceeded):
        model(t, lambda request: pytest.fail("Cancelled request reached network")).query(MESSAGES)


def test_points_limit_blocks_a_billed_error_retry(tmp_path, monkeypatch):
    from minisweagent.exceptions import LimitsExceeded
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "2")
    t = trace(tmp_path)
    t.points_limit = 0.001
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(500, json={"error": {"message": "server error"}, "usage": response()["usage"]})
    with pytest.raises(LimitsExceeded):
        model(t, handler).query(MESSAGES)
    assert len(seen) == 1 and t.points > t.points_limit


def test_bridge_submission_timeout_and_no_host_secrets(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from minisweagent.exceptions import Submitted, LimitsExceeded
    from glm_trace.host import Bridge
    monkeypatch.setenv("PRIVATE_HOST_KEY", "should-not-enter-container")
    t = trace(tmp_path)
    class Fake:
        async def exec(self, command, **kwargs):
            assert "PRIVATE_HOST_KEY" not in kwargs["env"]
            assert "should-not-enter-container" not in command
            assert kwargs["timeout_sec"] == 40
            return SimpleNamespace(stdout="COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nDone" if "echo COMPLETE" in command else "partial", stderr="", return_code=0 if "echo COMPLETE" in command else 124)
    async def run():
        bridge = Bridge(Fake(), asyncio.get_running_loop(), t, {"PAGER": "cat"})
        assert "PRIVATE_HOST_KEY" not in bridge.get_template_vars()
        with pytest.raises(Submitted):
            await asyncio.to_thread(bridge.execute, {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
        result = await asyncio.to_thread(bridge.execute, {"command": "sleep 100"})
        assert result["returncode"] == -1 and result["output"] == "partial"
        t.stop_event.set()
        with pytest.raises(LimitsExceeded):
            await asyncio.to_thread(bridge.execute, {"command": "pwd"})
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_host_failure_and_cancellation_persist_and_stop(tmp_path, monkeypatch, cancel):
    import asyncio
    import time
    from types import SimpleNamespace
    import glm_trace.host as host
    from pier.models.agent.context import AgentContext
    seen = []
    def handler(request):
        seen.append(request)
        time.sleep(0.1)
        return httpx.Response(200, json=response()) if cancel else httpx.Response(403, json={"error": {"message": "denied"}})
    def factory(**kwargs):
        return TracedModel(inner_transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(host, "TracedModel", factory)
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"GLM_API_KEY": "test-secret-key", "GLM_API_BASE": PARAMS["api_base"]})
    class FakeEnvironment:
        async def exec(self, *args, **kwargs):
            pytest.fail("Cancelled or failed model must not execute tools")
    async def run():
        agent = host.HostMiniAgent(logs_dir=tmp_path/"agent", credential_file="mock.env", experiment_id="mock", task_id="test", run_id="run")
        ctx = AgentContext()
        task = asyncio.create_task(agent.run("A mock task", FakeEnvironment(), ctx))
        if cancel:
            while not seen:
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            assert ctx.metadata and "agent_failure" in ctx.metadata
        assert len(seen) == (1 if cancel else 5)
        assert (tmp_path/"agent/trajectory.json").exists()
        summary = json.loads((tmp_path/"agent/trace_summary.json").read_text())
        assert summary["exit_status"] == "TaskCancelled" if cancel else summary["exit_status"] != "Submitted"
        assert "test-secret-key" not in (tmp_path/"agent/mini-swe-agent.trajectory.json").read_text()
    asyncio.run(run())


PARAMS = {"api_base": "https://open.bigmodel.cn/api/coding/paas/v4", "temperature": 0.95, "top_p": 1.0,
          "max_tokens": 64, "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
          "max_retries": 0, "num_retries": 0}
MESSAGES = [{"role": "system", "content": "Test context"}, {"role": "user", "content": "Return a bash call", "extra": {"metadata": 1}}]


def test_gzip_stream_preserves_sdk_response_and_usage(tmp_path):
    import gzip
    raw = json.dumps(response()).encode()
    encoded = gzip.compress(raw)
    class Encoded(httpx.SyncByteStream):
        def __iter__(self):
            yield encoded[:12]
            yield encoded[12:]
    t = trace(tmp_path)
    observed = model(t, lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Encoded())).query(MESSAGES)
    assert observed["extra"]["response"]["usage"]["prompt_tokens"] == 100
    assert list(t.rows.values())[0]["cached_tokens"] == 64
    events = list(map(json.loads,(tmp_path/"requests.jsonl").read_text().splitlines()))
    wire = next(e for e in events if e["event"] == "wire_response")
    assert wire["content_encoding"] == "gzip" and wire["body_raw"] == raw.decode()


def response(cached=64, tools=True):
    usage = {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    message = {"role": "assistant", "content": "response", "reasoning_content": "thinking"}
    if tools:
        message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}]
    return {"id": "test", "object": "chat.completion", "model": "glm-5.3", "created": 1,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if tools else "stop", "message": message}], "usage": usage}


def trace(tmp_path):
    return Trace(tmp_path, experiment_id="mock", task_id="test", run_id="run", secret="test-secret-key")


def model(t, handler):
    return TracedModel(trace=t, api_key="test-secret-key", inner_transport=httpx.MockTransport(handler),
                       model_name="openai/glm-5.3", model_kwargs=PARAMS, cost_tracking="ignore_errors")


def test_behavior_and_wire_capture(tmp_path):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    sdk = openai.OpenAI(api_key="test-secret-key", base_url=PARAMS["api_base"], max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    class Plain(LitellmModel):
        def _query(self, messages, **kwargs):
            return super()._query(messages, client=sdk, **kwargs)
    plain = Plain(model_name="openai/glm-5.3", model_kwargs=PARAMS, cost_tracking="ignore_errors").query(MESSAGES)
    t = trace(tmp_path)
    observed = model(t, handler).query(MESSAGES)
    assert bodies[0] == bodies[1]
    assert observed["extra"]["response"] == plain["extra"]["response"]
    assert observed["extra"]["actions"] == plain["extra"]["actions"]
    assert list(t.rows.values())[0]["cached_tokens"] == 64
    assert "test-secret-key" not in (tmp_path / "requests.jsonl").read_text()


def test_retries_are_distinct_and_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "2")
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={"error": {"message": "temporary", "type": "server_error"}}) if len(calls) == 1 else httpx.Response(200, json=response())
    t = trace(tmp_path)
    model(t, handler).query(MESSAGES)
    rows = list(t.rows.values())
    assert len(rows) == 2 and rows[0]["call_id"] == rows[1]["call_id"]
    assert rows[0]["attempt_id"] != rows[1]["attempt_id"]
    assert rows[0]["prompt_tokens"] is None and rows[1]["prompt_tokens"] == 100
    assert calls[0] == calls[1]


def test_format_error_still_preserves_usage_and_response(tmp_path):
    t = trace(tmp_path)
    with pytest.raises(FormatError):
        model(t, lambda r: httpx.Response(200, json=response(tools=False))).query(MESSAGES)
    assert len(t.rows) == 1 and list(t.rows.values())[0]["completion_tokens"] == 15
    events = [json.loads(l) for l in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert any(e["event"] == "call_error" and e["messages"][0]["extra"]["response"]["usage"]["prompt_tokens"] == 100 for e in events)


def test_missing_usage_and_recovery(tmp_path):
    t = trace(tmp_path)
    model(t, lambda r: httpx.Response(200, json=response(cached=None))).query(MESSAGES)
    row = next(iter(t.rows.values()))
    assert row["cached_tokens"] is None and row["api_cache_hit_rate"] is None and not row["usage_valid"]
    with (tmp_path / "requests.jsonl").open("ab") as f:
        f.write(b'{"unfinished":')
    resumed = trace(tmp_path)
    assert resumed.rows == t.rows and resumed.session_id != t.session_id
    assert list(tmp_path.glob("interrupted_tail_*.bin"))


def test_write_failure_prevents_network(tmp_path, monkeypatch):
    t = trace(tmp_path)
    calls = []
    m = model(t, lambda r: calls.append(r))
    def fail(*a, **k):
        raise TraceWriteError("disk full")
    monkeypatch.setattr(t, "event", fail)
    with pytest.raises(TraceWriteError):
        m.query(MESSAGES)
    assert not calls


def test_timeout_record(tmp_path, monkeypatch):
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "1")
    def fail(request):
        raise httpx.ReadTimeout("timeout", request=request)
    t = trace(tmp_path)
    with pytest.raises(Exception):
        model(t, fail).query(MESSAGES)
    assert len(t.rows) == 1 and next(iter(t.rows.values()))["status"] == "request_failed"


def test_sse_chunks_and_usage(tmp_path):
    chunks = [b'data: {"choices":[]}\n\n', b'data: {"model":"glm-5.3","usage":{"prompt_tokens":100,"completion_tokens":2,"prompt_tokens_details":{"cached_tokens":64}}}\n\ndata: [DONE]\n\n']
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks
    t = trace(tmp_path)
    t.begin([], {})
    with httpx.Client(transport=CaptureTransport(t, httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())))) as client:
        with client.stream("POST", "https://test.example/chat", json={}) as r:
            assert b"".join(r.iter_bytes()) == b"".join(chunks)
    assert next(iter(t.rows.values()))["cached_tokens"] == 64


def test_cancelled_stream_saved(tmp_path):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'partial response'
            raise KeyboardInterrupt()
    t = trace(tmp_path)
    t.begin([], {})
    with httpx.Client(transport=CaptureTransport(t, httpx.MockTransport(lambda r: httpx.Response(200, stream=Interrupted())))) as client:
        with pytest.raises(KeyboardInterrupt):
            client.post("https://test.example/chat", json={})
    assert len(t.rows) == 1
    assert next(iter(t.rows.values()))["status"] in {"response_interrupted", "response_closed_incomplete"}
    assert "partial response" in (tmp_path / "requests.jsonl").read_text()
