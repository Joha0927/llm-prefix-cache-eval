"""Audit one completed run, using only primary provider usage for totals."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def audit(run):
    events = [json.loads(line) for line in (run / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = list(csv.DictReader((run / "usage.csv").open(encoding="utf-8")))
    starts = {e["attempt_id"] for e in events if e["event"] == "wire_request"}
    errors = []
    if not starts:
        errors.append("no_http_attempts")
    if starts != {r["attempt_id"] for r in rows} or len(rows) != len(starts):
        errors.append("attempt_coverage")
    provider = {e["attempt_id"]: e for e in events if e["event"] == "wire_response"}
    native = {}
    for e in events:
        if e["event"] == "native_response":
            native[e["call_id"]] = e["message"]["extra"]["response"]
        elif e["event"] == "call_error":
            for message in e.get("messages") or []:
                response = message.get("extra", {}).get("response")
                if isinstance(response, dict):
                    native[e["call_id"]] = response
    checked = 0
    valid = []
    for row in rows:
        if row["http_status"] != "200":
            continue
        wire = provider.get(row["attempt_id"], {}).get("body") or {}
        original = wire.get("usage") or {}
        if row["usage_valid"] != "True":
            errors.append("missing_usage:" + row["attempt_id"])
            continue
        valid.append(row)
        expected = {"prompt_tokens": original.get("prompt_tokens"), "cached_tokens": (original.get("prompt_tokens_details") or {}).get("cached_tokens"), "completion_tokens": original.get("completion_tokens")}
        if any(str(value) != row[field] for field, value in expected.items()):
            errors.append("csv_wire_mismatch:" + row["attempt_id"])
        decoded = native.get(row["call_id"], {})
        usage = decoded.get("usage") or {}
        if any(usage.get(k) != original.get(k) for k in ("prompt_tokens", "completion_tokens")) or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") != expected["cached_tokens"]:
            errors.append("native_wire_mismatch:" + row["attempt_id"])
        else:
            checked += 1
        if wire.get("model") != "glm-5.3":
            errors.append("unexpected_response_model")
    manifest = json.loads((run / "manifest.json").read_text())
    if any(hashlib.sha256((run / name).read_bytes()).hexdigest() != digest for name, digest in manifest.items()):
        errors.append("archive_hash_mismatch")
    prompt = sum(int(r["prompt_tokens"]) for r in valid)
    cached = sum(int(r["cached_tokens"]) for r in valid)
    return {"run_id": run.name, "errors": errors, "quality_passed": not errors,
            "http_attempts": len(starts), "successful_usage_rows": len(valid), "native_wire_matches": checked,
            "prompt_tokens": prompt, "cached_tokens": cached,
            "completion_tokens": sum(int(r["completion_tokens"]) for r in valid),
            "token_weighted_cache_hit_rate": cached / prompt if prompt else None,
            "estimated_points": sum(float(r["estimated_points"] or 0) for r in rows),
            "counting_source": "primary usage.csv; native copies not added"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.run)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["quality_passed"] else 1)
