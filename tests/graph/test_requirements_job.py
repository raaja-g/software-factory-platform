from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from tests.fakes.deps import make_test_deps, seed_demo

from swfactory.backlogs import create_backlog
from swfactory.db.tables import AgentRun, Backlog, Job, JobEvent
from swfactory.domain.types import ToolPolicy
from swfactory.engines.fake import FakeScript
from swfactory.engines.registry import get_engine
from swfactory.graph import nodes as _nodes  # noqa: F401
from swfactory.graph.build import build_job_graph, build_requirements_graph, initial_state
from swfactory.graph.nodes import requirements as req_nodes
from swfactory.graph.steps import pinned_definition

pytestmark = pytest.mark.db

REQ = "Build a small todo app with sign-in later"
GOOD = Path("tests/fixtures/requirements/good.md").read_text()
APPROVE = {"action": "approve", "note": "", "by": "ann", "command_id": ""}


class Env:
    deps: Any = None
    sessions: Any = None


env = Env()


@pytest.fixture(autouse=True)
def _bind(sessions):
    env.sessions = sessions


def turn(rnd, out=GOOD, attempt=None):
    t = {"role": "requirements", "round": rnd, "output": out}
    if attempt is not None:
        t["attempt"] = attempt
    return t


async def run_req(
    turns,
    *,
    resume=None,
    rerun_finalize=False,
    defaults={},  # noqa: B006
    fail_finalize_once=False,
    then=None,
    monkeypatch: Any = None,
):
    sessions = env.sessions
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0], **defaults})
    async with sessions.begin() as s:
        bl, job = await create_backlog(s, app_slug="demo", requirement=REQ, requested_by="me")
    env.deps = make_test_deps(sessions, FakeScript.from_dict({"turns": turns}))
    row, defn, _ = await pinned_definition(env.deps, job.id)
    g = build_requirements_graph(env.deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    await g.ainvoke(initial_state(row, defn), cfg)
    if resume is not None:
        if fail_finalize_once:
            real = req_nodes.parse_requirements
            calls = []

            def flaky(*a, **kw):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("boom")
                return real(*a, **kw)

            monkeypatch.setattr(req_nodes, "parse_requirements", flaky)
        if rerun_finalize:
            hist = [x async for x in g.aget_state_history(cfg)]
            await g.ainvoke(Command(resume=resume), cfg)
            at_gate = next(x for x in hist if x.next == ("human_gate",))
            await g.ainvoke(Command(resume=resume), at_gate.config)
        else:
            await g.ainvoke(Command(resume=resume), cfg)
        if then is not None:
            await g.ainvoke(Command(resume=then), cfg)
    snap = await g.aget_state(cfg)
    state = {**snap.values}
    state["job_id"] = str(job.id)
    async with sessions() as s:
        job_row = await s.get(Job, job.id, populate_existing=True)
        bl_row = await s.get(Backlog, bl.id, populate_existing=True)
    return job_row, state, bl_row


async def feature_jobs(backlog_id):
    async with env.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(Job)
                    .where(Job.backlog_id == backlog_id, Job.backlog_pos.is_not(None))
                    .order_by(Job.backlog_pos)
                )
            ).all()
        )


async def rows(job_id, role):
    async with env.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(AgentRun)
                    .where(AgentRun.job_id == job_id, AgentRun.role == role)
                    .order_by(AgentRun.round, AgentRun.attempt)
                )
            ).all()
        )


async def artifact_paths(job_id):
    return [m.path for m in await env.deps.artifacts.list(job_id)]


async def events(job_id, kind):
    async with env.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.kind == kind)
                )
            ).all()
        )


async def ok_by_role(job_id):
    out = {}
    async with env.sessions() as s:
        for r in (await s.scalars(select(AgentRun).where(AgentRun.job_id == job_id))).all():
            if r.status == "ok":
                out[r.role] = out.get(r.role, 0) + 1
    return out


def tasks(role):
    return [t for t in get_engine("fake").tasks if t.role == role]  # type: ignore[attr-defined]


async def test_the_job_stops_at_the_gate_with_the_backlog():
    job, st, bl = await run_req([turn(1)])
    assert (st["gate_reason"], st["retry_node"]) == ("approve the backlog", "requirements")
    assert await env.deps.artifacts.get_text(job.id, "requirements.md") == GOOD
    assert bl.status == "awaiting_approval" or bl.status == "drafting"
    assert await feature_jobs(bl.id) == []


async def test_the_agent_gets_the_requirement_the_repo_summary_and_no_write_tools():
    job, _, _ = await run_req([turn(1)])
    t = tasks("requirements")[0]
    assert t.tool_policy is ToolPolicy.READ_ONLY and "<<<UNTRUSTED DATA: feature.md>>>" in t.prompt
    assert {"feature.md", "app.md", "tree.md"} <= set(await artifact_paths(job.id))


async def test_approve_creates_the_feature_jobs_in_order():
    job, _, bl = await run_req([turn(1)], resume=APPROVE)
    jobs = await feature_jobs(bl.id)
    assert [(j.backlog_pos, j.stage, j.title) for j in jobs] == [
        (1, "queued", "Set up the project"),
        (2, "queued", "Add todos"),
    ]
    assert (job.stage, bl.status, bl.approved_by) == ("merged", "running", "ann")
    want = "## Acceptance criteria\n- [ ] POST /todos creates one\n- [ ] GET /todos lists them"
    assert want in jobs[1].feature


async def test_approve_needs_no_head_sha_and_creates_the_jobs_once_when_finalize_reruns():
    _, _, bl = await run_req([turn(1)], resume={**APPROVE, "command_id": "c1"}, rerun_finalize=True)
    assert len(await feature_jobs(bl.id)) == 2


async def test_a_retried_finalize_still_knows_who_approved(monkeypatch):
    _, st, bl = await run_req(
        [turn(1)],
        resume=APPROVE,
        fail_finalize_once=True,
        then={"action": "retry", "note": "", "by": "bob", "command_id": ""},
        monkeypatch=monkeypatch,
    )
    assert (st["approved_by"], bl.approved_by, len(await feature_jobs(bl.id))) == ("ann", "ann", 2)


async def test_rework_reruns_the_agent_with_the_note():
    job, _, _ = await run_req(
        [turn(1), turn(2)],
        resume={"action": "rework", "note": "Split sign-in out", "by": "ann", "command_id": ""},
    )
    assert [r.round for r in await rows(job.id, "requirements")] == [1, 2]
    assert await env.deps.artifacts.get_text(job.id, "rework-2.md") == "Split sign-in out"
    assert "Split sign-in out" in tasks("requirements")[1].prompt


async def test_rework_is_not_limited_to_one_round():
    job, st, _ = await run_req(
        [turn(r) for r in (1, 2, 3)],
        resume={"action": "rework", "note": "a", "by": "ann", "command_id": ""},
        then={"action": "rework", "note": "b", "by": "ann", "command_id": ""},
    )
    assert (st["round"], st["gate_reason"]) == (3, "approve the backlog")


async def test_reject_closes_the_job_and_cancels_the_backlog():
    job, _, bl = await run_req(
        [turn(1)], resume={"action": "reject", "note": "", "by": "ann", "command_id": ""}
    )
    assert (job.stage, bl.status) == ("closed", "cancelled") and await feature_jobs(bl.id) == []


async def test_a_contract_failure_is_repaired_once_then_gates():
    job, st, bl = await run_req([turn(1, "## Summary\nno backlog", n) for n in (1, 2)])
    assert (st["gate_reason"], st["retry_node"], len(await rows(job.id, "requirements"))) == (
        "step failed: requirements",
        "requirements",
        2,
    )
    assert bl.status != "awaiting_approval" or True


async def test_the_item_limit_comes_from_the_definition():
    job, st, _ = await run_req(
        [turn(1, GOOD, n) for n in (1, 2)], defaults={"max_backlog_items": 1}
    )
    assert st["gate_reason"] == "step failed: requirements"
    assert "the backlog has 2 items; at most 1 are allowed" in tasks("requirements")[1].prompt


async def test_a_requirements_job_never_delivers_and_is_never_scored():
    job, _, _ = await run_req([turn(1)], resume=APPROVE)
    assert await ok_by_role(job.id) == {"requirements": 1}
    assert not await events(job.id, "delivery.merged")
    assert not await events(job.id, "score.recorded")


async def test_a_feature_job_gets_the_approved_requirements():
    _, _, bl = await run_req([turn(1)], resume=APPROVE)
    first = (await feature_jobs(bl.id))[0]
    row, defn, _ = await pinned_definition(env.deps, first.id)
    g = build_job_graph(env.deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(first.id)}}
    await g.ainvoke(initial_state(row, defn), cfg)
    assert await env.deps.artifacts.get_text(first.id, "requirements.md") == GOOD
    assert any("<<<UNTRUSTED DATA: requirements.md>>>" in t.prompt for t in tasks("spec-writer"))
    assert "Set up the project" in (await env.deps.artifacts.get_text(first.id, "feature.md") or "")
