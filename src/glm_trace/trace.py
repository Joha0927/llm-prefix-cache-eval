import csv
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


class TraceWriteError(RuntimeError):
    pass


class Trace:
    fields = ["experiment_id", "task_id", "run_id", "session_id", "call_id", "attempt_id", "turn_id",
              "request_start", "request_end", "requested_model", "response_model", "prompt_tokens",
              "cached_tokens", "completion_tokens", "reasoning_tokens", "api_cache_hit_rate",
              "usage_valid", "usage_source", "http_status", "status", "estimated_points"]

    def __init__(self, directory, *, experiment_id, task_id, run_id, secret=""):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)
        self.ids = dict(experiment_id=experiment_id, task_id=task_id, run_id=run_id)
        self.secret = secret
        self.session_id = str(uuid.uuid4())
        self.call_id = None
        self.turn_id = 0
        self.lock = threading.RLock()
        self.rows = {}
        self.stop_event = threading.Event()
        self.fatal_reason = None
        self.recover()
        self.event("session_start")

    def clean(self, x):
        if isinstance(x, str):
            return x.replace(self.secret, "[REDACTED]") if self.secret else x
        if isinstance(x, dict):
            return {k: "[REDACTED]" if k.lower() in {"api_key", "authorization", "cookie"} else self.clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [self.clean(v) for v in x]
        return x

    def recover(self):
        log = self.path / "requests.jsonl"
        if not log.exists():
            return
        data = log.read_bytes()
        boundary = data.rfind(b"\n") + 1
        if boundary < len(data):
            (self.path / ("interrupted_tail_" + uuid.uuid4().hex + ".bin")).write_bytes(data[boundary:])
            with log.open("r+b") as f:
                f.truncate(boundary)
        for line in data[:boundary].splitlines():
            event = json.loads(line)
            self.turn_id = max(self.turn_id, event.get("turn_id") or 0)
            if event.get("event") == "usage_row":
                self.rows[event["row"]["attempt_id"]] = event["row"]
        self.write_csv()

    def event(self, kind, **data):
        with self.lock:
            try:
                payload = {**self.ids, "session_id": self.session_id, "call_id": self.call_id,
                           "turn_id": self.turn_id, "time_utc": datetime.now(timezone.utc).isoformat(),
                           "event": kind, **data}
                with (self.path / "requests.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(self.clean(payload), ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                raise TraceWriteError("Trace persistence failed") from exc

    def begin(self, messages, parameters):
        self.turn_id += 1
        self.call_id = str(uuid.uuid4())
        self.event("call_start", messages=messages, parameters=parameters)

    def write_csv(self):
        temporary = self.path / "usage.csv.tmp"
        try:
            with temporary.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.fields)
                w.writeheader()
                w.writerows(self.clean(list(self.rows.values())))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path / "usage.csv")
        except OSError as exc:
            raise TraceWriteError("Usage persistence failed") from exc

    def complete(self, attempt, start, payload, status, http_status=None):
        usage = payload.get("usage") or {}
        p = usage.get("prompt_tokens")
        c = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        out = usage.get("completion_tokens")
        valid = type(p) is int and p > 0 and type(c) is int and 0 <= c <= p
        points = None
        if valid and type(out) is int and out >= 0:
            local = datetime.fromisoformat(start).astimezone(timezone(timedelta(hours=8)))
            multiplier = 1 if local.weekday() < 5 and 14 <= local.hour < 18 else 0.5
            points = ((p-c)*6.9+c*1.7+out*24)/10000*multiplier
        row = {**self.ids, "session_id": self.session_id, "call_id": self.call_id,
               "attempt_id": attempt, "turn_id": self.turn_id, "request_start": start,
               "request_end": datetime.now(timezone.utc).isoformat(), "requested_model": "glm-5.3",
               "response_model": payload.get("model"), "prompt_tokens": p, "cached_tokens": c,
               "completion_tokens": out, "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
               "api_cache_hit_rate": c/p if valid else None, "usage_valid": valid,
               "usage_source": "provider_http_response", "http_status": http_status, "status": status,
               "estimated_points": points}
        self.event("usage_row", attempt_id=attempt, row=row)
        self.rows[attempt] = row
        self.write_csv()

    @property
    def points(self):
        return sum(r["estimated_points"] or 0 for r in self.rows.values())


class TeeStream(httpx.SyncByteStream):
    def __init__(self, stream, trace, attempt, start, status, headers=None):
        self.stream, self.trace, self.attempt, self.start, self.status = stream, trace, attempt, start, status
        self.headers = headers or {}
        self.buffer = bytearray()
        self.completed = False

    def __iter__(self):
        try:
            for chunk in self.stream:
                self.buffer.extend(chunk)
                yield chunk
            # HTTPTransport exposes encoded bytes; the SDK decodes its own copy.
            # Decode a separate response so forwarded chunks remain byte-identical.
            decoded = httpx.Response(self.status, headers=self.headers, content=bytes(self.buffer))
            raw = decoded.text
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
                for line in raw.splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            part = json.loads(line[6:])
                            if part.get("usage"):
                                payload = part
                        except ValueError:
                            pass
            self.trace.event("wire_response", attempt_id=self.attempt, http_status=self.status,
                             body=payload, body_raw=raw, response_complete=True,
                             content_encoding=self.headers.get("content-encoding"),
                             encoded_body_sha256=hashlib.sha256(self.buffer).hexdigest())
            if self.status >= 400:
                provider_error = payload.get("error") or {}
                error = json.dumps(provider_error, ensure_ascii=False).lower()
                code = str(provider_error.get("code", "")) if isinstance(provider_error, dict) else ""
                if code == "1308" or any(term in error for term in ("quota", "额度", "余额不足", "insufficient_balance")):
                    self.trace.fatal_reason = "QuotaExhausted"
                    self.trace.event("fatal_provider_error", reason="QuotaExhausted", attempt_id=self.attempt)
            self.trace.complete(self.attempt, self.start, payload,
                                "response_received" if self.status < 400 else "api_error_response", self.status)
            self.completed = True
        except BaseException as exc:
            self.trace.event("response_interrupted", attempt_id=self.attempt,
                             partial_body=self.buffer.decode("utf-8", errors="replace"), error_type=type(exc).__name__)
            self.trace.complete(self.attempt, self.start, {}, "response_interrupted", self.status)
            raise

    def close(self):
        if not self.completed:
            self.trace.event("response_closed_incomplete", attempt_id=self.attempt,
                             partial_body=self.buffer.decode("utf-8", errors="replace"))
            self.trace.complete(self.attempt, self.start, {}, "response_closed_incomplete", self.status)
        self.stream.close()


class CaptureTransport(httpx.BaseTransport):
    def __init__(self, trace, inner=None):
        self.trace = trace
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        attempt = str(uuid.uuid4())
        start = datetime.now(timezone.utc).isoformat()
        self.trace.event("wire_request", attempt_id=attempt, url=str(request.url),
                         body=json.loads(request.content), request_sha256=hashlib.sha256(request.content).hexdigest())
        try:
            response = self.inner.handle_request(request)
        except BaseException as exc:
            self.trace.event("wire_error", attempt_id=attempt, error_type=type(exc).__name__, error=str(exc))
            self.trace.complete(attempt, start, {}, "request_failed")
            raise
        consumed = response.is_stream_consumed
        stream = httpx.ByteStream(response.content) if consumed else response.stream
        capture_headers = {} if consumed else response.headers
        headers = dict(response.headers)
        if consumed:
            # Pre-buffered transports have already decoded response.content.
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
        return httpx.Response(response.status_code, headers=headers,
                              stream=TeeStream(stream, self.trace, attempt, start, response.status_code, capture_headers),
                              extensions=response.extensions, request=request)

    def close(self):
        self.inner.close()
