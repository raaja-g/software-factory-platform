"""Benchmark prepare at the pinned base commit with host git (default set)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from tests.fakes.host_sandbox import HostSandbox

from swfactory.db.tables import Job
from swfactory.definition.models import AppDef
from swfactory.domain.types import JobKind
from swfactory.gitops.local import GitError, LocalGit

BRANCH = "bench/0000abcd/t/c"
APP = AppDef(repo_kind="local", repo_url="pyapp")


def g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def bench_job(base: str, branch: str = BRANCH) -> Job:
    return Job(
        id=uuid.uuid4(),
        seq=1,
        slug="t",
        kind=JobKind.BENCHMARK.value,
        branch=branch,
        base_sha=base,
    )


@pytest.fixture
def repos(tmp_path: Path) -> Path:
    return tmp_path / "repos"


@pytest.fixture
def shas(repos: Path, tmp_path: Path) -> list[str]:
    bare = LocalGit(repos).init_bare("pyapp")
    work = tmp_path / "work"
    g(tmp_path, "clone", "-q", str(bare), str(work))
    out = []
    for i in (1, 2, 3):
        (work / f"f{i}.txt").write_text(str(i))
        g(work, "add", "-A")
        g(work, "commit", "-q", "-m", f"c{i}")
        out.append(g(work, "rev-parse", "HEAD"))
    g(work, "push", "-q", "origin", "HEAD:main")
    return out


async def head(box: HostSandbox, ref: str = "HEAD") -> str:
    return (await box.exec(["git", "rev-parse", ref])).stdout.strip()


async def test_bench_prepare_at_base_without_later_commits(repos, shas, tmp_path):
    c1, _, c3 = shas
    box = HostSandbox(tmp_path / "box")
    git = LocalGit(repos)
    got = await git.prepare(bench_job(c1), APP, box)
    assert got == c1
    assert await head(box) == c1
    assert (await box.exec(["git", "branch", "--show-current"])).stdout.strip() == BRANCH
    assert await head(box, "origin/main") == c1
    assert (await box.exec(["git", "cat-file", "-e", c3])).exit_code != 0
    assert await git.rebase(bench_job(c1), APP, box) is True
    assert await head(box) == c1
    assert (await box.exec(["git", "cat-file", "-e", c3])).exit_code != 0


async def test_bench_prepare_is_repeatable_and_deliver_raises(repos, shas, tmp_path):
    c1 = shas[0]
    box = HostSandbox(tmp_path / "box")
    git = LocalGit(repos)
    job = bench_job(c1)
    await git.prepare(job, APP, box)
    await git.prepare(job, APP, box)
    assert await head(box) == c1
    with pytest.raises(GitError):
        await git.deliver(job, APP, box, draft=False, expected_head=c1)


async def test_reference_prepare_has_merge_tree(repos, tmp_path):
    bare = LocalGit(repos).init_bare("pyapp")
    work = tmp_path / "work2"
    g(tmp_path, "clone", "-q", str(bare), str(work))
    (work / "f1.txt").write_text("1")
    g(work, "add", "-A")
    g(work, "commit", "-q", "-m", "c1")
    c1 = g(work, "rev-parse", "HEAD")
    (work / "f2.txt").write_text("2")
    (work / "f1.txt").write_text("one")
    g(work, "add", "-A")
    g(work, "commit", "-q", "-m", "m1")
    m1 = g(work, "rev-parse", "HEAD")
    g(work, "push", "-q", "origin", "HEAD:main")
    job = bench_job(c1, "bench/check-0000abcd/t")
    job.title = "check t"
    job.source_ref = "bench-check:" + "0" * 64
    box = HostSandbox(tmp_path / "box")
    got = await LocalGit(repos).prepare(job, APP, box, merge_sha=m1)
    assert got == c1
    assert await head(box, "HEAD^{tree}") == g(work, "rev-parse", f"{m1}^{{tree}}")
    assert await head(box, "HEAD^") == c1
    log = (await box.exec(["git", "log", "-1", "--format=%an|%s"])).stdout.strip()
    assert log == "factory-bot|reference: t"
    again = await LocalGit(repos).prepare(job, APP, box, merge_sha=m1)  # repeatable
    assert again == c1 and await head(box, "HEAD^") == c1
