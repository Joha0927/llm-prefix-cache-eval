import httpx
import openai
import os
from datetime import datetime, timezone
from pathlib import Path

from minisweagent.exceptions import LimitsExceeded
from minisweagent.models.litellm_model import LitellmModel

from glm_trace.trace import CaptureTransport, TraceWriteError


class TracedModel(LitellmModel):
    def __init__(self, *, trace, api_key, inner_transport=None, **kwargs):
        super().__init__(**kwargs)
        self.trace = trace
        self.abort_exceptions = self.abort_exceptions + [TraceWriteError, LimitsExceeded]
        self.http = httpx.Client(transport=CaptureTransport(trace, inner_transport), timeout=self.config.model_kwargs.get("timeout", 120))
        self.client = openai.OpenAI(api_key=api_key, base_url=self.config.model_kwargs["api_base"],
                                    max_retries=self.config.model_kwargs.get("max_retries", 0), http_client=self.http)

    def _query(self, messages, **kwargs):
        reason = "TaskCancelled" if self.trace.stop_event.is_set() else self.trace.fatal_reason
        shared_stop = os.environ.get("GLM_SHARED_STOP_FILE")
        if not reason and shared_stop and Path(shared_stop).exists():
            reason = "CoordinatorPause"
        deadline = os.environ.get("GLM_EVAL_DEADLINE_UTC")
        if deadline and datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
            reason = "BillingDeadline"
        if self.trace.points >= getattr(self.trace, "points_limit", float("inf")):
            reason = "TaskPointsLimit"
        if any(r["http_status"] == 200 and (not r["usage_valid"] or r["estimated_points"] is None) for r in self.trace.rows.values()):
            reason = "UsageIncomplete"
        if any((r["prompt_tokens"] or 0) > 400000 for r in self.trace.rows.values()):
            reason = "ObservedContextLimit"
        if reason:
            raise LimitsExceeded({"role": "exit", "content": reason,
                                  "extra": {"exit_status": reason, "submission": ""}})
        try:
            return super()._query(messages, client=self.client, **kwargs)
        except Exception:
            if self.trace.fatal_reason:
                reason = self.trace.fatal_reason
                raise LimitsExceeded({"role": "exit", "content": reason,
                                      "extra": {"exit_status": reason, "submission": ""}}) from None
            raise

    def query(self, messages, **kwargs):
        self.trace.begin(messages, self.config.model_kwargs | kwargs)
        try:
            result = super().query(messages, **kwargs)
            self.trace.event("native_response", message=result)
            return result
        except BaseException as exc:
            self.trace.event("call_error", error_type=type(exc).__name__, error=str(exc),
                             messages=getattr(exc, "messages", None))
            raise
        finally:
            self.trace.event("call_end")

    def close(self):
        self.http.close()
