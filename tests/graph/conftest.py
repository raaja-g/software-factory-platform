"""Fixture `step`: seed_demo, one job and a FakeEngine that records every AgentTask."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq
from tests.support.m3_loop import collect

from swfactory.db.repo import set_stage
from swfactory.db.tables import AgentRun
from swfactory.domain.types import Role, Stage
from swfactory.engines.fake import FakeScript
from swfactory.engines.registry import get_engine
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import StepOutcome, _runner, pinned_definition, run_agent_step
from swfactory.queue import enqueue_job
from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.paths import REPO


@pytest.fixture
async def step(sessions):
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    script = FakeScript()
    deps = make_test_deps(sessions, script)
    box = await deps.sandboxes.ensure(job.id, RunnerSpec("standard", "img", 1.0, 1, 1, ()))
    s = SimpleNamespace(deps=deps, job=job.id, box=box, script=script)

    async def run(role: Role, outputs: list[str | None], **kw) -> StepOutcome:
        spec = deps.roles.get(role, definition_sha="x")
        turns = [
            {"role": str(role), "round": 1, "attempt": n, "output": o}
            for n, o in enumerate(outputs, 1)
        ]
        script.entries = [*script.entries, *FakeScript.from_dict({"turns": turns}).entries]
        kw.setdefault("tool_policy", spec.tool_policy)
        return await run_agent_step(
            deps,
            job_id=job.id,
            role=role,
            round=1,
            inputs=[i for i in spec.inputs if not any(c in i for c in "*?[")],
            output_name=spec.output.format(round=1),
            workdir=REPO,
            **kw,
        )

    async def runs(role: Role):
        async with sessions() as db:
            return list(
                (
                    await db.scalars(
                        select(AgentRun)
                        .where(AgentRun.job_id == job.id, AgentRun.role == role.value)
                        .order_by(AgentRun.attempt)
                    )
                ).all()
            )

    def tasks(role: Role):
        return [t for t in get_engine("fake").tasks if t.role == role]  # type: ignore[attr-defined]

    async def put(path: str, text: str) -> None:
        await deps.artifacts.put(job.id, path, text)

    async def text(path: str):
        return await deps.artifacts.get_text(job.id, path)

    s.run, s.runs, s.tasks, s.put, s.text = run, runs, tasks, put, text
    return s


@pytest.fixture
def loop(sessions):
    """Seed a demo app and run one job through the whole graph with a fake engine."""

    async def run(
        script,
        *,
        title="Say hello",
        feature="Add a hello command",
        app={},  # noqa: B006
        diff_paths=(),
        triage_rules=(),
        artifacts={},  # noqa: B006
    ):
        await seed_demo(
            sessions,
            app=app,
            defaults={"engine_retry_backoff_s": [0, 0]},
            triage_rules=triage_rules,
        )
        async with sessions.begin() as s:
            job = await enqueue_job(
                s,
                app_slug="demo",
                feature=feature,
                title=title,
                source="cli",
                source_ref=None,
                requested_by="me",
            )
        deps = make_test_deps(sessions, script, git=FakeGitService(diff_paths=diff_paths))
        for path, text in artifacts.items():
            await deps.artifacts.put(job.id, path, text)
        row, defn, app_def = await pinned_definition(deps, job.id)
        box = await deps.sandboxes.ensure(job.id, _runner(defn, app_def))  # finalize destroys it
        g = build_job_graph(deps, InMemorySaver())
        cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
        st = await g.ainvoke(initial_state(row, defn), cfg)
        if st.get("__interrupt__"):  # what the worker does at the first interrupt
            async with sessions.begin() as s:
                await set_stage(s, job.id, Stage.NEEDS_HUMAN, gate_reason=st.get("gate_reason"))
        return await collect(sessions, deps, job.id, st, box)

    return SimpleNamespace(run=run)
