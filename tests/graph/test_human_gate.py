import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select
from tests.fakes.deps import make_test_deps, seed_demo
from tests.graph.test_loop import FIXTURES, _bind, ctx, events, job_row, rows  # noqa: F401
from tests.support.db_helpers import enq

from swfactory.db.tables import Artifact
from swfactory.domain.types import DEFAULT_REVIEWERS
from swfactory.engines.fake import FakeScript
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition

pytestmark = pytest.mark.db

run = SimpleRun = None


async def _start(script, git=None):
    sessions = ctx.sessions
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    ctx.deps = make_test_deps(sessions, FakeScript.from_yaml(FIXTURES / script), git=git)
    row, defn, _ = await pinned_definition(ctx.deps, job.id)
    g = build_job_graph(ctx.deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    res = await g.ainvoke(initial_state(row, defn), cfg)
    return g, cfg, job, res


async def gate_value(script, **kw):
    _, _, _, res = await _start(script, **kw)
    return res["__interrupt__"][0].value


async def resume_job(script, payload, *, rerun_gate=False, new_head=None):
    g, cfg, job, _ = await _start(script)
    if new_head is not None:
        ctx.deps.sandboxes.boxes[job.id].head = new_head
    if rerun_gate:
        hist = [s async for s in g.aget_state_history(cfg)]
        at_gate = next(s for s in hist if s.next == ("human_gate",))
        await g.ainvoke(Command(resume=payload), cfg)
        st = await g.ainvoke(Command(resume=payload), at_gate.config)
    else:
        st = await g.ainvoke(Command(resume=payload), cfg)
    return await job_row(job.id), st


async def artifact_row_count(job_id, path):
    async with ctx.sessions() as s:
        return await s.scalar(
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.job_id == job_id, Artifact.path == path)
        )


def act(action, note="", command_id=""):
    return {"action": action, "note": note, "by": "ann", "command_id": command_id}


async def test_one_reviewer_error_waits_for_all():
    assert await gate_value("reviewer-error.yaml") == {
        "reason": "reviewer failed",
        "retry_node": "verify",
        "artifacts": [
            "round-1/review-code.md",
            "round-1/review-security.md",
            "round-1/review-ui.md",
        ],
    }


async def test_escalate_interrupt_lists_decision():
    value = await gate_value("escalate.yaml")
    assert (value["artifacts"][0], value["retry_node"]) == ("decision.md", None)


async def test_rework_after_escalate():
    job, st = await resume_job("escalate.yaml", act("rework", "Add a test"))
    assert (job.stage, st["round"], st["max_rounds"]) == ("merged", 2, 2)
    assert await ctx.deps.artifacts.get_text(job.id, "rework-2.md") == "Add a test"


async def test_reject_closes():
    job, _ = await resume_job("escalate.yaml", act("reject"))
    assert job.stage == "closed"


async def test_retry_reruns_failed_review_only():
    job, _ = await resume_job("reviewer-error.yaml", act("retry"))
    got = {k: [r.attempt for r in await rows(job.id, f"reviewer-{k}")] for k in DEFAULT_REVIEWERS}
    assert got == {"security": [1], "ux": [1, 2], "ui": [1], "code": [1]}


async def test_retry_after_the_code_changed_reruns_every_review():
    job, _ = await resume_job("reviewer-error.yaml", act("retry"), new_head="1" * 40)
    got = {k: [r.attempt for r in await rows(job.id, f"reviewer-{k}")] for k in DEFAULT_REVIEWERS}
    assert got == {k: [1, 2] for k in DEFAULT_REVIEWERS}
    assert {r.head_sha for r in await rows(job.id, "reviewer-security")} == {"0" * 40, "1" * 40}


async def test_node_exception_goes_to_gate():
    value = await gate_value("happy.yaml", git=FakeGitService(fail_on="prepare"))
    assert value == {"reason": "step failed: prepare", "retry_node": "prepare", "artifacts": []}


async def test_human_action_emitted_once():
    job, _ = await resume_job("escalate.yaml", act("rework", "Add a test", "c1"), rerun_gate=True)
    assert [e.data for e in await events(job.id, "human.action")] == [
        {"action": "rework", "by": "ann", "command_id": "c1", "status": "applied"}
    ]
    assert await artifact_row_count(job.id, "rework-2.md") == 1
