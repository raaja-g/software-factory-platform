"""GitHubGit prepares a benchmark sandbox at the base commit, depth 1, with no push."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from tests.fakes.github_git import DeliveryGitHub, FakeAuth, FakeGitops
from tests.fakes.host_sandbox import HostSandbox

from swfactory.db.tables import Job
from swfactory.definition.models import AppDef
from swfactory.domain.types import JobKind
from swfactory.gitops.github import GitHubGit
from swfactory.gitops.local import GitError, LocalGit

BRANCH = "bench/0000abcd/t/c"
APP = AppDef(repo_kind="github", repo_url="acme/web")


def g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def bench_job(base: str) -> Job:
    return Job(
        id=uuid.uuid4(),
        seq=1,
        slug="t",
        kind=JobKind.BENCHMARK.value,
        branch=BRANCH,
        base_sha=base,
    )


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, list[str]]:
    bare = LocalGit(tmp_path / "origin").init_bare("web")
    work = tmp_path / "w"
    g(tmp_path, "clone", "-q", str(bare), str(work))
    shas = []
    for i in (1, 2, 3):
        (work / f"f{i}").write_text(str(i))
        g(work, "add", "-A")
        g(work, "commit", "-q", "-m", f"c{i}")
        shas.append(g(work, "rev-parse", "HEAD"))
    g(work, "push", "-q", "origin", "HEAD:main")
    return bare, shas


@pytest.fixture
def fake_gh() -> DeliveryGitHub:
    return DeliveryGitHub()


@pytest.fixture
def gitops(tmp_path, origin, fake_gh) -> FakeGitops:
    return FakeGitops(tmp_path / "gitops", origin[0], fake_gh, FakeAuth())


@pytest.fixture
def github_git(tmp_path, gitops, fake_gh) -> GitHubGit:
    return GitHubGit(
        auth=gitops.auth,  # type: ignore[arg-type]
        gitops=gitops,  # type: ignore[arg-type]
        sessions=None,  # type: ignore[arg-type]
        repos_dir=tmp_path / "repos",
        client_for=lambda repo: fake_gh,  # type: ignore[arg-type,return-value]
    )


def fetch_argv(gitops: FakeGitops) -> list[str]:
    return next(a for a in gitops.argvs if "fetch" in a)


async def test_bench_github_prepare_depth_1_no_push(github_git, gitops, fake_gh, origin, tmp_path):
    c1, _, c3 = origin[1]
    box = HostSandbox(tmp_path / "box")
    job = bench_job(c1)
    assert await github_git.prepare(job, APP, box) == c1
    assert fetch_argv(gitops)[-2:] == ["--depth=1", c1]
    assert fake_gh.calls.get("create_pr", 0) == 0 and fake_gh.pushes == 0
    head = (await box.exec(["git", "rev-parse", "HEAD"])).stdout.strip()
    assert head == c1
    cur = (await box.exec(["git", "branch", "--show-current"])).stdout.strip()
    assert cur == BRANCH
    assert (await box.exec(["git", "cat-file", "-e", c3])).exit_code != 0
    n = len(gitops.argvs)
    assert await github_git.rebase(job, APP, box) is True
    assert len(gitops.argvs) == n


async def test_bench_github_deliver_raises(github_git, origin, tmp_path):
    box = HostSandbox(tmp_path / "box")
    with pytest.raises(GitError):
        await github_git.deliver(
            bench_job(origin[1][0]), APP, box, draft=False, expected_head=origin[1][0]
        )
