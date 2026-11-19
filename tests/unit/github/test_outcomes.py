"""Issue outcome comments and withdrawing an open PR (M8.5b); no database, no network."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.fakes.github_git import DeliveryGitHub, FakeAuth

from swfactory.definition.models import AppDef
from swfactory.domain.types import Stage
from swfactory.github.intake import outcome_comment
from swfactory.gitops.fake import FakeGitService
from swfactory.gitops.github import GitHubGit
from swfactory.gitops.service import GitDispatcher

PR_URL = "https://github.com/acme/web/pull/1"
BRANCH = "factory/001-x"


def test_outcome_comment_per_stage():
    merged = outcome_comment(
        stage=Stage.MERGED, ref="demo-gh:012", label="factory", pr_url=PR_URL, expired_days=None
    )
    assert merged == (
        f"Job demo-gh:012 was merged in {PR_URL}. "
        "Add the `factory` label again to request another run."
    )
    assert "was closed without merging" in outcome_comment(
        stage=Stage.CLOSED, ref="demo-gh:012", label="factory", pr_url=PR_URL, expired_days=None
    )
    assert "was closed after waiting 14 days for a person" in outcome_comment(
        stage=Stage.CLOSED, ref="demo-gh:012", label="factory", pr_url=None, expired_days=14
    )
    assert "was cancelled" in outcome_comment(
        stage=Stage.CANCELLED, ref="demo-gh:012", label="factory", pr_url=None, expired_days=None
    )


def gh_app() -> AppDef:
    return AppDef.model_validate(
        {"repo_kind": "github", "repo_url": "https://github.com/acme/web", "runner": "none"}
    )


@pytest.fixture
def fake_gh() -> DeliveryGitHub:
    return DeliveryGitHub()


def make_git(gh: DeliveryGitHub) -> GitHubGit:
    return GitHubGit(
        auth=FakeAuth(),
        gitops=None,  # type: ignore[arg-type]
        sessions=None,  # type: ignore[arg-type]
        repos_dir="/nonexistent",  # type: ignore[arg-type]
        client_for=lambda repo: gh,  # type: ignore[arg-type,return-value]
    )


JOB: SimpleNamespace = SimpleNamespace(id=uuid4(), seq=1, slug="x")


async def test_withdraw_comments_then_closes_once(fake_gh):
    await fake_gh.create_pr(head=BRANCH, base="main", title="t", body="b")
    git = make_git(fake_gh)
    for _ in range(2):
        await git.withdraw(JOB, gh_app(), note="Cancelled in the factory by ann@x.io.")  # type: ignore[arg-type]
    assert fake_gh.prs[1]["state"] == "closed"
    assert len(fake_gh.comments[1]) == 1 and fake_gh.counts["close_pr"] == 1
    assert "Cancelled in the factory by ann@x.io." in fake_gh.comments[1][0].body


async def test_withdraw_with_no_pr_does_nothing(fake_gh):
    await make_git(fake_gh).withdraw(JOB, gh_app(), note="x")  # type: ignore[arg-type]
    assert fake_gh.counts["close_pr"] == 0 and fake_gh.comments == {}


async def test_withdraw_does_nothing_for_local_apps():
    local = FakeGitService()
    local_app = AppDef.model_validate(
        {"repo_kind": "local", "repo_url": "/tmp/x", "runner": "none"}
    )
    await GitDispatcher({"local": local}).withdraw(JOB, local_app, note="x")  # type: ignore[arg-type]
    assert local.calls == []
