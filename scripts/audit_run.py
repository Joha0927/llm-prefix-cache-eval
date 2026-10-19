"""Audit one completed run, using only primary provider usage for totals."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def iter_events(path):
    """JSONL boundaries are LF, never Unicode separators inside JSON strings."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSONL record {path.name}:{number}") from exc


def file_hash(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def audit(run):
    usage_path = run / "usage.csv"
    rows = list(csv.DictReader(usage_path.open(encoding="utf-8"))) if usage_path.exists() else []
    starts, provider, native = set(), {}, {}
    for e in iter_events(run / "requests.jsonl"):
        if e["event"] == "wire_request":
            starts.add(e["attempt_id"])
        elif e["event"] == "wire_response":
            body = e.get("body") or {}
            provider[e["attempt_id"]] = {"usage": body.get("usage"), "model": body.get("model")}
        elif e["event"] == "native_response":
            native[e["call_id"]] = e["message"]["extra"]["response"].get("usage") or {}
        elif e["event"] == "call_error":
            for message in e.get("messages") or []:
                response = message.get("extra", {}).get("response")
                if isinstance(response, dict):
                    native[e["call_id"]] = response.get("usage") or {}
    errors = []
    if starts and not usage_path.exists():
        errors.append("missing_usage_csv")
    if not starts:
        errors.append("no_http_attempts")
    if starts != {r["attempt_id"] for r in rows} or len(rows) != len(starts):
        errors.append("attempt_coverage")
    checked = 0
    valid = []
    for row in rows:
        if row["http_status"] != "200":
            continue
        wire = provider.get(row["attempt_id"], {})
        original = wire.get("usage") or {}
        if row["usage_valid"] != "True":
            errors.append("missing_usage:" + row["attempt_id"])
            continue
        valid.append(row)
        expected = {"prompt_tokens": original.get("prompt_tokens"), "cached_tokens": (original.get("prompt_tokens_details") or {}).get("cached_tokens"), "completion_tokens": original.get("completion_tokens")}
        if any(str(value) != row[field] for field, value in expected.items()):
            errors.append("csv_wire_mismatch:" + row["attempt_id"])
        usage = native.get(row["call_id"], {})
        if any(usage.get(k) != original.get(k) for k in ("prompt_tokens", "completion_tokens")) or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") != expected["cached_tokens"]:
            errors.append("native_wire_mismatch:" + row["attempt_id"])
        else:
            checked += 1
        if wire.get("model") != "glm-5.3":
            errors.append("unexpected_response_model")
    manifest = json.loads((run / "manifest.json").read_text())
    if any(file_hash(run / name) != digest for name, digest in manifest.items()):
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
