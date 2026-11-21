"""The improver's definition workspace against real git processes (no mocks of git)."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.fakes.github_git import DeliveryGitHub, FakeAuth, FakeGitops
from tests.fakes.stub_sandbox import StubSandbox

import swfactory.improve.workspace as workspace
from swfactory.definition import sync
from swfactory.domain.types import Role
from swfactory.github.auth import PERMS_FETCH, PERMS_PUSH
from swfactory.improve.proposal import FileChange
from swfactory.improve.workspace import (
    DefinitionWorkspace,
    WorkspaceError,
    improve_branch,
    read_changes,
    seed_sandbox,
)
from swfactory.sandbox.paths import REPO

SLUG = "acme/factory-definition"
WHEN = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
FIXED = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
TREE = {
    "factory.yaml": b"mode: x\n",
    "roles/builder.md": b"# builder\n",
    "profiles/local.yaml": b"a: 1\n",
    "benchmarks/v1-parity/checks/003-todo-web/tests/test_web.py": b"def test(): ...\n",
    "benchmarks/v1-parity/tasks/003-todo-web.yaml": b"id: 003\n",
}
# Recorded once from the fixture repo (fixed dates and identity make the sha reproducible).
EXPECTED_SHA = "4a51cb1794f484d0bf708b0861be82303bd8198a"


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **FIXED, **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def make_repo(path: Path, tree: dict[str, bytes], *, bare: bool = False) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, data in tree.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def stub_box(tmp_path: Path) -> StubSandbox:
    return StubSandbox(tmp_path / "box", None)


@pytest.fixture
def local_def(tmp_path: Path) -> Path:
    make_repo(tmp_path / "def", TREE)
    return tmp_path / "def"


def ws_for(local_def: Path, root: Path) -> DefinitionWorkspace:
    return DefinitionWorkspace(str(local_def), root, job_id=uuid4(), auth=None, gitops=None)


CHANGES = [
    FileChange("roles/builder.md", "M", None, "100644", "# builder\n", "# builder v2\n"),
    FileChange("scorers/x.md", "A", None, "100644", None, "x\n"),
]


def test_branch_name():
    assert (
        improve_branch(date(2026, 11, 11), Role.REVIEWER_SECURITY)
        == "improve/20261111-reviewer-security"
    )


def test_branch_name_gets_a_suffix_from_the_improvement_id():
    a = improve_branch(
        date(2026, 11, 11), Role.BUILDER, UUID("12345678-aaaa-bbbb-cccc-1234567890ab")
    )
    assert a == "improve/20261111-builder-12345678"
    assert a == improve_branch(
        date(2026, 11, 11), Role.BUILDER, "12345678-aaaa-bbbb-cccc-1234567890ab"
    )
    other = improve_branch(
        date(2026, 11, 11), Role.BUILDER, UUID("87654321-aaaa-bbbb-cccc-1234567890ab")
    )
    assert other != a  # a re-run the same day is a different improvement, so a new branch


def test_repo_slug_is_the_m8_one():
    assert workspace.definition_repo_slug is sync.definition_repo_slug


async def test_read_changes(stub_box: StubSandbox):
    await seed_sandbox(stub_box, TREE)
    assert await read_changes(stub_box, TREE) == []
    root = stub_box.root / "work" / "repo"
    (root / "roles" / "builder.md").write_text("# builder 2\n")
    (root / "scorers").mkdir()
    (root / "scorers" / "x.md").write_text("x\n")
    (root / "profiles" / "local.yaml").unlink()
    os.symlink("builder.md", root / "roles" / "ln.md")
    got = {(c.path, c.status, c.mode) for c in await read_changes(stub_box, TREE)}
    assert got >= {
        ("roles/builder.md", "M", "100644"),
        ("scorers/x.md", "A", "100644"),
        ("profiles/local.yaml", "D", "000000"),
        ("roles/ln.md", "A", "120000"),
    }


async def test_read_changes_sees_type_and_mode_changes(stub_box: StubSandbox):
    await seed_sandbox(stub_box, TREE)
    root = stub_box.root / "work" / "repo"
    (root / "roles" / "builder.md").unlink()
    os.symlink("/etc/passwd", root / "roles" / "builder.md")
    os.chmod(root / "profiles" / "local.yaml", 0o755)
    got = {(c.path, c.mode) for c in await read_changes(stub_box, TREE)}
    assert got == {("roles/builder.md", "120000"), ("profiles/local.yaml", "100755")}


async def test_read_changes_fails_closed_on_binary_and_git_error(stub_box: StubSandbox):
    await seed_sandbox(stub_box, TREE)
    root = stub_box.root / "work" / "repo"
    (root / "roles" / "builder.md").write_bytes(b"\x00\xff\xfe")
    with pytest.raises(WorkspaceError):
        await read_changes(stub_box, TREE)
    (root / "roles" / "builder.md").write_bytes(b"ok\n")
    import shutil

    shutil.rmtree(root / ".git")
    with pytest.raises(WorkspaceError):
        await read_changes(stub_box, TREE)


async def test_hostile_path_names_come_back_verbatim(stub_box: StubSandbox):
    await seed_sandbox(stub_box, TREE)
    root = stub_box.root / "work" / "repo"
    (root / "roles" / "a\nb.md").write_text("x\n")
    (root / "roles" / "--output=x.md").write_text("x\n")
    paths = {c.path for c in await read_changes(stub_box, TREE)}
    assert paths == {"roles/a\nb.md", "roles/--output=x.md"}


async def test_benchmarks_never_reach_the_sandbox(stub_box: StubSandbox):
    await seed_sandbox(stub_box, TREE)
    assert not list(stub_box.root.rglob("benchmarks"))
    assert await read_changes(stub_box, TREE) == []


async def test_seed_writes_shipped_roles_outside_the_repo(stub_box: StubSandbox):
    base = await seed_sandbox(stub_box, TREE)
    assert len(base) == 40
    assert (stub_box.root / "work/.factory/in/shipped-roles/builder.md").is_file()
    assert not (stub_box.root / "work/repo/shipped-roles").exists()
    assert REPO == "/work/repo"


async def checkout(ws: DefinitionWorkspace, local_def: Path) -> str:
    sha = git(local_def, "rev-parse", "HEAD")
    files = await ws.checkout(sha)
    assert files["roles/builder.md"] == b"# builder\n"
    assert "benchmarks/v1-parity/tasks/003-todo-web.yaml" in files  # the host keeps it
    return sha


async def commit_in(ws: DefinitionWorkspace, local_def: Path) -> str:
    await checkout(ws, local_def)
    return await ws.commit(
        "improve/20261111-builder", CHANGES, message="improve builder", when=WHEN
    )


async def test_commit_is_deterministic(local_def: Path, tmp_path: Path):
    a = await commit_in(ws_for(local_def, tmp_path / "a"), local_def)
    b = await commit_in(ws_for(local_def, tmp_path / "b"), local_def)
    assert a == b == EXPECTED_SHA


async def test_commit_keeps_benchmarks_and_author(local_def: Path, tmp_path: Path):
    ws = ws_for(local_def, tmp_path / "a")
    sha = await commit_in(ws, local_def)
    assert (ws.path / "benchmarks/v1-parity/tasks/003-todo-web.yaml").is_file()
    assert git(ws.path, "log", "-1", "--format=%an|%cn|%aI", sha) == (
        "factory-bot|factory-bot|2026-11-11T09:00:00Z"
    )
    assert (
        git(ws.path, "diff", "--name-status", "main", sha) == "M\troles/builder.md\nA\tscorers/x.md"
    )


@pytest.mark.parametrize(
    "bad",
    [
        FileChange("../x.md", "A", None, "100644", None, "x"),
        FileChange(".git/config", "A", None, "100644", None, "x"),
        FileChange("roles/a.md", "A", None, "120000", None, "x"),
        FileChange("roles/a.md", "A", None, "100755", None, "x"),
        FileChange("/etc/x", "A", None, "100644", None, "x"),
        FileChange("roles\\a.md", "A", None, "100644", None, "x"),
        FileChange("benchmarks/x.yaml", "A", None, "100644", None, "x"),
    ],
)
async def test_commit_refuses_unsafe_changes(bad: FileChange, local_def: Path, tmp_path: Path):
    ws = ws_for(local_def, tmp_path / "a")
    await checkout(ws, local_def)
    with pytest.raises(WorkspaceError):
        await ws.commit("improve/20261111-builder", [bad], message="m", when=WHEN)


async def test_commit_refuses_a_non_improver_branch(local_def: Path, tmp_path: Path):
    ws = ws_for(local_def, tmp_path / "a")
    await checkout(ws, local_def)
    for branch in ("main", "--delete", "improve/../x", "improve/a b"):
        with pytest.raises(WorkspaceError):
            await ws.commit(branch, CHANGES, message="m", when=WHEN)


def rev(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref)


async def test_local_push_creates_branch_only(local_def: Path, tmp_path: Path):
    main_before = rev(local_def, "main")
    ws = ws_for(local_def, tmp_path / "a")
    sha = await commit_in(ws, local_def)
    await ws.push("improve/20261111-builder", sha)
    assert (rev(local_def, "improve/20261111-builder"), rev(local_def, "main")) == (
        sha,
        main_before,
    )
    await ws.push("improve/20261111-builder", sha)  # idempotent


async def test_local_push_to_a_moved_branch_raises(local_def: Path, tmp_path: Path):
    git(local_def, "branch", "improve/20261111-builder", "main")
    ws = ws_for(local_def, tmp_path / "a")
    sha = await commit_in(ws, local_def)
    with pytest.raises(WorkspaceError, match="branch changed"):
        await ws.push("improve/20261111-builder", sha)


async def test_read_only_local_source(local_def: Path, tmp_path: Path):
    ws = ws_for(local_def, tmp_path / "a")
    sha = await commit_in(ws, local_def)
    os.chmod(local_def, 0o555)
    try:
        if os.access(local_def, os.W_OK):
            pytest.skip("running as a user that ignores directory modes")
        with pytest.raises(WorkspaceError, match="read-only"):
            await ws.push("improve/20261111-builder", sha)
    finally:
        os.chmod(local_def, 0o755)


class RecAuth(FakeAuth):
    def __init__(self) -> None:
        super().__init__()
        self.requested: list[tuple[str, dict[str, str]]] = []

    async def token_for(self, repo, permissions):
        self.requested.append((repo, dict(permissions)))
        return await super().token_for(repo, permissions)


class Gitops(FakeGitops):
    def _map(self, job_id, value):
        value = value.replace(f"https://github.com/{SLUG}.git", "REMOTE")
        return super()._map(job_id, value).replace("REMOTE", str(self.origin))

    @property
    def ops(self) -> list[str]:
        names = []
        for argv in self.argvs:
            rest = [a for a in argv if a not in ("-c",)]
            if "init" in rest:
                names.append("init")
            elif "push" in rest:
                names.append("push")
            elif "bundle" in rest:
                names.append("export_bundle")
            elif any(a.endswith("job.bundle") for a in rest):
                names.append("import_bundle")
            elif "fetch" in rest:
                names.append("fetch")
        return names


@pytest.fixture
def gh_env(tmp_path: Path):
    origin = tmp_path / "origin"
    base = make_repo(origin, TREE)
    gh = DeliveryGitHub(SLUG)
    gh.branches["main"] = base
    auth = RecAuth()
    gitops = Gitops(tmp_path / "gitops", origin, gh, auth)
    ws = DefinitionWorkspace(
        f"https://github.com/{SLUG}",
        tmp_path / "ws",
        job_id=uuid4(),
        auth=auth,
        gitops=gitops,  # type: ignore[arg-type]
        client=gh,  # type: ignore[arg-type]
    )
    return ws, gh, gitops, auth, origin, base


async def test_github_push_checks_before_acting(gh_env):
    ws, gh, gitops, _auth, _origin, base = gh_env
    await ws.checkout(base)
    sha = await ws.commit("improve/20261111-builder", CHANGES, message="m", when=WHEN)
    gh.branches["improve/20261111-builder"] = sha
    await ws.push("improve/20261111-builder", sha)
    assert gitops.ops == ["init", "fetch", "export_bundle"]


async def test_github_push_to_a_moved_branch_raises(gh_env):
    ws, gh, _gitops, _auth, _origin, base = gh_env
    await ws.checkout(base)
    sha = await ws.commit("improve/20261111-builder", CHANGES, message="m", when=WHEN)
    gh.branches["improve/20261111-builder"] = "f" * 40
    with pytest.raises(WorkspaceError, match="branch changed on GitHub"):
        await ws.push("improve/20261111-builder", sha)


async def test_tokens_only_reach_gitops(gh_env, monkeypatch: pytest.MonkeyPatch):
    ws, gh, gitops, auth, origin, base = gh_env
    calls: list[dict[str, str]] = []
    real = subprocess.run

    def spy(argv, **kw):
        calls.append(dict(kw.get("env") or {}))
        return real(argv, **kw)

    monkeypatch.setattr(workspace.subprocess, "run", spy)
    await ws.checkout(base)
    sha = await ws.commit("improve/20261111-builder", CHANGES, message="m", when=WHEN)
    await ws.push("improve/20261111-builder", sha)
    assert [(r, p) for r, p in auth.requested] == [(SLUG, PERMS_FETCH), (SLUG, PERMS_PUSH)]
    assert gitops.ops[-2:] == ["import_bundle", "push"]
    assert calls and all("GIT_TOKEN" not in env for env in calls)
    assert gh.branches["improve/20261111-builder"] == sha
    assert rev(origin, "main") == base
