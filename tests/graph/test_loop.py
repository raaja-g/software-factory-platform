from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, text
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.backlogs import RequirementItem, import_backlog
from swfactory.db.repo import set_stage
from swfactory.db.tables import AgentRun, Job, JobEvent
from swfactory.domain.types import DEFAULT_REVIEWERS, Stage
from swfactory.engines.fake import FakeScript
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
ctx = SimpleNamespace(deps=None, sessions=None)


@pytest.fixture(autouse=True)
def _bind(sessions):
    ctx.sessions = sessions


async def run_job(script=None, *, turns=(), app={}, defaults={}, exec_results=()):  # noqa: B006
    sessions = ctx.sessions
    await seed_demo(sessions, app=app, defaults={"engine_retry_backoff_s": [0, 0], **defaults})
    job = await enq(sessions, app="demo")
    if script is not None:
        fs = FakeScript.from_yaml(FIXTURES / script)
        if turns:
            fs = FakeScript.from_dict({"turns": [*_raw(fs), *turns]})
    else:
        fs = FakeScript.from_dict({"turns": list(turns)})
    ctx.deps = make_test_deps(sessions, fs, exec_results=exec_results)
    row, defn, _ = await pinned_definition(ctx.deps, job.id)
    g = build_job_graph(ctx.deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    st = await g.ainvoke(initial_state(row, defn), cfg)
    return await job_row(job.id), st


def _raw(fs):
    return [
        {
            k: v
            for k, v in {
                "role": e.role,
                "round": e.round,
                "attempt": e.attempt,
                **({"output": e.output} if e.has_output else {}),
            }.items()
            if v is not None
        }
        for e in fs.entries
    ]


async def job_row(job_id):
    async with ctx.sessions() as s:
        return await s.get(Job, job_id, populate_existing=True)


async def rows(job_id, role=None, status=None):
    q = select(AgentRun).where(AgentRun.job_id == job_id).order_by(AgentRun.round, AgentRun.attempt)
    if role:
        q = q.where(AgentRun.role == role)
    if status:
        q = q.where(AgentRun.status == status)
    async with ctx.sessions() as s:
        return list((await s.scalars(q)).all())


async def ok_by_role(job_id):
    out: dict[str, int] = {}
    for r in await rows(job_id, status="ok"):
        out[r.role] = out.get(r.role, 0) + 1
    return out


async def events(job_id, kind):
    async with ctx.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.kind == kind)
                )
            ).all()
        )


def changes(round_):
    return {"role": "reviewer-security", "round": round_, "output": "VERDICT: CHANGES\nx"}


async def test_happy_path_merges():
    job, st = await run_job("happy.yaml")
    assert (job.stage, st["round"]) == ("merged", 1)
    assert await ok_by_role(job.id) == {
        "spec-writer": 1,
        "builder": 1,
        "reviewer-security": 1,
        "reviewer-ux": 1,
        "reviewer-ui": 1,
        "reviewer-code": 1,
        "approver": 1,
    }


async def test_changes_then_pass_round_2():
    job, st = await run_job("rework.yaml")
    assert (job.stage, st["round"], [r.round for r in await rows(job.id, "builder")]) == (
        "merged",
        2,
        [1, 2],
    )
    paths = {m.path for m in await ctx.deps.artifacts.list(job.id)}
    assert {f"round-2/review-{k}.md" for k in DEFAULT_REVIEWERS} <= paths


async def test_changes_in_last_round_go_to_approver():  # L4
    job, _ = await run_job(turns=[changes(r) for r in (1, 2)])
    assert [r.round for r in await rows(job.id, "approver")] == [2]


async def test_approve_with_outstanding_changes_is_overridden():  # L4
    job, st = await run_job(
        turns=[
            *[changes(r) for r in (1, 2)],
            {"role": "approver", "round": 2, "output": "APPROVE\nship it"},
        ]
    )
    assert (st["decision"], st["gate_reason"], st["retry_node"]) == (
        "ESCALATE",
        "approver escalated",
        None,
    )
    assert [e.data for e in await events(job.id, "decision.made")] == [
        {
            "decision": "ESCALATE",
            "round": 2,
            "head_sha": "0" * 40,
            "overridden": True,
            "reason": "outstanding verdicts: security CHANGES",
        }
    ]


async def test_one_reviewer_error_waits_for_all_reviews():
    job, st = await run_job("reviewer-error.yaml")
    assert st["reviews"]["1"]["ux"]["verdict"] == "ERROR"
    done = sorted(r.role for r in await rows(job.id, status="ok") if r.role.startswith("reviewer-"))
    assert done == ["reviewer-code", "reviewer-security", "reviewer-ui"]


def transient(n):
    return {
        "role": "builder",
        "round": 1,
        "attempt": n,
        "status": "failed",
        "error_class": "gateway",
    }


async def test_transient_build_failure_retries_then_merges():
    job, _ = await run_job(turns=[transient(n) for n in (1, 2)])
    assert (job.stage, [r.status for r in await rows(job.id, "builder")]) == (
        "merged",
        ["failed", "failed", "ok"],
    )


async def test_transient_build_failure_exhausted_gates():
    job, st = await run_job(turns=[transient(n) for n in (1, 2, 3)])
    assert (st["gate_reason"], len(await rows(job.id, "builder"))) == ("step failed: build", 3)


async def test_no_test_command_skips_verify():
    job, st = await run_job("happy.yaml", app={"test_command": None})
    assert (st["verify_status"], (await ok_by_role(job.id))["reviewer-ux"]) == ("skipped", 1)


async def test_set_stage_never_leaves_a_terminal_stage():
    job, _ = await run_job("happy.yaml")
    async with ctx.sessions.begin() as s:
        assert await set_stage(s, job.id, Stage.NEEDS_HUMAN, gate_reason="x") is False
    assert (await job_row(job.id)).stage == "merged"


async def test_set_stage_refreshes_the_backlog():
    sessions = ctx.sessions
    await seed_demo(sessions)
    async with sessions.begin() as s:
        bl, jobs = await import_backlog(
            s,
            app_slug="demo",
            title="t",
            requested_by="me",
            items=[
                RequirementItem("a", "A", "do a", (), ()),
                RequirementItem("b", "B", "do b", (), ()),
            ],
        )
    async with sessions.begin() as s:
        assert await set_stage(s, jobs[0].id, Stage.CLOSED) is True
    async with sessions() as s:
        got = await s.scalar(text("select status from app.backlogs where id = :i"), {"i": bl.id})
    assert got == "paused"
