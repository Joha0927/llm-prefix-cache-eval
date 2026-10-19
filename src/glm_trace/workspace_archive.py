"""Archive each workspace into a fresh private container directory."""
import uuid
from pathlib import Path


async def archive_workspace(environment, target, trace=None):
    directory = "/tmp/glm-checkpoint-" + uuid.uuid4().hex
    source = directory + "/worktree.tar.gz"
    result = await environment.exec(
        "mkdir -m 700 " + directory + " && tar -C /app -czf " + source + " .",
        timeout_sec=600)
    if result.return_code:
        if trace is not None:
            trace.event("checkpoint_archive_error", returncode=result.return_code,
                        stdout=result.stdout, stderr=result.stderr, archive_source=source)
        raise RuntimeError("Workspace checkpoint failed")
    await environment.download_file(source, Path(target))
    if trace is not None:
        trace.event("workspace_archive_downloaded", archive_source=source)
    return source
