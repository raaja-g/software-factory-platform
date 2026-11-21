"""The improver can push a branch and open a pull request; it can never merge."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.fakes.github import FakeGitHub

import swfactory.improve as improve_pkg
from swfactory.domain.types import Role
from swfactory.github.client import GitHubClient
from swfactory.improve.graph import ImproverGitHub
from swfactory.improve.workspace import improve_branch

MERGE_CALLS = {"merge_async", "merge_async_status", "enable_auto_merge", "mark_ready"}
FORBIDDEN_WORDS = ("merge", "approve", "force", "auto_merge")


def _trees() -> list[tuple[Path, ast.AST]]:
    root = Path(improve_pkg.__file__).parent
    return [(p, ast.parse(p.read_text())) for p in sorted(root.glob("*.py"))]


def call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def call_nodes():
    return [n for _, t in _trees() for n in ast.walk(t) if isinstance(n, ast.Call)]


def imported_names() -> set[str]:
    out: set[str] = set()
    for _, t in _trees():
        for n in ast.walk(t):
            if isinstance(n, ast.ImportFrom | ast.Import):
                out |= {a.name for a in n.names}
    return out


def test_improve_package_never_calls_merge():
    assert not ({call_name(n) for n in call_nodes()} | imported_names()) & MERGE_CALLS


def test_improve_package_has_no_force_push_or_approve_calls():
    called = {call_name(n) for n in call_nodes()}
    bad = {c for c in called if "approve" in c or "force_push" in c or "auto_merge" in c}
    assert not bad
    for path, tree in _trees():
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert "--force" not in n.value, path


def test_improver_github_exposes_no_merge_method():
    gh = ImproverGitHub(FakeGitHub())  # type: ignore[arg-type]
    public = {n for n in dir(gh) if not n.startswith("_")}
    # two read-only lookups and the one write; no merge, approve or force-push
    assert public == {"find_pr", "find_open_pr", "create_pr"}
    assert not any(w in n for n in public for w in FORBIDDEN_WORDS)
    assert MERGE_CALLS & public == set()
    # the real client does have them, which is why the improver is handed the narrow wrapper
    assert MERGE_CALLS <= {n for n in dir(GitHubClient)}


async def test_improver_github_only_opens_improver_branches():
    fake = FakeGitHub()
    gh = ImproverGitHub(fake)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await gh.create_pr(head="main", base="main", title="t", body="b")
    pr = await gh.create_pr(head="improve/20261111-builder", base="main", title="t", body="b")
    assert pr.draft is False
    assert fake.calls["create_pr"] == 1


def test_branch_is_under_the_improver_prefix():
    from datetime import date

    for role in Role:
        assert improve_branch(date(2026, 11, 11), role).startswith("improve/")
