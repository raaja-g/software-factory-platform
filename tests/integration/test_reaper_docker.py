import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import docker
import pytest

from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.docker import DockerSandboxProvider
from swfactory.sandbox.template import (
    container_config,
    home_volume_name,
    job_network_name,
    volume_name,
)

pytestmark = pytest.mark.docker

STANDARD = RunnerSpec("standard", "swfactory/runner:0.1.0", 1, 1024, 512, ())


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def provider(client, tmp_path):
    return DockerSandboxProvider(lambda: client, spool_dir=tmp_path / "spool")


@pytest.fixture
def job_id(provider):
    jid = uuid.uuid4()
    yield jid
    asyncio.run(provider.destroy(jid))


@pytest.fixture
def edge_standin(client):
    c = client.containers.run(
        STANDARD.image,
        ["sleep", "infinity"],
        detach=True,
        labels={"com.docker.compose.service": "gateway-edge"},
        name=f"edge-standin-{uuid.uuid4().hex[:8]}",
    )
    yield c
    c.remove(force=True)


def networks(client, jid):
    return [n.name for n in client.networks.list(filters={"label": f"factory.job_id={jid}"})]


def volumes(client, jid):
    return [v.name for v in client.volumes.list(filters={"label": f"factory.job_id={jid}"})]


async def test_reap_orphans_removes_container_network_and_volumes(
    client, provider, job_id, edge_standin
):
    await provider.ensure(job_id, STANDARD)
    assert await provider.reap_orphans(live=set(), active=set()) == 4
    assert networks(client, job_id) == [] and volumes(client, job_id) == []


async def test_reap_keeps_volumes_of_waiting_job(client, provider, job_id, edge_standin):
    await provider.ensure(job_id, STANDARD)
    c = client.containers.get(f"factory-sandbox-{job_id}")
    # container label factory.expires in the past: recreate the container with an old label
    cfg_labels = dict(c.labels)
    assert cfg_labels["factory.job_id"] == str(job_id)
    c.remove(force=True)
    cfg = container_config(
        job_id,
        STANDARD,
        work=volume_name(job_id),
        home=home_volume_name(job_id),
        network=job_network_name(job_id),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    provider._create_container(f"factory-sandbox-{job_id}", cfg)
    assert await provider.reap_orphans(live={job_id}, active=set()) == 1
    assert len(volumes(client, job_id)) == 2 and len(networks(client, job_id)) == 1
