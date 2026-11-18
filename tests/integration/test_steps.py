import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.fakes.deps import DEFAULT_TIMEOUTS, make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.tables import AgentRun, JobEvent
from swfactory.definition.models import Defaults
from swfactory.domain.types import AgentResult, Role, RunStatus, ToolPolicy, Usage, Verdict
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import get_engine
from swfactory.graph import steps
from swfactory.graph.steps import is_transient, run_agent_step, step_budget_s
from swfactory.logging import configure_logging, log_context
from swfactory.sandbox.paths import REPO

pytestmark = pytest.mark.db

AGENT_RESULT_DEFAULTS = {
    "output": None,
    "session_id": None,
    "usage": Usage(),
    "used_models": (),
    "reported_cost_usd": None,
    "transcript": None,
    "error": None,
    "engine_version": "fake-1",
}


@pytest.fixture
async def env(sessions):
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    e = SimpleNamespace(sessions=sessions, j=job.id)

    def deps_for(turns):
        d = make_test_deps(sessions, FakeScript.from_dict({"turns": turns}))
        e.deps = d
        return d

    e.deps_for = deps_for

    async def review_step(d):
        return await run_agent_step(
            d,
            job_id=e.j,
            role=Role.REVIEWER_SECURITY,
            round=1,
            inputs=["spec.md"],
            output_name="round-1/review-security.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_ONLY,
        )

    async def build_step(d, occurrence=1):
        return await run_agent_step(
            d,
            job_id=e.j,
            role=Role.BUILDER,
            round=1,
            inputs=["spec.md"],
            output_name="build.md",
            workdir=REPO,
            tool_policy=ToolPolicy.WRITE,
            occurrence=occurrence,
        )

    async def rows(role):
        async with sessions() as s:
            return list(
                (
                    await s.scalars(
                        select(AgentRun)
                        .where(AgentRun.job_id == e.j, AgentRun.role == role.value)
                        .order_by(AgentRun.attempt)
                    )
                ).all()
            )

    async def kinds():
        async with sessions() as s:
            return list(
                (
                    await s.scalars(
                        select(JobEvent.kind)
                        .where(JobEvent.job_id == e.j, JobEvent.kind.like("run.%"))
                        .order_by(JobEvent.id)
                    )
                ).all()
            )

    async def insert_running(role, attempt, meta=None):
        async with sessions.begin() as s:
            s.add(
                AgentRun(
                    job_id=e.j,
                    role=role.value,
                    round=1,
                    attempt=attempt,
                    engine="fake",
                    status="running",
                    meta=meta or {},
                )
            )

    e.review_step, e.build_step, e.rows, e.kinds, e.insert_running = (
        review_step,
        build_step,
        rows,
        kinds,
        insert_running,
    )
    return e


def FAKE_TASKS():
    return get_engine("fake").tasks  # pyright: ignore[reportAttributeAccessIssue]


def clock_per_attempt(monkeypatch, secs):
    clock = [0.0]
    monkeypatch.setattr(steps, "_monotonic", lambda: clock[0])
    orig = FakeEngine.run

    async def run(self, task, box):
        try:
            return await orig(self, task, box)
        finally:
            clock[0] += secs

    monkeypatch.setattr(FakeEngine, "run", run)


def transient(role, n, status="failed", **kw):
    return {"role": role, "round": 1, "attempt": n, "status": status, **kw}


async def test_records_ok_run_artifact_and_events(env):
    d = env.deps_for([])
    out = await env.review_step(d)
    assert (out.status, out.verdict, out.reused) == (RunStatus.OK, Verdict.PASS, False)
    assert [
        (r.attempt, r.status, r.verdict, r.artifact_path)
        for r in await env.rows(Role.REVIEWER_SECURITY)
    ] == [(1, "ok", "PASS", "round-1/review-security.md")]
    assert await env.kinds() == ["run.started", "run.finished"]
    text = await d.artifacts.get_text(env.j, "round-1/review-security.md")
    assert text.startswith("VERDICT: PASS")


async def test_second_call_reuses(env):
    d = env.deps_for([])
    first = await env.review_step(d)
    again = await env.review_step(d)
    assert (
        again.reused,
        again.run_id,
        len(await env.rows(Role.REVIEWER_SECURITY)),
        (await env.kinds()).count("run.finished"),
    ) == (True, first.run_id, 1, 1)


async def test_interrupted_run_becomes_new_attempt(env):
    await env.insert_running(Role.REVIEWER_SECURITY, attempt=1)
    await env.review_step(env.deps_for([]))
    assert [(r.attempt, r.status, r.error) for r in await env.rows(Role.REVIEWER_SECURITY)] == [
        (1, "failed", "interrupted"),
        (2, "ok", None),
    ]


async def test_stale_write_attempt_restarts_box_and_resets_branch(env):
    await env.insert_running(Role.BUILDER, attempt=1, meta={"start_sha": "a" * 40})
    await env.build_step(env.deps_for([]))
    box = env.deps.sandboxes.boxes[env.j]
    assert box.restarts == 1
    assert box.exec_calls[:3] == [
        ["git", "reset", "--hard", "a" * 40],
        ["git", "clean", "-fdx"],
        ["git", "rev-parse", "HEAD"],
    ]
    assert (await env.rows(Role.BUILDER))[1].meta["start_sha"] == "0" * 40


async def test_stale_read_only_attempt_does_not_restart(env):
    await env.insert_running(Role.REVIEWER_SECURITY, attempt=1)
    await env.review_step(env.deps_for([]))
    box = env.deps.sandboxes.boxes[env.j]
    assert (box.restarts, box.exec_calls) == (0, [["git", "rev-parse", "HEAD"]])


async def test_occurrence_2_starts_fix_attempt_then_reuses(env):
    d = env.deps_for([])
    await env.build_step(d)
    assert [(await env.build_step(d, occurrence=2)).reused for _ in range(2)] == [False, True]
    assert [r.attempt for r in await env.rows(Role.BUILDER)] == [1, 2]


async def test_review_reuse_needs_the_same_head(env):
    d = env.deps_for([])
    await env.review_step(d)
    d.sandboxes.boxes[env.j].head = "1" * 40
    again = await env.review_step(d)
    assert again.reused is False
    assert [(r.attempt, r.head_sha) for r in await env.rows(Role.REVIEWER_SECURITY)] == [
        (1, "0" * 40),
        (2, "1" * 40),
    ]
    assert (await env.review_step(d)).reused is True


async def test_builder_reuse_ignores_the_head(env):
    d = env.deps_for([])
    await env.build_step(d)
    d.sandboxes.boxes[env.j].head = "1" * 40
    assert (await env.build_step(d)).reused is True


async def test_engine_error_is_recorded_not_raised(env):
    out = await env.review_step(
        env.deps_for([{"role": "reviewer-security", "round": 1, "raise": "boom"}])
    )
    assert out.status == RunStatus.FAILED
    assert [(r.status, r.error) for r in await env.rows(Role.REVIEWER_SECURITY)] == [
        ("failed", "boom")
    ]
    assert (await env.kinds()).count("run.finished") == 1


async def test_transient_failures_are_retried(env):
    out = await env.build_step(
        env.deps_for([transient("builder", n, error_class="gateway") for n in (1, 2)])
    )
    assert out.status == RunStatus.OK
    assert [(r.attempt, r.status) for r in await env.rows(Role.BUILDER)] == [
        (1, "failed"),
        (2, "failed"),
        (3, "ok"),
    ]


async def test_retries_exhausted_returns_failed(env):
    out = await env.build_step(
        env.deps_for([transient("builder", n, "timeout") for n in (1, 2, 3)])
    )
    assert out.status == RunStatus.TIMEOUT
    assert [r.attempt for r in await env.rows(Role.BUILDER)] == [1, 2, 3]


async def test_attempts_share_one_step_deadline(env, monkeypatch):
    clock_per_attempt(monkeypatch, 4000)
    await env.build_step(env.deps_for([transient("builder", n, "timeout") for n in (1, 2, 3)]))
    assert [t.timeout_s for t in FAKE_TASKS()[-3:]] == [3600, 3600, 2800]


async def test_no_attempt_starts_with_under_a_minute_left(env, monkeypatch):
    clock_per_attempt(monkeypatch, 5390)
    out = await env.build_step(
        env.deps_for([transient("builder", n, "timeout") for n in (1, 2, 3)])
    )
    assert out.status == RunStatus.TIMEOUT
    assert [r.attempt for r in await env.rows(Role.BUILDER)] == [1, 2]


def test_step_budget():
    assert step_budget_s(Defaults(), "build") == 3 * 3600 + 10 + 60
    assert step_budget_s(Defaults(engine_retries=0), "build") == 3600


async def test_unavailable_model_is_not_retried_on_same_model(env, monkeypatch):
    r = AgentResult(
        status=RunStatus.FAILED, meta={"error_class": "gateway"}, **AGENT_RESULT_DEFAULTS
    )
    assert (is_transient(r), is_transient(r, is_model_unavailable=lambda _: True)) == (True, False)
    monkeypatch.setattr(steps, "_is_model_unavailable", lambda deps, result: True)
    out = await env.build_step(env.deps_for([transient("builder", 1, error_class="gateway")]))
    assert out.status == RunStatus.FAILED
    assert [(r.attempt, r.status) for r in await env.rows(Role.BUILDER)] == [(1, "failed")]


async def test_budget_is_not_retried(env):
    out = await env.build_step(env.deps_for([{"role": "builder", "round": 1, "status": "budget"}]))
    assert out.status == RunStatus.BUDGET
    assert len(await env.rows(Role.BUILDER)) == 1


async def test_missing_verdict_line_is_error(env):
    out = await env.review_step(
        env.deps_for([{"role": "reviewer-security", "round": 1, "output": "looks good"}])
    )
    assert (out.verdict, out.status) == (Verdict.ERROR, RunStatus.FAILED)
    rows = await env.rows(Role.REVIEWER_SECURITY)
    assert len(rows) == 2 and all(r.error.startswith("contract:") for r in rows)


async def test_task_timeout_comes_from_pinned_definition(sessions):
    await seed_demo(
        sessions,
        defaults={"step_timeouts_min": {**DEFAULT_TIMEOUTS, "build": 7}},
    )
    j = (await enq(sessions, app="demo")).id
    await seed_demo(sessions, defaults={"step_timeouts_min": {**DEFAULT_TIMEOUTS, "build": 9}})
    d = make_test_deps(sessions, FakeScript())
    await run_agent_step(
        d,
        job_id=j,
        role=Role.BUILDER,
        round=1,
        inputs=[],
        output_name="build.md",
        workdir=REPO,
        tool_policy=ToolPolicy.WRITE,
    )
    assert FAKE_TASKS()[-1].timeout_s == 420


async def test_log_lines_carry_job_run_and_role(env, capsys):
    configure_logging()
    with log_context(job_id=str(env.j)):
        out = await env.review_step(env.deps_for([]))
    line = next(
        json.loads(ln) for ln in capsys.readouterr().err.splitlines() if '"fake.turn"' in ln
    )
    assert (line["job_id"], line["run_id"], line["role"]) == (
        str(env.j),
        str(out.run_id),
        "reviewer-security",
    )
