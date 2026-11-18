from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from tests.fakes.host_sandbox import HostSandbox
from tests.unit.gitops.conftest import box_git, commit_file, make_layout
from tests.unit.gitops.conftest import git_cmd as git

from swfactory.gitops.folder import FolderError, FolderOutsideRoot, container_path
from swfactory.gitops.local import LocalGit
from swfactory.gitops.service import REASON_FOLDER_DIRTY, REASON_FOLDER_MOVED


async def head_sha_of(box: HostSandbox) -> str:
    return (await box.exec(["git", "rev-parse", "HEAD"])).stdout.strip()


@pytest_asyncio.fixture
async def edited_box(folder_git, job, folder_app, box) -> HostSandbox:
    await folder_git.prepare(job, folder_app, box)
    await box.write_text("/work/repo/src/app.py", "def greet() -> str:\n    return 'hi'\n")
    await box_git(box, "add", "-A")
    await box_git(box, "commit", "-q", "-m", "add app")
    return box


def test_container_path_maps_and_refuses():
    assert container_path("/Users/me/work/app", "/Users/me/work", "/host-repos") == Path(
        "/host-repos/app"
    )
    assert container_path("/Users/me/work", "/Users/me/work", "/host-repos") == Path("/host-repos")
    for bad in ("/Users/me/workshop/app", "/Users/me/work/../x", "/etc/passwd", "app"):
        with pytest.raises(FolderOutsideRoot):
            container_path(bad, "/Users/me/work", "/host-repos")


@pytest.mark.parametrize(
    "layout, expected",
    [("on_feature_branch", "main"), ("origin_head_trunk", "trunk"), ("master_only", "master")],
)
async def test_default_branch_detection(folders, tmp_path, layout, expected):
    assert await folders.detect_default_branch(make_layout(tmp_path / "work", layout)) == expected


async def test_adopt_seeds_a_bare_repo_by_copy(folders, folder_app, host_folder, tmp_path):
    assert await folders.adopt("app", folder_app.local_path) == "main"
    bare = tmp_path / "repos" / "app.git"
    assert git(bare, "rev-parse", "main") == git(host_folder, "rev-parse", "main")
    assert [
        p for p in (bare / "objects").rglob("*") if p.is_file() and p.stat().st_nlink > 1
    ] == []  # --no-local: nothing is hard-linked to the folder


async def test_adopt_gives_an_empty_folder_a_first_commit(folders, tmp_path):
    empty = tmp_path / "work" / "new"
    empty.mkdir()
    assert await folders.adopt("new", str(empty)) == "main"
    assert (
        git(empty, "log", "--format=%an|%s") == "factory-bot|factory: initial commit"
        and git(empty, "ls-files") == ""
    )


async def test_adopt_refuses_a_non_repo_with_files(folders, tmp_path):
    d = tmp_path / "work" / "docs"
    d.mkdir()
    (d / "a.txt").write_text("x")
    with pytest.raises(FolderError, match="not a git repository"):
        await folders.adopt("docs", str(d))


async def test_adopt_refuses_a_folder_outside_the_root(folders, tmp_path):
    with pytest.raises(FolderOutsideRoot):
        await folders.adopt("x", str(tmp_path / "elsewhere"))


async def test_fetch_default_follows_the_folder(folders, folder_app, host_folder, tmp_path):
    await folders.adopt("app", folder_app.local_path)
    commit_file(host_folder, "b.txt", "b")
    bare = tmp_path / "repos" / "app.git"
    assert (
        await folders.fetch_default(folder_app)
        == git(host_folder, "rev-parse", "main")
        == git(bare, "rev-parse", "main")
    )
    git(host_folder, "reset", "--hard", "HEAD~1")
    commit_file(host_folder, "c.txt", "other")  # a rewritten main
    assert await folders.fetch_default(folder_app) == git(host_folder, "rev-parse", "main")


async def test_update_folder_with_another_branch_checked_out(
    folders, folder_app, host_folder, bare_ahead, tmp_path
):
    git(host_folder, "switch", "-c", "feature")
    assert (
        await folders.update_folder(folder_app, branch="factory/001-x", merge_sha=bare_ahead)
        is None
    )
    assert (
        git(host_folder, "rev-parse", "main") == bare_ahead
        and git(host_folder, "branch", "--show-current") == "feature"
    )
    assert git(host_folder, "rev-parse", "factory/001-x") == git(
        tmp_path / "repos" / "app.git", "rev-parse", "factory/001-x"
    )


async def test_update_folder_fast_forwards_a_checked_out_main(
    folders, folder_app, host_folder, bare_ahead
):
    assert (
        await folders.update_folder(folder_app, branch="factory/001-x", merge_sha=bare_ahead)
        is None
    )
    assert (
        git(host_folder, "rev-parse", "HEAD") == bare_ahead
        and (host_folder / "c.txt").read_text() == "c"
    )


@pytest.mark.parametrize("dirt, name", [("edit", "README.md"), ("untracked", "new.txt")])
async def test_dirty_tree_is_refused_and_left_alone(
    folders, folder_app, host_folder, bare_ahead, dirt, name
):
    before = git(host_folder, "rev-parse", "HEAD")
    (host_folder / name).write_text("mine")
    assert await folders.check_clean(folder_app) == REASON_FOLDER_DIRTY
    assert (
        await folders.update_folder(folder_app, branch="factory/001-x", merge_sha=bare_ahead)
        == REASON_FOLDER_DIRTY
    )
    assert (
        git(host_folder, "rev-parse", "HEAD") == before
        and (host_folder / name).read_text() == "mine"
    )
    assert (
        git(host_folder, "branch", "--list", "factory/001-x") == ""
    )  # nothing was pushed into the folder either


async def test_main_that_cannot_fast_forward_is_refused(
    folders, folder_app, host_folder, bare_ahead
):
    commit_file(host_folder, "mine.txt", "m")  # the folder has a commit the bare repo lacks
    assert (
        await folders.update_folder(folder_app, branch="factory/001-x", merge_sha=bare_ahead)
        == REASON_FOLDER_MOVED
    )
    assert git(host_folder, "log", "-1", "--format=%s") == "add mine.txt"


async def test_folder_hooks_and_config_never_run(
    folders, folder_app, host_folder, bare_ahead, tmp_path
):
    hook = host_folder / ".git/hooks/post-merge"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/pwned\n")
    hook.chmod(0o755)
    git(host_folder, "config", "core.fsmonitor", f"touch {tmp_path}/pwned2")
    await folders.update_folder(folder_app, branch="factory/001-x", merge_sha=bare_ahead)
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


async def test_prepare_and_rebase_follow_the_folder(folder_git, folder_app, host_folder, job, box):
    base1 = await folder_git.prepare(job, folder_app, box)
    commit_file(host_folder, "b.txt", "b")
    assert await folder_git.rebase(job, folder_app, box) is True
    assert (
        await folder_git.base_head(job, folder_app)
        == git(host_folder, "rev-parse", "main")
        != base1
    )


async def test_deliver_merges_into_the_folder(folder_git, folder_app, host_folder, job, edited_box):
    head = await head_sha_of(edited_box)
    d = await folder_git.deliver(job, folder_app, edited_box, draft=False, expected_head=head)
    assert (
        d.status == "merged"
        and git(host_folder, "rev-parse", "main") == d.merge_sha
        and (host_folder / "src/app.py").exists()
    )
    assert git(host_folder, "rev-parse", "factory/001-add-greeting") == head
    assert (
        await folder_git.deliver(job, folder_app, edited_box, draft=False, expected_head=head) == d
    )  # idempotent


async def test_deliver_into_a_dirty_folder_waits_then_succeeds(
    folder_git, folder_app, host_folder, job, edited_box, tmp_path
):
    head = await head_sha_of(edited_box)
    base = git(host_folder, "rev-parse", "main")
    (host_folder / "README.md").write_text("mine")
    d = await folder_git.deliver(job, folder_app, edited_box, draft=False, expected_head=head)
    assert (d.status, d.detail, d.merge_sha) == ("failed", REASON_FOLDER_DIRTY, None)
    assert (
        git(tmp_path / "repos" / "app.git", "rev-parse", "main") == base
        and (host_folder / "README.md").read_text() == "mine"
    )
    git(host_folder, "checkout", "--", "README.md")  # the person cleans up, then Retry
    assert (
        await folder_git.deliver(job, folder_app, edited_box, draft=False, expected_head=head)
    ).status == "merged"


async def test_deliver_when_the_folders_main_moved_on(
    folder_git, folder_app, host_folder, job, edited_box
):
    commit_file(host_folder, "b.txt", "b")  # after prepare, before deliver
    d = await folder_git.deliver(
        job, folder_app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
    )
    assert (d.status, d.detail) == ("failed", REASON_FOLDER_MOVED) and git(
        host_folder, "log", "-1", "--format=%s"
    ) == "add b.txt"
    assert await folder_git.rebase(job, folder_app, edited_box) is True  # what Retry does
    assert (
        await folder_git.deliver(
            job, folder_app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
        )
    ).status == "merged"


async def test_deliver_needs_the_host_root_setting(tmp_path, folder_app, job, edited_box):
    d = await LocalGit(tmp_path / "repos").deliver(
        job, folder_app, edited_box, draft=False, expected_head=await head_sha_of(edited_box)
    )
    assert d.status == "failed" and "FACTORY_HOST_REPOS_ROOT" in d.detail
