"""Review follow-ups that need no database: GitHub permissions, PR lookup, branch names, the
benchmark relevance guard and the sandbox read-back edge cases."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import Definition
from swfactory.domain.types import Role
from swfactory.github.auth import PERMS_DEFINITION_PR, PERMS_DELIVER
from swfactory.github.client import GitHubClient, MutationQueue, PullRef
from swfactory.improve.graph import ImproverGitHub, improver_client_factory
from swfactory.improve.proposal import FileChange, exercised_by_benchmark
from swfactory.improve.workspace import WorkspaceError, read_changes, seed_sandbox

API = "https://api.github.com"


class SpyAuth:
    def __init__(self) -> None:
        self.asked: list[dict[str, str]] = []

    async def token_for(self, repo: str, permissions: Any) -> str:
        self.asked.append(dict(permissions))
        return "tok"

    async def org_token(self, org: str, permissions: Any) -> str:
        return "tok"


def _pr(state: str = "open", merged: bool = False) -> dict[str, Any]:
    return {
        "number": 7,
        "node_id": "n",
        "html_url": "https://github.com/acme/def/pull/7",
        "head": {"sha": "a" * 40},
        "state": state,
        "draft": False,
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
    }


def test_the_improver_permission_set_is_the_narrow_one() -> None:
    assert PERMS_DEFINITION_PR == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert "issues" not in PERMS_DEFINITION_PR and "checks" not in PERMS_DEFINITION_PR
    assert "issues" in PERMS_DELIVER


@respx.mock(base_url=API, assert_all_called=False)
async def test_improver_client_asks_for_exactly_the_definition_pr_permissions(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = SpyAuth()
    import swfactory.github.auth as auth_mod

    monkeypatch.setattr(auth_mod, "make_auth", lambda settings: auth)
    respx_mock.get("/repos/acme/def/pulls").mock(return_value=httpx.Response(200, json=[_pr()]))
    respx_mock.post("/repos/acme/def/pulls").mock(return_value=httpx.Response(201, json=_pr()))
    client = improver_client_factory(SimpleNamespace(broker_token_file=None))("acme/def")  # type: ignore[arg-type]
    gh = ImproverGitHub(client)
    await gh.find_pr("improve/20261111-builder-12345678")
    await gh.create_pr(head="improve/20261111-builder-12345678", base="main", title="t", body="b")
    assert auth.asked and all(p == PERMS_DEFINITION_PR for p in auth.asked)


@respx.mock(base_url=API, assert_all_called=False)
async def test_default_client_keeps_the_delivery_permissions(
    respx_mock: respx.MockRouter,
) -> None:
    auth = SpyAuth()
    respx_mock.get("/repos/acme/web/pulls").mock(return_value=httpx.Response(200, json=[]))
    await GitHubClient(auth, "acme/web", MutationQueue()).find_pr("x")
    assert auth.asked == [dict(PERMS_DELIVER)]


class _Client:
    def __init__(self, pr: PullRef | None) -> None:
        self.pr = pr

    async def find_pr(self, branch: str) -> PullRef | None:
        return self.pr


def _ref(state: str, merged: bool = False) -> PullRef:
    return PullRef(7, "n", "u", "a" * 40, state, merged, False, None, None)  # type: ignore[arg-type]


async def test_find_open_pr_never_returns_a_closed_or_merged_pr() -> None:
    br = "improve/20261111-builder-12345678"
    assert await ImproverGitHub(_Client(_ref("open"))).find_open_pr(br) is not None  # type: ignore[arg-type]
    assert await ImproverGitHub(_Client(_ref("closed"))).find_open_pr(br) is None  # type: ignore[arg-type]
    assert await ImproverGitHub(_Client(_ref("closed", True))).find_open_pr(br) is None  # type: ignore[arg-type]
    assert await ImproverGitHub(_Client(None)).find_open_pr(br) is None  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await ImproverGitHub(_Client(_ref("open"))).find_open_pr("main")  # type: ignore[arg-type]


# -- the benchmark only counts what it runs -------------------------------------------------

_SHIPPED_RESULT = load_definition(str(defaults_dir()))
assert _SHIPPED_RESULT.definition is not None, _SHIPPED_RESULT.errors
DEFN: Definition = _SHIPPED_RESULT.definition
assert DEFN is not None
SUITE = next(iter(DEFN.benchmarks))
FIRST = DEFN.benchmarks[SUITE].configurations[0]


def _mod(path: str) -> FileChange:
    return FileChange(path, "M", None, "100644", "a", "b")


def test_a_role_file_is_exercised() -> None:
    assert exercised_by_benchmark(DEFN, SUITE, Role.BUILDER, [_mod("roles/builder.md")])


def test_a_profile_the_configuration_uses_is_exercised() -> None:
    assert exercised_by_benchmark(
        DEFN, SUITE, Role.BUILDER, [_mod(f"profiles/{FIRST.profile}.yaml")]
    )


def test_a_profile_the_configuration_does_not_use_is_not() -> None:
    other = next(p for p in DEFN.profiles if p != FIRST.profile)
    assert not exercised_by_benchmark(DEFN, SUITE, Role.BUILDER, [_mod(f"profiles/{other}.yaml")])


def test_a_pinned_model_shadows_the_profile_edit() -> None:
    from swfactory.definition.models import RoleOverride

    pinned = FIRST.model_copy(
        update={"roles": {Role.BUILDER: RoleOverride(model="gemini-pro@vertex")}}
    )
    suite = DEFN.benchmarks[SUITE].model_copy(
        update={"configurations": (pinned, *DEFN.benchmarks[SUITE].configurations[1:])}
    )
    d = DEFN.model_copy(update={"benchmarks": {**DEFN.benchmarks, SUITE: suite}})
    assert not exercised_by_benchmark(
        d, SUITE, Role.BUILDER, [_mod(f"profiles/{FIRST.profile}.yaml")]
    )
    assert not exercised_by_benchmark(d, SUITE, Role.BUILDER, [_mod("scorers/x.md")])
    assert not exercised_by_benchmark(
        DEFN, "no-such-suite", Role.BUILDER, [_mod("roles/builder.md")]
    )


# -- reading the sandbox back ---------------------------------------------------------------

TREE = {"roles/builder.md": b"# builder\n", "scorers/s.md": b"s\n"}


@pytest.fixture
async def box(tmp_path: Path) -> StubSandbox:
    b = StubSandbox(tmp_path / "box", None)
    await seed_sandbox(b, TREE)
    return b


async def test_git_history_games_do_not_change_the_difference(box: StubSandbox) -> None:
    root = box.root / "work" / "repo"
    (root / "roles" / "builder.md").write_text("# forbidden\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@a",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@a",
    }
    import subprocess

    for argv in (
        ["git", "commit", "-qam", "x"],
        ["git", "commit", "-q", "--amend", "-m", "y"],
        ["git", "update-index", "--assume-unchanged", "roles/builder.md"],
        ["git", "tag", "base", "HEAD"],
    ):
        subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)
    got = await read_changes(box, TREE)
    assert [(c.path, c.status, c.old_text) for c in got] == [
        ("roles/builder.md", "M", "# builder\n")
    ]


async def test_a_pipe_or_oversize_file_fails_closed(box: StubSandbox) -> None:
    root = box.root / "work" / "repo"
    os.mkfifo(root / "roles" / "pipe.md")
    with pytest.raises(WorkspaceError):
        await read_changes(box, TREE)
    os.unlink(root / "roles" / "pipe.md")
    (root / "roles" / "big.md").write_bytes(b"x" * (2 * 2**20 + 1))
    with pytest.raises(WorkspaceError, match="too large"):
        await read_changes(box, TREE)


async def test_an_executable_bit_is_a_change_and_benchmarks_are_ignored(box: StubSandbox) -> None:
    root = box.root / "work" / "repo"
    os.chmod(root / "roles" / "builder.md", 0o755)
    (root / "benchmarks").mkdir()
    (root / "benchmarks" / "x.yaml").write_text("x\n")
    got = await read_changes(box, TREE)
    assert [(c.path, c.mode) for c in got] == [("roles/builder.md", "100755")]
