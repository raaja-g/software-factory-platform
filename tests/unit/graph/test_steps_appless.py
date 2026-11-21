"""run_agent_step for an app-less improver job: engine override, default profile, job budget."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.fakes.deps import make_test_deps, seed_demo

from swfactory.costs.budgets import BudgetService
from swfactory.db.tables import Job
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.domain.types import JobKind, Role, ToolPolicy
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import register_engine
from swfactory.graph.steps import TIMEOUT_KEY, run_agent_step, step_timeout_s


async def improver_job(sessions, sha: str, *, budget: Decimal = Decimal("10")) -> Job:
    async with sessions.begin() as s:
        row = Job(
            app_id=None,
            seq=1,
            slug="improve",
            title="improve",
            feature="improve",
            kind=JobKind.IMPROVER.value,
            requested_by="improver",
            definition_sha=sha,
            budget_usd=budget,
        )
        s.add(row)
        await s.flush()
        await s.refresh(row)
        return row


class SpySelector:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.profiles: list = []

    async def choose(self, profile, ctx, *, exclude=()):
        self.profiles.append(profile)
        return await self.inner.choose(profile, ctx, exclude=exclude)


@pytest.fixture
async def world(sessions):
    sha = await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    script = FakeScript()
    deps = make_test_deps(sessions, script)
    direct = FakeEngine(script, name="direct")
    register_engine(direct)
    spy = SpySelector(deps.selector)
    from dataclasses import replace

    deps = replace(deps, selector=spy)
    job = await improver_job(sessions, sha)
    yield SimpleNamespace(deps=deps, job=job, direct=direct, spy=spy, sha=sha, sessions=sessions)
    async with sessions.begin() as s:
        await s.execute(__import__("sqlalchemy").text("truncate app.jobs cascade"))


@pytest.mark.db
async def test_engine_override_on_appless_job(world):
    # the improver role file says write; M3.6's effective_tool_policy narrows it to NONE
    await run_agent_step(
        world.deps,
        job_id=world.job.id,
        role=Role.IMPROVER,
        round=1,
        inputs=[],
        output_name="analysis.md",
        workdir=None,
        tool_policy=ToolPolicy.NONE,
        engine="direct",
    )
    task = world.direct.tasks[-1]
    assert (task.engine, task.tool_policy) == ("direct", ToolPolicy.NONE)


@pytest.mark.db
async def test_appless_job_uses_default_profile_and_job_budget(world, monkeypatch):
    seen: list[list[str]] = []
    real = BudgetService._scopes

    async def spy_scopes(self, s, defn, job, slug, *, only_job=False):
        out = await real(self, s, defn, job, slug, only_job=only_job)
        if not only_job:
            seen.append([i[0] for i in out.items])
        return out

    monkeypatch.setattr(BudgetService, "_scopes", spy_scopes)
    await run_agent_step(
        world.deps,
        job_id=world.job.id,
        role=Role.IMPROVER,
        round=1,
        inputs=[],
        output_name="analysis.md",
        workdir=None,
        tool_policy=ToolPolicy.NONE,
        engine="direct",
    )
    defn = await world.deps.definitions.get(world.sha)
    assert world.spy.profiles[-1].name == defn.factory.defaults.profile
    assert seen and all(scopes == ["job", "daily"] for scopes in seen)


@pytest.mark.db
async def test_job_budget_falls_back_to_the_factory_job_budget(world):
    defn = await world.deps.definitions.get(world.sha)
    transient = Job(
        id=world.job.id,
        kind=JobKind.IMPROVER.value,
        budget_usd=None,  # type: ignore[arg-type]
    )
    svc = BudgetService(world.sessions, world.deps.definitions)
    async with world.sessions() as s:
        scopes = await svc._scopes(s, defn, transient, None)
    names = [i[0] for i in scopes.items]
    assert names == ["job", "daily"]
    assert scopes.items[0][1] == defn.factory.budgets.job_usd


def test_improver_has_a_step_timeout():
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        defn = load_definition(d).definition
    assert defn is not None
    assert step_timeout_s(defn, TIMEOUT_KEY[Role.IMPROVER]) == 3600
