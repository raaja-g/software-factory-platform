from __future__ import annotations

import subprocess
from pathlib import Path

from tests.fakes.host_sandbox import HostSandbox

from swfactory.gitops.local import LocalGit
from swfactory.gitops.service import (
    REASON_HEAD_MISMATCH,
    REASON_NOTHING_TO_DELIVER,
    SAFE_GIT_ENV,
    DiffStat,
    GitDispatcher,
    safe_git,
)

BRANCH = "factory/001-add-greeting"


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


async def head_sha_of(box: HostSandbox) -> str:
    return (await box.exec(["git", "rev-parse", "HEAD"])).stdout.strip()


def main_sha(bare: Path) -> str:
    return _g(bare, "rev-parse", "main")


def rev_parse(bare: Path, ref: str) -> str:
    return _g(bare, "rev-parse", ref)


def parents(bare: Path, sha: str) -> list[str]:
    return _g(bare, "rev-list", "--parents", "-n1", sha).split()[1:]


def message(bare: Path, sha: str) -> str:
    return _g(bare, "log", "-1", "--format=%B", sha)


def branch_exists(bare: Path, name: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{name}"],
            capture_output=True,
        ).returncode
        == 0
    )


def bundle_prerequisites(path: Path) -> list[str]:
    out = []
    for line in path.read_bytes().split(b"\n\n", 1)[0].decode().splitlines()[1:]:
        if line.startswith("-"):
            out.append(line[1:].split()[0])
    return out


async def test_prepare_clones_and_branches(git, box, job, app):
    base = await git.prepare(job, app, box)
    assert len(base) == 40
    assert (await box.exec(["git", "branch", "--show-current"])).stdout.strip() == BRANCH
    assert await git.prepare(job, app, box) == base


async def test_prepare_restores_kept_branch(git, job, app, prepared_box, tmp_path):
    await git.keep_branch(job, app, prepared_box)
    fresh = HostSandbox(tmp_path / "new")
    await git.prepare(job, app, fresh)
    assert await head_sha_of(fresh) == await head_sha_of(prepared_box)


async def test_keep_branch_is_idempotent_and_never_merges(git, job, app, prepared_box, bare):
    before = main_sha(bare)
    await git.keep_branch(job, app, prepared_box)
    await git.keep_branch(job, app, prepared_box)
    assert rev_parse(bare, BRANCH) == await head_sha_of(prepared_box)
    assert main_sha(bare) == before


async def test_rebase_conflict_returns_false_and_aborts(git, job, app, conflicting_box):
    assert await git.rebase(job, app, conflicting_box) is False
    assert (await conflicting_box.exec(["git", "status", "--porcelain"])).stdout == ""
    assert not (conflicting_box.root / "work/repo/.git/rebase-merge").exists()


async def test_rebase_onto_moved_main(git, job, app, edited_box, bare, tmp_path):
    from tests.unit.gitops.conftest import advance_main

    advance_main(bare, tmp_path, "other.txt", "x")
    assert await git.rebase(job, app, edited_box) is True
    assert (edited_box.root / "work/repo/other.txt").exists()
    assert await git.base_head(job, app) == main_sha(bare)


async def test_diff_stat_lists_paths(git, edited_box):
    assert await git.diff_stat(edited_box, "origin/main") == DiffStat(
        2, 3, 1, ("README.md", "src/app.py")
    )


async def test_planted_git_config_never_runs(git, job, app, planted_box):
    await git.rebase(job, app, planted_box)
    await git.diff_stat(planted_box, "origin/main")
    assert not (planted_box.root / "pwned").exists()


async def test_replace_refs_never_change_the_diff(git, replaced_box):
    assert SAFE_GIT_ENV["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert (await git.diff_stat(replaced_box, "origin/main")).paths == ("README.md", "src/app.py")


async def test_base_head_is_the_bundled_default_branch(git, job, app, prepared_box, bare):
    assert (
        await git.base_head(job, app)
        == main_sha(bare)
        == rev_parse(bare, f"refs/factory/base/{job.id}")
    )


async def test_deliver_merges_no_ff_once(git, job, app, edited_box, bare):
    head = await head_sha_of(edited_box)
    d1 = await git.deliver(job, app, edited_box, draft=False, expected_head=head)
    d2 = await git.deliver(job, app, edited_box, draft=False, expected_head=head)
    assert d1.status == d2.status == "merged" and d1.merge_sha == d2.merge_sha
    assert d1.merge_sha is not None and len(parents(bare, d1.merge_sha)) == 2
    assert f"Factory-Job: {job.id}" in message(bare, d1.merge_sha)
    assert branch_exists(bare, BRANCH)
    assert main_sha(bare) == d1.merge_sha


async def test_deliver_bundle_carries_only_the_job_commits(git, job, app, edited_box, bare):
    base = main_sha(bare)
    await git.deliver(
        job, app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
    )
    assert bundle_prerequisites(edited_box.root / "work/.factory/git/job.bundle") == [base]


async def test_bundle_over_the_limit_fails_clearly(job, app, edited_box, bare):
    d = await LocalGit(bare.parent, max_bundle_mb=0).deliver(
        job, app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
    )
    assert d.status == "failed" and "raise FACTORY_MAX_BUNDLE_MB" in d.detail


async def test_deliver_conflict_leaves_main_untouched(git, job, app, conflicting_box, bare):
    before = main_sha(bare)
    d = await git.deliver(
        job, app, conflicting_box, draft=False, expected_head=await head_sha_of(conflicting_box)
    )
    assert d.status == "conflict" and main_sha(bare) == before
    assert _g(bare, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_pr_policy_on_local_repo_fails(git, job, pr_app, edited_box):
    d = await git.deliver(
        job, pr_app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
    )
    assert d.status == "failed"


async def test_deliver_refuses_a_head_other_than_expected(git, job, app, edited_box, bare):
    before = main_sha(bare)
    d = await git.deliver(job, app, edited_box, draft=False, expected_head="c" * 40)
    assert (d.status, d.detail) == ("conflict", REASON_HEAD_MISMATCH) and main_sha(bare) == before
    assert not branch_exists(bare, BRANCH)


async def test_branch_equal_to_base_is_failed_not_merged(git, job, app, prepared_box):
    d = await git.deliver(
        job, app, prepared_box, draft=False, expected_head=await head_sha_of(prepared_box)
    )
    assert (d.status, d.detail, d.merge_sha) == ("failed", REASON_NOTHING_TO_DELIVER, None)


async def test_dispatcher_routes_on_repo_kind(git, job, app, box):
    disp = GitDispatcher({"local": git})
    assert len(await disp.prepare(job, app, box)) == 40


def test_safe_git_flags():
    argv = safe_git("status")
    assert argv[0] == "git" and "core.hooksPath=/dev/null" in argv and argv[-1] == "status"
