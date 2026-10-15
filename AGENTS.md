# Project instructions

- Preserve the provider-observed cache metric: `cached_tokens / prompt_tokens`.
- Keep invalid or absent Usage unavailable; never replace it with zero.
- Never publish credentials, local `.env` files, raw requests, trajectories, dataset contents, checkpoints or generated reports.
- Default CLI behavior must remain preview-only. Real model calls require explicit execution and budget arguments.
- The evaluated agent is mini-SWE-agent. Development assistants must not solve Benchmark tasks or alter official tests, solutions or scoring rules.
- API credentials remain on the host; do not mount them or authoritative request logs into task containers.
- Preserve the native prompt, tool schema and parsing behavior. Record and test any intentional protocol change.
- Persist inputs before requests and responses/Usage immediately after each response. Archive and hash each task independently.
- Keep logical call IDs separate from actual request IDs. Do not add duplicate SDK/native Usage to provider totals.
- Stop on quota exhaustion, collection failures and invalid Usage; do not switch accounts, interfaces or models automatically.
- Keep concurrency one unless a separately reviewed experiment changes it. Do not delete unrelated Docker resources.
- Explicit resumes use a new Run ID, original conversation and exact retained container; preserve cumulative budget, steps and active time.
- Use pinned upstream versions. Linux task scripts must use LF; do not run Windows CRLF archives.
- Run offline Mock tests for code changes. Document changed measurement conditions and material limitations in README.
- Keep code portable: configurable local paths, no developer usernames, absolute workstation paths or historical experiment state.
