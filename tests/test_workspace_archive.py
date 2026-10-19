import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from glm_trace.workspace_archive import archive_workspace


def test_download_chown_does_not_reuse_archive_path(tmp_path):
    class Environment:
        def __init__(self):
            self.downloaded = set()
            self.commands = []
        async def exec(self, command, **kwargs):
            self.commands.append(command)
            assert command.startswith("mkdir -m 700 /tmp/glm-checkpoint-")
            assert not any(path in command for path in self.downloaded)
            return SimpleNamespace(return_code=0)
        async def download_file(self, source, target):
            assert source not in self.downloaded
            self.downloaded.add(source)  # Download may change source owner to UID 1000.
            Path(target).write_bytes(b"archive")
    env = Environment()
    async def run():
        first = await archive_workspace(env, tmp_path / "first")
        second = await archive_workspace(env, tmp_path / "second")
        assert first != second and len(env.downloaded) == 2
    asyncio.run(run())


def test_failed_archive_records_diagnostic_and_does_not_download(tmp_path):
    class Environment:
        async def exec(self, *args, **kwargs):
            return SimpleNamespace(return_code=2, stdout="", stderr="Permission denied")
        async def download_file(self, *args):
            pytest.fail("Failed archive must not be downloaded")
    events = []
    trace = SimpleNamespace(event=lambda name, **kwargs: events.append((name, kwargs)))
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        asyncio.run(archive_workspace(Environment(), tmp_path / "out", trace))
    assert events[0][1]["stderr"] == "Permission denied"
