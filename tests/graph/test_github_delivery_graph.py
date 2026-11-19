"""The graph with a github app: draft PR at approve, open PRs end in stage delivered (M8.4c)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from tests.fakes.deps import make_test_deps, seed_demo
from tests.fakes.github_git import DeliveryGitHub
from tests.graph.test_loop import FIXTURES
from tests.support.db_helpers import job_events

from swfactory.db.tables import Job, JobEvent
from swfactory.domain.types import HumanAction, Stage
from swfactory.engines.fake import FakeScript
from swfactory.gitops.fake import ZERO, FakeGitService
from swfactory.gitops.service import Delivery
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition
from swfactory.queue import branch_name, enqueue_job

pytestmark = pytest.mark.db

MERGE_SHA = "m" * 40
HEAD_SHA = ZERO
GH_APP = {"repo_kind": "github", "repo_url": "https://github.com/acme/web"}
APPS: dict[str, dict[str, Any]] = {
    "gh-manual": {**GH_APP, "merge_policy": "pr_manual", "github": {"ci_safe": True}},
    "gh-auto": {**GH_APP, "merge_policy": "pr_auto_merge", "github": {"ci_safe": True}},
    "gh-unsafe": {**GH_APP, "merge_policy": "pr_manual"},
    "local": {},
}


class GraphGitHubService(FakeGitService):
    """FakeGitService whose deliver talks to a DeliveryGitHub the way GitHubGit does."""

    def __init__(self, gh: DeliveryGitHub) -> None:
        super().__init__()
        self.gh = gh

    async def deliver(self, job, app, box, *, draft, expected_head):  # type: ignore[no-untyped-def]
        if app.repo_kind != "github":
            return await super().deliver(job, app, box, draft=draft, expected_head=expected_head)
        self._call("deliver")
        gh, branch = self.gh, branch_name(job.seq, job.slug)
        pr = await gh.find_pr(branch)
        if pr is None or gh.branches.get(branch) != expected_head:
            gh.branches[branch] = expected_head
            gh.pushes += 1
        if pr is None:
            pr = await gh.create_pr(head=branch, base="main", title="t", body="b", draft=draft)
        if draft:
            return Delivery("pr_open", pr.url, None, "draft PR open")
        if pr.draft:
            await gh.mark_ready(pr)
        if app.merge_policy == "pr_manual":
            return Delivery("pr_open", pr.url, None, "ready")
        try:
            await gh.merge_async(pr.number, sha=expected_head, method="squash")
        except Exception:  # noqa: BLE001  405 blocked: auto-merge enabled on GitHub
            return Delivery("pr_open", pr.url, None, "auto-merge enabled")
        done = await gh.get_pr(pr.number)
        return Delivery("merged", done.url, done.merge_sha, "merged on GitHub")


@pytest.fixture
def github_graph(sessions):
    gh = DeliveryGitHub()
    h = SimpleNamespace(fake_gh=gh, sessions=sessions, job_id=None, graph=None, cfg=None, deps=None)

    async def run(*, app: str, decision: str = "APPROVE") -> dict:
        await seed_demo(
            sessions,
            app=APPS[app],
            apps=(app,),
            defaults={"engine_retry_backoff_s": [0, 0]},
        )
        async with sessions.begin() as s:
            job = await enqueue_job(
                s,
                app_slug=app,
                feature="x",
                title="x",
                source="cli",
                source_ref=None,
                requested_by="me",
            )
        h.job_id = job.id
        if app == "gh-auto":
            gh.merge_blocked = h.blocked
        script = FakeScript.from_yaml(FIXTURES / "happy.yaml")
        h.deps = make_test_deps(sessions, script, git=GraphGitHubService(gh))
        row, defn, _ = await pinned_definition(h.deps, job.id)
        h.graph = build_job_graph(h.deps, InMemorySaver())
        cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
        st = await h.graph.ainvoke(initial_state(row, defn), cfg)
        return await _settle(st)

    async def _settle(st: dict) -> dict:
        if st.get("__interrupt__"):
            return {**st, "gate_reason": st["__interrupt__"][0].value["reason"]}
        return st

    async def resume_with(action: HumanAction) -> dict:
        payload = {"action": action.value, "note": "", "by": "ann", "command_id": ""}
        cfg: RunnableConfig = {"configurable": {"thread_id": str(h.job_id)}}
        return await h.graph.ainvoke(Command(resume=payload), cfg)

    async def job_row() -> Job:
        async with sessions() as s:
            return await s.get_one(Job, h.job_id)

    async def stage_events() -> list[str]:
        async with sessions() as s:
            rows = await s.scalars(
                select(JobEvent.data)
                .where(JobEvent.job_id == h.job_id, JobEvent.kind == "job.stage")
                .order_by(JobEvent.id)
            )
            return [d["stage"] for d in rows]

    async def events(kind: str) -> list[dict]:
        return list(await job_events(sessions, kind))

    h.blocked = False
    h.run, h.resume_with, h.job_row, h.stage_events, h.events = (
        run,
        resume_with,
        job_row,
        stage_events,
        events,
    )
    return h


async def test_approve_opens_draft_pr_once(github_graph):
    await github_graph.run(app="gh-manual")
    assert len(await github_graph.events("delivery.pr_opened")) == 1
    assert github_graph.fake_gh.counts["create_pr"] == 1


async def test_ci_unsafe_app_pushes_nothing_before_a_person_approves(github_graph):
    gate = await github_graph.run(app="gh-unsafe")
    gh = github_graph.fake_gh
    assert gate["gate_reason"] == "human approval required (intake)"
    assert (await github_graph.job_row()).require_human_approval is True
    assert (gh.counts["push"], gh.counts["create_pr"]) == (0, 0)
    final = await github_graph.resume_with(HumanAction.APPROVE)
    assert final["outcome"] == Stage.DELIVERED
    assert gh.counts["push"] == 1 and gh.pr_for("factory/001-x")["draft"] is False


async def test_pr_manual_ends_delivered_with_pr_url(github_graph):
    final = await github_graph.run(app="gh-manual")
    row = await github_graph.job_row()
    assert final["outcome"] == Stage.DELIVERED and row.stage == "delivered"
    assert row.pr_url and row.pr_url.endswith("/pull/1")
    assert "delivering" in await github_graph.stage_events()
    assert github_graph.fake_gh.pr_for("factory/001-x")["draft"] is False


async def test_blocked_auto_merge_ends_delivered(github_graph):
    github_graph.blocked = True
    assert (await github_graph.run(app="gh-auto"))["outcome"] == Stage.DELIVERED


async def test_auto_merge_ends_merged(github_graph):
    final = await github_graph.run(app="gh-auto")
    assert final["outcome"] == Stage.MERGED
    assert await github_graph.events("delivery.merged") == [
        {
            "merge_sha": MERGE_SHA,
            "head_sha": HEAD_SHA,
            "pr_url": "https://github.com/acme/web/pull/1",
        }
    ]


async def test_local_app_unchanged(github_graph):
    assert (await github_graph.run(app="local"))["outcome"] == Stage.MERGED
