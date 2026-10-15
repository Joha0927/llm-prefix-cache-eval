"""Resume only an exact retained task container; no other Docker resources touched."""
import asyncio
import json
from types import SimpleNamespace


async def command(*args, timeout=660):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        process.kill()
        await process.communicate()
        raise
    return SimpleNamespace(stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"),
                           return_code=process.returncode)


async def container_identity(hostname):
    result = await command("docker", "inspect", "--format", "{{.Id}}", hostname)
    if result.return_code or len(result.stdout.strip()) != 64:
        raise RuntimeError("Cannot identify exact retained container")
    return result.stdout.strip()


class RetainedEnvironment:
    def __init__(self, container_id):
        self.container_id = container_id

    @classmethod
    async def attach(cls, container_id):
        result = await command("docker", "inspect", "--format", '{{json .HostConfig}}', container_id)
        if result.return_code:
            raise RuntimeError("Retained container missing; no automatic fresh restart")
        config = json.loads(result.stdout)
        if config["NetworkMode"] != "none" or config["Privileged"]:
            raise RuntimeError("Retained container isolation changed")
        started = await command("docker", "start", container_id)
        if started.return_code:
            raise RuntimeError("Retained container could not start")
        environment = cls(container_id)
        checked = await environment.exec("test ! -e /solution && test ! -e /tests && test ! -e /var/run/docker.sock")
        if checked.return_code:
            raise RuntimeError("Retained task isolation failed")
        return environment

    async def exec(self, action, cwd=None, env=None, timeout_sec=30, **kwargs):
        args = ["docker", "exec", "-w", cwd or "/app"]
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([self.container_id, "bash", "-c", action])
        return await command(*args, timeout=timeout_sec)

    async def download_file(self, source_path, target_path):
        result = await command("docker", "cp", self.container_id + ":" + source_path, str(target_path))
        if result.return_code:
            raise RuntimeError("Retained workspace backup transfer failed")

    async def upload_file(self, source_path, target_path):
        result = await command("docker", "cp", str(source_path), self.container_id + ":" + target_path)
        if result.return_code:
            raise RuntimeError("Workspace restore transfer failed")

    async def stop(self):
        result = await command("docker", "stop", self.container_id)
        if result.return_code:
            raise RuntimeError("Retained task could not be stopped")
