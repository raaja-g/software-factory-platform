"""M10.11: a benchmark job closes without delivery, runs hidden checks in an isolated box."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.bench.runner import HIDDEN_CHECKS_BOX
from swfactory.db.tables import AgentRun, BenchResult, BenchRun, Job, JobEvent
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef
from swfactory.domain.types import JobKind
from swfactory.engines.fake import FakeScript
from swfactory.graph import build as build_mod
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition
from swfactory.sandbox.base import ExecResult
from swfactory.scoring import service as svc_mod
from swfactory.scoring.objective import ScoreRow
from swfactory.scoring.service import ScoringService

pytestmark = pytest.mark.db

SHA = "a" * 40
HIDDEN_TEST_BROWSER = "import unittest\nclass B(unittest.TestCase):\n    def test_b(self): pass\n"
HIDDEN_TEST_WEB = "import unittest\nclass W(unittest.TestCase):\n    def test_w(self): pass\n"
TASK = BenchTaskDef(
    title="todo web",
    feature="Add a todo web page",
    success_criteria=("lists todos", "adds a todo"),
    repo="local:demo",
    base_sha=SHA,
    merge_sha=SHA,
    definition_sha=SHA,
    check_command="python3 -m unittest -q {file}",
    check_files={
        "tests/test_browser.py": HIDDEN_TEST_BROWSER,
        "tests/test_web.py": HIDDEN_TEST_WEB,
    },
    scorers=("merge-readiness", "spec-adherence"),
)
BUILDER_FAILS = {
    "turns": [
        {"role": "builder", "round": 1, "attempt": n, "raise": "build broke"} for n in range(1, 6)
    ]
}
ESCALATE = {"turns": [{"role": "approver", "round": 1, "output": "ESCALATE\nneeds a human"}]}


class FakeJudge:
    def __init__(self, sessions):
        self.sessions = sessions
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, deps, job_id, name, scorer, *, round, occurrence, **kw):
        self.calls.append((name, dict(kw.get("extra_sections") or {})))
        async with self.sessions.begin() as s:
            s.add(AgentRun(job_id=job_id, role="judge", round=round, attempt=occurrence,
                           engine="fake", status="ok"))  # fmt: skip
        return ScoreRow(name, "B", Decimal("0.7"), True, "ok", judge_model="m@v")


@pytest.fixture
async def env(sessions, monkeypatch):
    await _clean(sessions)
    judge = FakeJudge(sessions)
    monkeypatch.setattr(svc_mod, "run_judge", judge)
    yield SimpleNamespace(sessions=sessions, judge=judge)
    await _clean(sessions)


async def _clean(sessions):
    from sqlalchemy import text

    async with sessions.begin() as s:
        await s.execute(
            text(
                "truncate app.bench_results, app.bench_runs, app.scores, app.agent_runs, "
                "app.job_events, app.jobs cascade"
            )
        )


async def run(
    env,
    *,
    script: dict | None = None,
    iso_exit: dict[str, int] | None = None,
    iso_raises: Exception | None = None,
    finalize_raises: Exception | None = None,
    monkeypatch=None,
):
    sessions = env.sessions
    suite = BenchSuiteDef(
        configurations=(BenchConfigDef(name="c1", profile="team-a"),), tasks={"001-todo": TASK}
    )
    await seed_demo(
        sessions,
        defaults={"engine_retry_backoff_s": [0, 0]},
        benchmarks={"suite": suite},
    )
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        row = await s.get_one(Job, job.id)
        row.kind = JobKind.BENCHMARK.value
        run_row = BenchRun(
            suite="suite",
            configurations=[],
            definition_sha=row.definition_sha,
            status="running",
            budget_usd=Decimal(10),
            estimate_usd=Decimal(1),
            repetitions=1,
            max_parallel=1,
        )
        s.add(run_row)
        await s.flush()
        s.add(
            BenchResult(
                run_id=run_row.id,
                task="001-todo",
                configuration="c1",
                repetition=1,
                status="running",
                job_id=job.id,
                overrides={},
            )
        )
    deps = make_test_deps(sessions, FakeScript.from_dict(script or {"turns": []}))
    object.__setattr__(
        deps, "scoring", ScoringService(sessions, deps.events, deps.definitions, deps.catalog)
    )
    sb = deps.sandboxes
    orig = sb.isolated

    @asynccontextmanager
    async def isolated(job_id, runner, *, name):
        if iso_raises is not None:
            raise iso_raises
        async with orig(job_id, runner, name=name) as box:
            codes = iso_exit or {}
            box.exec_results = [  # pyright: ignore[reportAttributeAccessIssue]
                ExecResult(codes.get(p, 0), "", "", False, 0.1) for p in sorted(TASK.check_files)
            ]
            yield box

    sb.isolated = isolated  # type: ignore[method-assign]
    row, defn, _ = await pinned_definition(deps, job.id)
    calls: list[str] = []
    if finalize_raises is not None:

        def make(d):
            async def finalize(state):
                calls.append("finalize")
                raise finalize_raises  # before the stage write: the job is still open

            return finalize

        assert monkeypatch is not None
        monkeypatch.setattr(build_mod.finalize, "make", make)
    g = build_job_graph(deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    st = await g.ainvoke(initial_state(row, defn), cfg)
    return SimpleNamespace(state=st, job=job.id, deps=deps, calls=calls)


async def result_row(sessions, job_id):
    async with sessions() as s:
        r = await s.scalar(select(BenchResult).where(BenchResult.job_id == job_id))
        assert r is not None
        detail = json.loads(r.hidden_detail) if r.hidden_detail else None
        return {
            "hidden_passed": r.hidden_passed,
            "hidden_detail": detail,
            "status": r.status,
            "judge_pass": r.judge_pass,
            "judge_pass_uncalibrated": r.judge_pass_uncalibrated,
            "rounds": r.rounds,
            "cost_usd": r.cost_usd,
        }


async def events(sessions, job_id, kind):
    async with sessions() as s:
        return list(
            (
                await s.scalars(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.kind == kind)
                    .order_by(JobEvent.id)
                )
            ).all()
        )


async def stage(sessions, job_id):
    async with sessions() as s:
        return (await s.get_one(Job, job_id, populate_existing=True)).stage


async def test_benchmark_job_never_delivers_or_waits(env):
    r = await run(env, script=ESCALATE)
    assert r.state["outcome"] == "closed" and not r.state.get("__interrupt__")
    assert await stage(env.sessions, r.job) == "closed"
    assert await events(env.sessions, r.job, "human.waiting") == []
    gate = (await events(env.sessions, r.job, "bench.gate"))[-1]
    assert gate.data["reason"] == "approver escalated" and gate.data["scope"] is None


async def test_approved_benchmark_job_is_closed_not_merged(env):
    r = await run(env)
    assert r.state["outcome"] == "closed" and await stage(env.sessions, r.job) == "closed"
    assert await events(env.sessions, r.job, "delivery.merged") == []


async def test_hidden_checks_error_records_null(env):
    r = await run(env, iso_raises=RuntimeError("no space left on device"))
    row = await result_row(env.sessions, r.job)
    assert (row["hidden_passed"], row["hidden_detail"]["error"]) == (
        None,
        "hidden checks could not run: no space left on device",
    )
    assert await stage(env.sessions, r.job) == "closed"


async def test_benchmark_finalize_error_ends_failed(env, monkeypatch):
    r = await run(env, finalize_raises=RuntimeError("db down"), monkeypatch=monkeypatch)
    assert await stage(env.sessions, r.job) == "failed"
    assert r.calls.count("finalize") == 1 and not r.state.get("__interrupt__")
    assert (await events(env.sessions, r.job, "job.failed"))[-1].data == {
        "error": "step failed: finalize"
    }


async def test_hidden_checks_run_each_file_in_isolated_box(env):
    r = await run(env)
    sb = r.deps.sandboxes
    iso = sb.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert sb.isolated_names[-1] == "hidden-checks"
    assert iso.files["/work/repo/tests/test_browser.py"].decode() == HIDDEN_TEST_BROWSER
    assert iso.files["/work/repo/tests/test_web.py"].decode() == HIDDEN_TEST_WEB
    assert iso.exec_calls[0][0] == "bash"  # the sanitizer is stubbed here: first call is a check
    checks = [
        (c, t) for c, t in zip(iso.exec_calls, iso.exec_timeouts, strict=True) if c[0] == "bash"
    ]
    assert checks == [
        (["bash", "-c", "python3 -m unittest -q tests/test_browser.py"], 900),
        (["bash", "-c", "python3 -m unittest -q tests/test_web.py"], 900),
    ]
    job_box = sb.boxes.get(UUID(str(r.state["job_id"])))
    assert job_box is None or "/work/repo/tests/test_web.py" not in job_box.files
    row = await result_row(env.sessions, r.job)
    assert row["hidden_passed"] is True
    assert "bench/hidden-checks.log" in {m.path for m in await r.deps.artifacts.list(r.job)}


async def test_one_failing_check_file_fails_the_trial(env):
    r = await run(env, iso_exit={"tests/test_browser.py": 5})
    row = await result_row(env.sessions, r.job)
    assert (row["hidden_passed"], row["hidden_detail"]["files"]) == (
        False,
        {"tests/test_browser.py": 5, "tests/test_web.py": 0},
    )


async def test_benchmark_judges_run_task_scorers(env):
    r = await run(env)
    assert {c[0] for c in env.judge.calls} == {"merge-readiness", "spec-adherence"}
    assert all("success-criteria" in c[1] for c in env.judge.calls)
    assert "lists todos" in env.judge.calls[0][1]["success-criteria"]
    row = await result_row(env.sessions, r.job)
    assert row["status"] == "done" and row["judge_pass"] is None
    assert row["judge_pass_uncalibrated"] is True and row["rounds"] is not None


async def test_failed_build_is_a_failed_trial(env):
    r = await run(env, script=BUILDER_FAILS)
    row = await result_row(env.sessions, r.job)
    assert (row["hidden_passed"], row["hidden_detail"]["error"], row["status"]) == (
        False,
        "no verified build to check",
        "done",
    )
    assert r.deps.sandboxes.isolated_names == []


async def test_record_trial_marks_run_budget_stop_as_not_run(env):
    from swfactory.bench.runner import record_trial
    from swfactory.events import record_event

    r = await run(env, script=ESCALATE)
    async with env.sessions.begin() as s:
        await record_event(s, r.job, "bench.gate", {"reason": "budget", "scope": "bench_run"})
    async with env.sessions.begin() as s:
        await record_trial(s, r.job)
    assert (await result_row(env.sessions, r.job))["status"] == "budget_stopped"
