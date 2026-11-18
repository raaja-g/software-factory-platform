from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from tests.fakes.host_sandbox import HostSandbox

from swfactory.db.tables import Job
from swfactory.definition.models import AppDef, MergePolicy
from swfactory.gitops.folder import FolderRepo
from swfactory.gitops.local import LocalGit

SEED = Path(__file__).resolve().parents[2] / "fixtures" / "repos" / "pyapp"
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def host_git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


async def box_git(box: HostSandbox, *args: str) -> str:
    r = await box.exec(["git", *IDENT, *args])
    assert r.exit_code == 0, r.stderr
    return r.stdout.strip()


def advance_main(bare: Path, tmp: Path, name: str, text: str, *, readme: bool = False) -> None:
    work = tmp / f"adv-{uuid.uuid4().hex[:6]}"
    host_git(tmp, "clone", "-q", str(bare), str(work))
    if readme:
        lines = (work / "README.md").read_text().splitlines(keepends=True)
        lines[0] = text + "\n"
        (work / "README.md").write_text("".join(lines))
    else:
        (work / name).write_text(text)
    host_git(work, "add", "-A")
    host_git(work, "commit", "-q", "-m", "advance main")
    host_git(work, "push", "-q", "origin", "main")


@pytest.fixture
def git(tmp_path: Path) -> LocalGit:
    return LocalGit(tmp_path / "repos")


@pytest.fixture
def bare(git: LocalGit) -> Path:
    return git.init_bare("pyapp", seed=SEED)


@pytest.fixture
def app(bare: Path) -> AppDef:
    return AppDef(repo_kind="local", repo_url="pyapp")


@pytest.fixture
def pr_app() -> AppDef:
    return AppDef.model_construct(
        repo_kind="local", repo_url="pyapp", merge_policy="pr_manual", default_branch="main"
    )


@pytest.fixture
def job() -> Job:
    return Job(id=uuid.uuid4(), seq=1, slug="add-greeting")


@pytest.fixture
def box(tmp_path: Path) -> HostSandbox:
    return HostSandbox(tmp_path / "box")


@pytest_asyncio.fixture
async def prepared_box(git, job, app, box, bare) -> HostSandbox:
    await git.prepare(job, app, box)
    return box


async def _edit(box: HostSandbox, readme: str = "# pyapp edited") -> None:
    text = await box.read_text("/work/repo/README.md")
    assert text is not None
    lines = text.splitlines(keepends=True)
    lines[0] = readme + "\n"
    await box.write_text("/work/repo/README.md", "".join(lines))


@pytest_asyncio.fixture
async def edited_box(prepared_box: HostSandbox) -> HostSandbox:
    box = prepared_box
    await _edit(box)
    app_py = await box.read_text("/work/repo/src/app.py")
    await box.write_text(
        "/work/repo/src/app.py", (app_py or "") + "def greet() -> str:\n    return 'hi'\n"
    )
    await box_git(box, "add", "-A")
    await box_git(box, "commit", "-q", "-m", "edit")
    return box


@pytest_asyncio.fixture
async def conflicting_box(prepared_box: HostSandbox, bare: Path, tmp_path: Path) -> HostSandbox:
    await _edit(prepared_box, "# branch title")
    await box_git(prepared_box, "commit", "-q", "-am", "branch edit")
    advance_main(bare, tmp_path, "", "# main title", readme=True)
    return prepared_box


@pytest_asyncio.fixture
async def planted_box(edited_box: HostSandbox, bare: Path, tmp_path: Path) -> HostSandbox:
    box = edited_box
    advance_main(bare, tmp_path, "other.txt", "x")
    await box_git(box, "config", "core.fsmonitor", f"touch {box.root}/pwned")
    hook = box.root / "work/repo/.git/hooks/post-rewrite"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {box.root}/pwned\n")
    hook.chmod(0o755)
    return box


@pytest_asyncio.fixture
async def replaced_box(edited_box: HostSandbox) -> HostSandbox:
    box = edited_box
    head = await box_git(box, "rev-parse", "HEAD")
    await box_git(box, "switch", "-q", "-c", "scratch", "origin/main")
    await _edit(box, "# only readme")
    await box_git(box, "commit", "-q", "-am", "readme only")
    other = await box_git(box, "rev-parse", "HEAD")
    await box_git(box, "switch", "-q", "factory/001-add-greeting")
    await box_git(box, "replace", head, other)
    return box


# -- local folder fixtures (M4.5b) -----------------------------------------

_FOLDER_IDENT = [
    "-c",
    "user.name=tester",
    "-c",
    "user.email=tester@localhost",
    "-c",
    "protocol.file.allow=always",
]


def git_cmd(path: Path, *args: str) -> str:
    """Plain `git -C path ...` with a fixed identity; returns stripped stdout."""
    r = subprocess.run(
        ["git", *_FOLDER_IDENT, "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def commit_file(path: Path, name: str, text: str) -> None:
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    git_cmd(path, "add", "-A")
    git_cmd(path, "commit", "-q", "-m", f"add {name}")


def make_folder(parent: Path, name: str, *, branch: str = "main") -> Path:
    d = parent / name
    d.mkdir(parents=True)
    git_cmd(d, "init", "-q", "-b", branch)
    commit_file(d, "README.md", "# app\n")
    return d


def make_layout(parent: Path, name: str) -> Path:
    if name == "on_feature_branch":
        d = make_folder(parent, name)
        git_cmd(d, "switch", "-q", "-c", "feature")
    elif name == "origin_head_trunk":
        d = make_folder(parent, name, branch="trunk")
        git_cmd(d, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git_cmd(d, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        git_cmd(d, "branch", "-m", "other")
    elif name == "master_only":
        d = make_folder(parent, name, branch="master")
    else:
        raise ValueError(name)
    return d


@pytest.fixture
def host_folder(tmp_path: Path) -> Path:
    return make_folder(tmp_path / "work", "app")


@pytest.fixture
def folders(tmp_path: Path) -> FolderRepo:
    (tmp_path / "work").mkdir(exist_ok=True)
    return FolderRepo(str(tmp_path / "work"), str(tmp_path / "work"), bare_dir=tmp_path / "repos")


@pytest.fixture
def folder_app(host_folder: Path) -> AppDef:
    return AppDef(
        repo_kind="local",
        repo_url="app",
        local_path=str(host_folder),
        merge_policy=MergePolicy.LOCAL_MERGE,
    )


@pytest.fixture
def folder_git(tmp_path: Path, folders: FolderRepo) -> LocalGit:
    return LocalGit(tmp_path / "repos", folders=folders)


@pytest_asyncio.fixture
async def bare_ahead(folders: FolderRepo, folder_app: AppDef, tmp_path: Path) -> str:
    await folders.adopt("app", folder_app.local_path or "")
    bare = tmp_path / "repos" / "app.git"
    work = tmp_path / "ahead"
    git_cmd(tmp_path, "clone", "-q", str(bare), str(work))
    commit_file(work, "c.txt", "c")
    git_cmd(work, "push", "-q", "origin", "main")
    sha = git_cmd(work, "rev-parse", "HEAD")
    git_cmd(bare, "branch", "factory/001-x", sha)
    return sha
