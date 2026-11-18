import asyncio
import uuid
from dataclasses import replace

import docker
import pytest

from swfactory.sandbox.base import RunnerSpec, SandboxFileTooLarge, WorkspaceOverLimit
from swfactory.sandbox.docker import DockerSandboxProvider
from swfactory.sandbox.template import container_name

pytestmark = pytest.mark.docker

STANDARD = RunnerSpec("standard", "swfactory/runner:0.1.0", 1, 1024, 512, ())


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def provider(client, tmp_path):
    return DockerSandboxProvider(
        lambda: client, spool_dir=tmp_path / "spool", output_max_bytes=100_000
    )


@pytest.fixture
def job_id(provider):
    jid = uuid.uuid4()
    yield jid
    asyncio.run(provider.destroy(jid))


async def test_exec_output_keeps_head_and_tail(provider, job_id):
    box = await provider.ensure(job_id, STANDARD)
    r = await box.exec(["sh", "-c", "echo START; yes x | head -c 5000000; echo END"])
    assert (
        r.exit_code == 0
        and len(r.stdout) < 100_100
        and r.stdout.startswith("START")
        and r.stdout.rstrip().endswith("END")
    )
    assert "[omitted: " in r.stdout


async def test_read_bytes_refuses_a_huge_file(provider, job_id, monkeypatch):
    monkeypatch.setattr("swfactory.sandbox.docker.READ_BYTES_MAX", 1_000_000)
    box = await provider.ensure(job_id, STANDARD)
    await box.exec(["sh", "-c", "head -c 2000000 /dev/zero > /work/big"])
    with pytest.raises(SandboxFileTooLarge):
        await box.read_bytes("/work/big")


async def test_workspace_over_limit_is_killed(provider, job_id, client):
    box = await provider.ensure(job_id, replace(STANDARD, max_work_gb=1))
    await box.exec(["sh", "-c", "head -c 1200000000 /dev/zero > /work/fill"], timeout_s=120)
    assert await provider.check_workspaces() == [job_id]
    assert client.containers.get(container_name(job_id)).status == "exited"
    with pytest.raises(WorkspaceOverLimit, match="workspace over 1 GB"):
        await box.exec(["true"])
