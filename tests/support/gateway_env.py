"""A step environment with the real selector, the real budgets, a catalog and a FakeGateway."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text
from tests.fakes.deps import make_test_deps, seed_demo
from tests.fakes.gateway import FakeGateway
from tests.support.db_helpers import enq

from swfactory.db.repo import upsert_models
from swfactory.db.tables import AgentRun, BudgetReservation, Job, JobEvent, ModelDecision
from swfactory.domain.types import EngineSupport, Role, ToolPolicy
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import register_engine
from swfactory.events import EventBus
from swfactory.graph import steps
from swfactory.graph.steps import StepOutcome, run_agent_step
from swfactory.models.catalog import Catalog, ModelInfo
from swfactory.models.selection import ModelSelector
from swfactory.sandbox.paths import REPO

PRO, FLASH, SONNET = "gemini-pro@vertex", "gemini-flash@vertex", "claude-sonnet@vertex"


def info(alias: str, upstream: str, *, pin="0.30", pout="2.50", ctx=1_048_576) -> ModelInfo:
    return ModelInfo(
        alias=alias,
        provider="vertex",
        upstream_id=upstream,
        strength="strong",
        price_in_per_mtok=Decimal(pin),
        price_out_per_mtok=Decimal(pout),
        context_window=ctx,
        supports_tools=True,
        messages_tools_ok=True,
        available=True,
        error_class=None,
        latency_ms=10,
        last_error=None,
    )


CATALOG = [
    info(PRO, "gemini-4-argon"),
    info(FLASH, "gemini-3.8-flash", pin="0.10", pout="0.40"),
    info(SONNET, "claude-sonnet-5-5", pin="3", pout="15", ctx=200_000),
    info("claude-opus-5@vertex", "claude-opus-5"),
    info("claude-sonnet-5@vertex", "claude-sonnet-5"),
    replace(info("local@ollama", "qwen3", pin="0", pout="0"), provider="ollama", strength="local"),
]


class RecordingSelector(ModelSelector):
    """The real selector, remembering what it was asked."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.contexts: list[Any] = []
        self.exclude_calls: list[set[str]] = []

    @property
    def last_ctx(self) -> Any:
        return self.contexts[-1]

    async def choose(self, profile, ctx, *, exclude=()):  # type: ignore[override]
        self.contexts.append(ctx)
        self.exclude_calls.append(set(exclude))
        return await super().choose(profile, ctx, exclude=exclude)


def _any_engine(engine: str, info: ModelInfo) -> EngineSupport:
    return EngineSupport.OK


async def make_env(
    sessions: Any, monkeypatch: Any, *, defaults: dict | None = None, app: dict | None = None
) -> SimpleNamespace:
    """Seed a definition, a job and a catalog; engines opencode, claude_code, direct and fake."""
    await seed_demo(
        sessions,
        defaults={
            "engine_retry_backoff_s": [10, 60],
            "claude_classifier_fallbacks": ["claude-opus-5@vertex", "claude-sonnet-5@vertex"],
            **(defaults or {}),
        },
        app=app or {},
    )
    job = await enq(sessions, app="demo")
    script = FakeScript()
    fake = FakeGateway()
    e = SimpleNamespace(sessions=sessions, job=job.id, script=script, fake=fake)
    deps = make_test_deps(sessions, script, gateway=fake)
    catalog = Catalog(sessions, None, deps.definitions)
    selector = RecordingSelector(sessions, catalog, EventBus(sessions, ""), support=_any_engine)
    deps = replace(deps, selector=selector, catalog=catalog)
    engines = {n: FakeEngine(script, name=n) for n in ("opencode", "claude_code", "direct")}
    for eng in engines.values():
        register_engine(eng)
    e.deps, e.selector, e.catalog, e.engines = deps, selector, catalog, engines
    async with sessions.begin() as s:
        await upsert_models(s, CATALOG, probed_at=datetime.now(UTC))
    e.slept = []
    e.log = []

    async def fake_sleep(seconds: float) -> None:
        e.slept.append(seconds)
        fake.call_log.append(("sleep", seconds))

    monkeypatch.setattr(steps, "_sleep", fake_sleep)

    def turn(role: Role, attempt: int, **kw: Any) -> None:
        t = {"role": role.value, "round": 1, "attempt": attempt, **kw}
        e.script.entries = [*e.script.entries, *FakeScript.from_dict({"turns": [t]}).entries]

    async def build(*, round: int = 1, **kw: Any) -> StepOutcome:
        return await run_agent_step(
            deps,
            job_id=job.id,
            role=Role.BUILDER,
            round=round,
            inputs=["spec.md"],
            output_name="build.md",
            workdir=REPO,
            tool_policy=ToolPolicy.WRITE,
            **kw,
        )

    async def rows(model, *where):
        async with sessions() as s:
            return list((await s.scalars(select(model).where(*where))).all())

    async def runs(role: str = "builder") -> list[AgentRun]:
        async with sessions() as s:
            q = select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.role == role)
            return list((await s.scalars(q.order_by(AgentRun.started_at))).all())

    async def run_row(run_id) -> AgentRun:
        async with sessions() as s:
            r = await s.get(AgentRun, run_id)
            assert r is not None
            return r

    async def reservation(run_id) -> BudgetReservation:
        async with sessions() as s:
            r = await s.get(BudgetReservation, run_id)
            assert r is not None
            return r

    async def job_row() -> Job:
        async with sessions() as s:
            r = await s.get(Job, job.id)
            assert r is not None
            return r

    async def events(kind: str) -> list[dict]:
        async with sessions() as s:
            q = select(JobEvent.data).where(JobEvent.job_id == job.id, JobEvent.kind == kind)
            return list((await s.scalars(q.order_by(JobEvent.id))).all())

    async def decision(run_id) -> ModelDecision | None:
        async with sessions() as s:
            return (
                await s.scalars(select(ModelDecision).where(ModelDecision.run_id == run_id))
            ).first()

    async def sql(q: str, **params: Any) -> None:
        async with sessions.begin() as s:
            await s.execute(text(q), params)

    e.turn, e.build, e.runs, e.run_row, e.reservation = turn, build, runs, run_row, reservation
    e.job_row, e.events, e.decision, e.sql, e.rows = job_row, events, decision, sql, rows
    return e
