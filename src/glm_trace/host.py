import asyncio
import json
import os
import platform
import shlex
import uuid
import hashlib
import math
from pathlib import Path

import yaml
from dotenv import dotenv_values
from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import LimitsExceeded
from pier.agents.base import BaseAgent
from pier.agents.installed.mini_swe_agent import convert_and_save_trajectory, populate_context_from_final_metrics

from glm_trace.model import TracedModel
from glm_trace.trace import Trace
from glm_trace.checkpoint import save_checkpoint, continue_agent, load_checkpoint
from glm_trace.workspace_archive import archive_workspace


def points_allowance(value):
    """Explicit unlimited opt-in; finite legacy budgets retain their meaning."""
    if value == 'unlimited':
        return float('inf')
    amount=float(value)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('Invalid points allowance')
    return amount


class Bridge(LocalEnvironment):
    def __init__(self, environment, loop, trace, settings):
        super().__init__(cwd="/app", env=settings)
        self.environment, self.loop, self.trace = environment, loop, trace

    def execute(self, action, cwd="", *, timeout=None):
        if self.trace.stop_event.is_set():
            raise LimitsExceeded({"role": "exit", "content": "TaskCancelled",
                                  "extra": {"exit_status": "TaskCancelled", "submission": ""}})
        self.trace.event("tool_start", action=action)
        try:
            seconds = timeout or self.config.timeout
            command = "timeout --kill-after=5s " + str(seconds) + "s bash -c " + shlex.quote(action.get("command", "")) + " 2>&1"
            request = self.environment.exec(command, cwd=cwd or self.config.cwd,
                                            env=self.config.env, timeout_sec=seconds + 10)
            result = asyncio.run_coroutine_threadsafe(request, self.loop).result()
            output = {"output": (result.stdout or "") + (result.stderr or ""),
                      "returncode": result.return_code, "exception_info": ""}
            if result.return_code in (124, 137):
                output.update(returncode=-1, exception_info="Command exceeded execution timeout",
                              extra={"exception_type": "TimeoutExpired"})
        except Exception as exc:
            output = {"output": "", "returncode": -1, "exception_info": str(exc),
                      "extra": {"exception_type": type(exc).__name__}}
        self.trace.event("tool_end", output=output)
        self._check_finished(output)
        return output

    def get_template_vars(self, **kwargs):
        return {**self.config.model_dump(), **platform.uname()._asdict(), **kwargs}


class GuardedAgent(InteractiveAgent):
    def __init__(self, *args, trace, points_limit, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace, self.points_limit = trace, points_limit

    def serialize(self, *extra):
        return self.trace.clean(super().serialize(*extra))

    def query(self):
        save_checkpoint(self)
        reason = None
        if self.trace.stop_event.is_set():
            reason = "TaskCancelled"
        if self.trace.points >= self.points_limit:
            reason = "TaskPointsLimit"
        if any(r["http_status"] == 200 and not r["usage_valid"] for r in self.trace.rows.values()):
            reason = "UsageIncomplete"
        if any((r["prompt_tokens"] or 0) > 400000 for r in self.trace.rows.values()):
            reason = "ObservedContextLimit"
        if reason:
            self.trace.event("limit_reached", reason=reason)
            raise LimitsExceeded({"role": "exit", "content": reason,
                                  "extra": {"exit_status": reason, "submission": ""}})
        return super().query()


class HostMiniAgent(BaseAgent):
    SUPPORTS_ATIF = True

    def __init__(self, *, credential_file, experiment_id, task_id, run_id, points_limit=1500,
                 max_tokens=16384, resume_directory=None, total_points_limit=1500, **kwargs):
        super().__init__(**kwargs)
        self.credential_file = Path(credential_file)
        self.ids = dict(experiment_id=experiment_id, task_id=task_id, run_id=run_id)
        self.points_limit, self.max_tokens = points_allowance(points_limit), int(max_tokens)
        self.resume_directory = Path(resume_directory) if resume_directory else None
        self.total_points_limit = points_allowance(total_points_limit)

    @staticmethod
    def name():
        return "mini-swe-agent-host"

    def version(self):
        return "2.4.6-host-adapter-v9-parallel-pause"

    async def setup(self, environment):
        result = await environment.exec("test ! -e /solution && test ! -e /tests && test ! -e /var/run/docker.sock", timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Task isolation check failed")

    async def run(self, instruction, environment, context):
        credentials = dotenv_values(self.credential_file)
        key = credentials.get("GLM_API_KEY") or ""
        base = (credentials.get("GLM_API_BASE") or "").rstrip("/")
        if not key or base != "https://open.bigmodel.cn/api/coding/paas/v4":
            raise RuntimeError("Missing credential or unexpected endpoint")
        trace = Trace(self.logs_dir.parent / "requests", secret=key, **self.ids)
        trace.points_limit = self.points_limit
        resume_state = None
        original_environment = environment
        if self.resume_directory:
            from glm_trace.retained import RetainedEnvironment
            resume_state, manifest = load_checkpoint(self.resume_directory, self.ids["task_id"])
            environment = await RetainedEnvironment.attach(manifest["container_id"])
            trace.prior_points = resume_state["cumulative_points"]
            trace.points_limit = min(self.points_limit, self.total_points_limit - trace.prior_points)
            trace.event("resume_from", parent_run_id=resume_state["ids"]["run_id"],
                        cumulative_points=trace.prior_points, n_calls=resume_state["n_calls"])
        os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "5"
        from minisweagent import package_dir
        config = yaml.safe_load((package_dir / "config/mini.yaml").read_text())
        parameters = {"api_base": base, "temperature": 0.95, "top_p": 1.0, "max_tokens": self.max_tokens,
                      "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
                      "num_retries": 0, "max_retries": 0, "timeout": 600, "drop_params": False}
        model_cfg = dict(config["model"])
        model_cfg.pop("model_kwargs", None)
        model = TracedModel(trace=trace, api_key=key, model_name="openai/glm-5.3", model_kwargs=parameters,
                            cost_tracking="ignore_errors", **model_cfg)
        bridge = Bridge(environment, asyncio.get_running_loop(), trace, config["environment"]["env"])
        bridge.original_environment = original_environment
        agent_cfg = dict(config["agent"])
        agent_cfg.update(mode="yolo", confirm_exit=False, step_limit=400, cost_limit=0,
                         wall_time_limit_seconds=10800, output_path=self.logs_dir / "mini-swe-agent.trajectory.json")
        agent = GuardedAgent(model, bridge, trace=trace, points_limit=trace.points_limit, **agent_cfg)
        worker = asyncio.create_task(asyncio.to_thread(
            continue_agent, agent, resume_state, instruction) if resume_state else asyncio.to_thread(agent.run, instruction))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            trace.stop_event.set()
            trace.event("task_cancel_requested")
            # Keep the event loop/container alive while the bounded active request finishes.
            # Subsequent model retries and tools are blocked by the cancellation flag.
            try:
                await asyncio.shield(worker)
            except Exception as exc:
                trace.event("cancel_worker_error", error_type=type(exc).__name__)
            raise
        except Exception as exc:
            # Native Agent already persisted its failure. Return so Pier can still
            # collect the existing committed patch and run its independent verifier.
            trace.event("agent_failure", error_type=type(exc).__name__, error=str(exc))
            context.metadata = {"agent_failure": type(exc).__name__}
        finally:
            agent.save(agent.config.output_path)
            model.close()
            trace.event("task_model_end", estimated_points=trace.points)
            checkpoint = save_checkpoint(agent)
            if checkpoint["resume_allowed"] or self.resume_directory:
                archive_path = trace.path / "worktree.tar.gz"
                await archive_workspace(environment, archive_path, trace)
                identity = await environment.exec("cat /etc/hostname", timeout_sec=30)
                from glm_trace.retained import container_identity
                container_id = await container_identity(identity.stdout.strip())
                manifest = {"container_id": container_id, "resume_allowed": checkpoint["resume_allowed"],
                            "hashes": {name: hashlib.sha256((trace.path/name).read_bytes()).hexdigest()
                                       for name in ("checkpoint.json", "worktree.tar.gz")}}
                (trace.path / "checkpoint_manifest.json").write_text(json.dumps(manifest, indent=2))
                trace.event("checkpoint_saved", **manifest)
                if self.resume_directory:
                    # Official collection still runs in Pier's new isolated agent environment.
                    original_environment = bridge.original_environment
                    await original_environment.upload_file(archive_path, "/tmp/glm-resumed-worktree.tar.gz")
                    restored = await original_environment.exec("mkdir /tmp/glm-resumed-app && tar -xzf /tmp/glm-resumed-worktree.tar.gz -C /tmp/glm-resumed-app && mv /app /tmp/glm-original-app && mv /tmp/glm-resumed-app /app", timeout_sec=600)
                    if restored.return_code:
                        raise RuntimeError("Resume collection transfer failed")
                    await environment.stop()
            (self.logs_dir / "trace_summary.json").write_text(json.dumps({"calls": trace.turn_id,
                "attempts": len(trace.rows), "estimated_points": trace.points,
                "exit_status": agent.messages[-1].get("extra", {}).get("exit_status") if agent.messages else None}))
            trajectory = convert_and_save_trajectory(agent.config.output_path, self.logs_dir / "trajectory.json", trace.session_id)
            populate_context_from_final_metrics(context, trajectory.final_metrics)
