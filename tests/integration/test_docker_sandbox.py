# ruff: noqa: E501
import os
import uuid

import docker
import pytest
from docker.errors import NotFound

from swfactory.sandbox.base import RunnerSpec, TransferTooLarge
from swfactory.sandbox.docker import ISOLATED_GIT_CONFIG, DockerSandboxProvider
from swfactory.sandbox.template import (
    container_name,
    verified_volume_name,
)

pytestmark = pytest.mark.docker

STANDARD = RunnerSpec("standard", "swfactory/runner:0.1.0", 1, 1024, 512, ())
GIT = "git -c user.name=t -c user.email=t@t"


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def provider(client, tmp_path):
    return DockerSandboxProvider(lambda: client, spool_dir=tmp_path / "spool")


@pytest.fixture
def job_id(client, provider):
    jid = uuid.uuid4()
    yield jid
    import asyncio

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


def _list(client, jid, kind, ephemeral=False):
    filters = {
        "label": [f"factory.job_id={jid}"] + (["factory.ephemeral=true"] if ephemeral else [])
    }
    if kind == "containers":
        return [c.name for c in client.containers.list(all=True, filters=filters)]
    if kind == "volumes":
        return [v.name for v in client.volumes.list(filters=filters)]
    return [n.name for n in client.networks.list(filters={"label": f"factory.job_id={jid}"})]


def state(client, jid):
    return client.containers.get(container_name(jid)).status


def network_members(client, name):
    net = client.networks.get(name)
    net.reload()
    return {c["Name"] for c in net.attrs["Containers"].values()}


@pytest.fixture
async def job_with_repo(provider, job_id):
    box = await provider.ensure(job_id, STANDARD)
    script = (
        "set -e; git init -q -b factory/001-x /work/repo; cd /work/repo; "
        "printf 'node_modules/\\n' > .gitignore; echo hello > README.md; mkdir secret; echo s > secret/s; "
        f"git add .gitignore README.md; {GIT} commit -qm A; "
        "git rev-parse HEAD"
    )
    r = await box.exec(["sh", "-c", script], cwd="/work")
    assert r.exit_code == 0, r.stderr
    a = r.stdout.strip().splitlines()[-1]
    readme = "hello\n"
    dirty = (
        "set -e; cd /work/repo; mkdir -p node_modules/.bin hooks; echo j > node_modules/.bin/jest; "
        "echo secret/ >> .git/info/exclude; echo '#!/bin/sh' > .git/hooks/post-checkout; "
        "git config core.fsmonitor true; echo dirty >> README.md; echo bash > /home/factory/.bashrc; "
        f"b=$({GIT} commit-tree -m b $(git mktree </dev/null)); git replace {a} $b"
    )
    r = await box.exec(["sh", "-c", dirty], cwd="/work")
    assert r.exit_code == 0, r.stderr
    return job_id, box, a, readme


async def test_ensure_exec_files(provider, job_id, tmp_path):
    box = await provider.ensure(job_id, STANDARD)
    assert (
        box.image_digest.startswith("sha256:") and (await provider.ensure(job_id, STANDARD)) is box
    )
    r = await box.exec(["sh", "-c", "id -u; touch /etc/x"], cwd="/work")
    assert r.stdout.startswith("10001") and r.exit_code != 0
    await box.write_bytes("/work/.factory/in/a.bin", b"\x00\x01")
    assert await box.read_bytes("/work/.factory/in/a.bin") == b"\x00\x01"
    assert await box.read_text("/work/missing.md") is None
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024))
    await box.copy_in(big, "/work/.factory/git/x.bundle")
    n = await box.copy_out(
        "/work/.factory/git/x.bundle", tmp_path / "back.bin", max_bytes=4 * 2**20
    )
    assert n == 3 * 1024 * 1024
    assert (tmp_path / "back.bin").read_bytes() == big.read_bytes()
    with pytest.raises(TransferTooLarge, match="raise FACTORY_MAX_BUNDLE_MB"):
        await box.copy_out("/work/.factory/git/x.bundle", tmp_path / "no.bin", max_bytes=2**20)
    assert not (tmp_path / "no.bin").exists()
    r = await box.exec(["sh", "-c", "ls /home/factory | grep -c repo"])
    assert r.stdout.strip() == "0"


async def test_job_network_attaches_job_and_edge_only(
    client, provider, edge_standin, job_with_repo
):
    job_id, box, a, _ = job_with_repo
    name = f"factory-net-{job_id}"
    assert network_members(client, name) == {container_name(job_id), edge_standin.name}
    assert client.networks.get(name).attrs["Internal"] is False
    r = await box.exec(["sh", "-c", "getent hosts gateway-edge >/dev/null; echo $?"])
    assert r.stdout.strip() == "0"
    async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a):
        assert len(network_members(client, name)) == 3


async def test_jobs_cannot_reach_each_other(provider, client):
    j1, j2 = uuid.uuid4(), uuid.uuid4()
    try:
        a, _ = await provider.ensure(j1, STANDARD), await provider.ensure(j2, STANDARD)
        r = await a.exec(["sh", "-c", f"getent hosts {container_name(j2)} >/dev/null; echo $?"])
        assert r.stdout.strip() == "2"
    finally:
        await provider.destroy(j1)
        await provider.destroy(j2)


async def test_restart_kills_processes_ignoring_refcount(client, provider, job_id):
    box = await provider.ensure(job_id, STANDARD)
    await box.start()
    await box.start()
    await box.exec(["sh", "-c", "setsid sleep 300 &"])
    await box.write_text("/work/k", "kept")
    await box.restart()
    r = await box.exec(["sh", "-c", "pgrep -fc 'sleep 30[0]' || true"])
    assert r.stdout.strip() == "0" and state(client, job_id) == "running"
    assert await box.read_text("/work/k") == "kept"
    await box.stop()
    assert state(client, job_id) == "running"
    await box.stop()
    assert state(client, job_id) == "exited"


async def test_exec_timeout_kills(provider, job_id):
    box = await provider.ensure(job_id, STANDARD)
    r = await box.exec(["sleep", "30"], timeout_s=2)
    assert r.timed_out and r.duration_s < 10


async def test_stop_is_refcounted_and_destroy_removes_volumes(
    client, provider, job_id, edge_standin
):
    box = await provider.ensure(job_id, STANDARD)
    await box.start()
    await box.start()
    await box.stop()
    assert state(client, job_id) == "running"
    await box.stop()
    assert state(client, job_id) == "exited"
    await provider.destroy(job_id)
    assert _list(client, job_id, "volumes") == [] and _list(client, job_id, "containers") == []
    assert _list(client, job_id, "networks") == []
    edge_standin.reload()
    assert f"factory-net-{job_id}" not in edge_standin.attrs["NetworkSettings"]["Networks"]


async def test_verify_box_is_a_clean_clone_at_head(client, provider, job_with_repo):
    job_id, box, a, readme = job_with_repo
    async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a) as vb:
        assert (await vb.exec(["git", "rev-parse", "HEAD"])).stdout.strip() == a
        assert await vb.read_text("/work/repo/README.md") == readme
        assert not await vb.read_text("/work/repo/node_modules/.bin/jest")
        assert not await vb.read_text("/work/repo/.git/hooks/post-checkout")
        assert (await vb.exec(["git", "for-each-ref", "refs/replace"])).stdout == ""
        assert (await vb.exec(["ls", "-A", "/home/factory"])).stdout == ""
        assert (await vb.exec(["ls", "-A", "/work/.factory/in"])).stdout == ""
        assert (await vb.exec(["ls", "/work/repo/secret"])).exit_code != 0
        await vb.exec(
            [
                "sh",
                "-c",
                "mkdir -p /work/repo/node_modules && echo ok > /work/repo/node_modules/setup",
            ]
        )
        a_tree = (await vb.exec(["git", "rev-parse", "HEAD^{tree}"])).stdout.strip()
        await vb.exec(
            [
                "sh",
                "-c",
                "cd /work/repo && git replace HEAD $(git -c user.name=t -c user.email=t@t commit-tree -m b $(git mktree </dev/null))"
                " && echo hidden/ >> .git/info/exclude && mkdir hidden && echo h > hidden/h",
            ]
        )
    assert verified_volume_name(job_id) in _list(client, job_id, "volumes")
    assert _list(client, job_id, "containers", ephemeral=True) == []
    async with provider.isolated(job_id, STANDARD, name="review-code-r1") as iso:
        assert (await iso.exec(["ls", "-A", "/home/factory"])).stdout == ""
        assert (await iso.exec(["touch", "/snapshot/repo/x"])).exit_code != 0
        assert await iso.read_text("/work/repo/.git/config") == ISOLATED_GIT_CONFIG
        assert await iso.read_text("/work/repo/node_modules/setup") == "ok\n"
        assert not await iso.read_text("/work/repo/node_modules/.bin/jest")
        assert (await iso.exec(["git", "rev-parse", "HEAD^{tree}"])).stdout.strip() == a_tree
        assert (await iso.exec(["git", "for-each-ref", "refs/replace"])).stdout == ""
        assert not await iso.read_text("/work/repo/hidden/h")
        assert (await iso.exec(["git", "status", "--porcelain"])).stdout == ""
        await iso.write_text("/work/.factory/out/r.md", "VERDICT: PASS")
    assert await box.read_text("/work/.factory/out/r.md") is None
    assert _list(client, job_id, "containers", ephemeral=True) == []


async def test_verify_box_replaces_a_leftover_with_the_same_name(client, provider, job_with_repo):
    job_id, _, a, _ = job_with_repo
    cm = provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a)
    await cm.__aenter__()
    async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a) as vb:
        assert (await vb.exec(["git", "rev-parse", "HEAD"])).stdout.strip() == a
        assert len(_list(client, job_id, "containers", ephemeral=True)) == 1
    assert _list(client, job_id, "containers", ephemeral=True) == []


async def test_verify_box_with_an_unknown_head_raises(client, provider, job_with_repo):
    job_id, *_ = job_with_repo
    with pytest.raises(RuntimeError, match="verify box head"):
        async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head="c" * 40):
            pass
    assert verified_volume_name(job_id) not in _list(client, job_id, "volumes")


async def test_isolated_without_a_verified_tree_raises(provider, job_id):
    await provider.ensure(job_id, STANDARD)
    with pytest.raises(RuntimeError, match="no verified tree"):
        async with provider.isolated(job_id, STANDARD, name="review-code-r1"):
            pass


async def test_isolated_replaces_a_leftover_with_the_same_name(client, provider, job_with_repo):
    job_id, _, a, _ = job_with_repo
    async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a):
        pass
    cm = provider.isolated(job_id, STANDARD, name="review-code-r1")
    await cm.__aenter__()
    async with provider.isolated(job_id, STANDARD, name="review-code-r1") as iso:
        assert (await iso.exec(["true"])).exit_code == 0
        assert len(_list(client, job_id, "containers", ephemeral=True)) == 1
    assert _list(client, job_id, "containers", ephemeral=True) == []
    assert [v for v in _list(client, job_id, "volumes") if v.endswith("review-code-r1")] == []


async def test_writes_outside_volumes(provider, job_with_repo):
    job_id, box, a, _ = job_with_repo
    async with provider.verify_box(job_id, STANDARD, name="gate-r1-a1", head=a):
        pass
    await box.write_text("/tmp/j.txt", "j")
    assert await box.read_text("/tmp/j.txt") == "j"
    async with provider.isolated(job_id, STANDARD, name="review-code-r1") as iso:
        await iso.write_text("/home/factory/opencode/runs/r1/opencode.json", "{}")
        await iso.write_bytes("/tmp/x.bin", b"\x00\x01")
        assert await iso.read_text("/home/factory/opencode/runs/r1/opencode.json") == "{}"
        assert await iso.read_bytes("/tmp/x.bin") == b"\x00\x01"
        assert await iso.read_text("/home/factory/missing.json") is None
        assert (await iso.exec(["cat"], stdin="hello")).stdout == "hello"
        assert (await iso.exec(["ls", "-A", "/work/.factory/tmp"])).stdout == ""


def test_notfound_import():
    assert NotFound
