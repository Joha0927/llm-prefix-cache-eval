"""Stable-turn checkpoints and explicit continuation of the pinned native agent."""
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from minisweagent.exceptions import FormatError, InterruptAgentFlow

PAUSABLE = {"TaskPointsLimit", "QuotaExhausted"}


def save_checkpoint(agent):
    messages = copy.deepcopy(agent.messages)
    exit_status = messages[-1].get("extra", {}).get("exit_status") if messages else None
    if messages and messages[-1].get("role") == "exit":
        messages.pop()
    value = agent.trace.clean({"schema_version": 1, "messages": messages,
        "n_calls": agent.n_calls, "cost": agent.cost,
        "n_consecutive_format_errors": agent.n_consecutive_format_errors,
        "active_seconds": time.time() - agent._start_time,
        "cumulative_points": getattr(agent.trace, "prior_points", 0) + agent.trace.points,
        "resume_allowed": exit_status in PAUSABLE, "exit_status": exit_status,
        "ids": agent.trace.ids})
    target = agent.trace.path / "checkpoint.json"
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    return value


def continue_agent(agent, state, task):
    """Keep native step()/format handling; initialization is replaced only on resume."""
    if not state["resume_allowed"] or not state["messages"]:
        raise RuntimeError("Checkpoint is not a supported clean budget/quota pause")
    agent.messages = copy.deepcopy(state["messages"])
    agent.n_calls, agent.cost = state["n_calls"], state["cost"]
    agent.n_consecutive_format_errors = state["n_consecutive_format_errors"]
    agent._start_time = time.time() - state["active_seconds"]
    agent.extra_template_vars["task"] = task
    while True:
        try:
            agent.step()
            agent.n_consecutive_format_errors = 0
        except FormatError as exc:
            agent.cost += exc.messages[0].get("extra", {}).get("cost", 0)
            agent.n_consecutive_format_errors += 1
            agent.add_messages(*exc.messages)
            if 0 < agent.config.max_consecutive_format_errors <= agent.n_consecutive_format_errors:
                agent.add_messages({"role": "exit", "content": "RepeatedFormatError",
                                    "extra": {"exit_status": "RepeatedFormatError", "submission": ""}})
        except InterruptAgentFlow as exc:
            agent.add_messages(*exc.messages)
        except Exception as exc:
            agent.handle_uncaught_exception(exc)
            raise
        finally:
            agent.save(agent.config.output_path)
        if agent.messages[-1].get("role") == "exit":
            break
    return agent.messages[-1].get("extra", {})


def load_checkpoint(directory, task_id):
    directory = Path(directory)
    state = json.loads((directory / "checkpoint.json").read_text())
    manifest = json.loads((directory / "checkpoint_manifest.json").read_text())
    if state["ids"]["task_id"] != task_id or not state["resume_allowed"]:
        raise RuntimeError("Wrong task or non-resumable checkpoint")
    for name, digest in manifest["hashes"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Checkpoint hash mismatch")
    return state, manifest
