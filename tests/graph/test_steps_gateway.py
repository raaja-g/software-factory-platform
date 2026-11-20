"""run_agent_step with selection, reserved run keys and spend (M5.8)."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from tests.support.db_helpers import enq
from tests.support.gateway_env import CATALOG, PRO, make_env

from swfactory.costs.budgets import BudgetExceeded
from swfactory.db.repo import start_agent_run
from swfactory.db.tables import BudgetReservation
from swfactory.definition.models import Defaults, max_turns_for
from swfactory.domain.types import Role, RunStatus, ToolPolicy
from swfactory.graph import steps
from swfactory.graph.build import make_error_handler
from swfactory.graph.steps import (
    DAILY_RECHECK_S,
    DAILY_WAIT_S,
    context_tokens_for,
    run_agent_step,
)
from swfactory.roles.render import DEFAULT_CONTEXT_TOKENS
from swfactory.sandbox.paths import REPO

pytestmark = pytest.mark.db

USAGE = {"input_tokens": 1_000_000, "output_tokens": 200_000}


@pytest.fixture
async def env(sessions, monkeypatch):
    return await make_env(sessions, monkeypatch)


def minted_for(env, role: str):
    return [m for m in env.fake.minted if m["tags"]["role"] == role]


async def stale_attempt(env, run_id: UUID | None = None, amount="5") -> UUID:
    """A killed builder attempt: a running row with an open reservation."""
    async with env.sessions.begin() as s:
        rid, _ = await start_agent_run(
            s, job_id=env.job, role="builder", round=1, engine="opencode", model_alias=PRO
        )
        s.add(
            BudgetReservation(
                run_id=rid,
                job_id=env.job,
                role="builder",
                round=1,
                day=(await env.job_row()).created_at.date(),
                month=(await env.job_row()).created_at.date().replace(day=1),
                amount=Decimal(amount),
            )
        )
    return rid


async def test_run_charged_to_its_own_key(env):
    env.fake.spend_reads = [Decimal(0), Decimal("0.0123")]  # the first read lags
    env.turn(Role.BUILDER, 1, usage=USAGE)
    out = await env.build()
    run = await env.run_row(out.run_id)
    assert (run.cost_usd, run.cost_source, run.model_alias) == (Decimal("0.0123"), "gateway", PRO)
    dec = await env.decision(out.run_id)
    assert dec is not None and run.decision_id == dec.id and dec.role == "builder"
    m = env.fake.minted[0]
    assert m["models"] == [PRO] and m["ttl_s"] == 60 * 60 + 300 and m["tags"]["role"] == "builder"
    assert m["run_id"] == out.run_id and m["tags"]["run"] == str(out.run_id)
    task = env.engines["opencode"].last_task
    assert env.fake.revoked == [m["key"]] and task.gateway_key == m["key"]
    assert task.gateway_base_url == "http://gateway-edge:4000" and task.max_turns == 80
    r = await env.reservation(out.run_id)
    assert r.actual_usd == Decimal("0.0123") and r.released_at is not None
    assert (await env.job_row()).cost_usd == Decimal("0.0123")
    assert len(await env.events("cost.updated")) == 1


async def test_selection_context_values(env):
    await env.deps.artifacts.put(env.job, "spec.md", "s" * 900)
    await env.sql(
        "update app.jobs set feature = :f, base_sha = 'abc' where id = :j",
        f="f" * 120,
        j=env.job,
    )
    for role, verdict in (("reviewer-security", "PASS"), ("reviewer-code", "CHANGES")):
        async with env.sessions.begin() as s:
            rid, _ = await start_agent_run(
                s, job_id=env.job, role=role, round=1, engine="opencode", model_alias=PRO
            )
            from swfactory.db.repo import finish_agent_run
            from swfactory.domain.types import Usage

            await finish_agent_run(
                s,
                rid,
                status=RunStatus.OK,
                verdict=verdict,
                artifact_path=None,
                usage=Usage(),
                error=None,
                engine_version=None,
                used_models=(),
            )
    from swfactory.gitops.fake import FakeGitService

    env.deps.git.diff_paths = ("a", "b", "c")  # type: ignore[attr-defined]
    assert isinstance(env.deps.git, FakeGitService)
    await env.build(round=1)  # first build: decision recorded, builder family known afterwards
    await env.build(round=2)
    ctx = env.selector.last_ctx
    assert (ctx.feature_chars, ctx.spec_chars) == (120, 900)
    assert ctx.prior_verdicts == {"security": "PASS", "code": "CHANGES"}
    assert ctx.builder_family == "gemini" and ctx.diff_files == 3
    assert ctx.budget_left_usd == await env.deps.budgets.remaining_for_job(env.job)


async def test_claude_code_key_includes_classifier_fallbacks(env):
    await env.sql("update app.jobs set profile_name = 'claude-vertex' where id = :j", j=env.job)
    await env.build()
    assert env.fake.minted[0]["models"] == [
        "claude-sonnet@vertex",
        "claude-opus-5@vertex",
        "claude-sonnet-5@vertex",
    ]
    # other engines: the chosen alias only
    from swfactory.roles.library import RoleLibrary  # noqa: F401

    out = await run_agent_step(
        env.deps,
        job_id=env.job,
        role=Role.REVIEWER_SECURITY,
        round=1,
        inputs=[],
        output_name="round-1/review-security.md",
        workdir=REPO,
        tool_policy=ToolPolicy.READ_ONLY,
    )
    assert out.status == RunStatus.OK
    assert minted_for(env, "reviewer-security")[0]["models"] == [PRO]


async def test_reused_run_mints_nothing(env):
    first = await env.build()
    again = await env.build()
    assert again.reused and again.run_id == first.run_id and len(env.fake.minted) == 1


async def test_stale_attempt_key_revoked_and_released(env):
    stale = await stale_attempt(env)
    await env.build()
    assert f"run-{stale}" in env.fake.revoked_aliases
    assert (await env.reservation(stale)).released_at is not None


async def test_stale_attempt_charged_from_spend_logs(env):
    stale = await stale_attempt(env)
    env.fake.run_spends[stale] = Decimal("4.5")
    await env.build()
    run = await env.run_row(stale)
    assert (run.cost_usd, run.cost_source) == (Decimal("4.5"), "gateway")
    assert (await env.reservation(stale)).actual_usd == Decimal("4.5")
    assert (await env.job_row()).cost_usd >= Decimal("4.5")


async def test_unreadable_spend_keeps_reservation_open(env):
    stale = await stale_attempt(env)
    env.fake.run_spends[stale] = None
    await env.build()
    assert (await env.run_row(stale)).cost_usd is None
    assert (await env.reservation(stale)).released_at is None


async def test_cancelled_step_charged_from_spend_logs(env):
    env.turn(Role.BUILDER, 1, latency_s=3600)  # blocks until the task is cancelled
    t = asyncio.create_task(env.build())
    for _ in range(200):
        await asyncio.sleep(0.05)
        rows = await env.runs()
        if rows and env.engines["opencode"].tasks:
            break
    run_id = rows[0].id
    env.fake.run_spends[run_id] = Decimal("0.42")
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    run = await env.run_row(run_id)
    assert run.cost_usd == Decimal("0.42") and run.status == "running"  # the resume cleans it up
    assert (await env.reservation(run_id)).actual_usd == Decimal("0.42")
    assert (await env.job_row()).cost_usd == Decimal("0.42")
    assert env.fake.revoked  # the key was revoked first


async def test_context_tokens_from_catalog_window(env, monkeypatch):
    seen = []
    real = steps.render_prompt

    def spy(spec, ctx):
        seen.append(ctx)
        return real(spec, ctx)

    monkeypatch.setattr(steps, "render_prompt", spy)
    await env.build()
    assert seen[-1].context_tokens == 1_048_576


def test_context_tokens_fallback():
    pro = next(i for i in CATALOG if i.alias == PRO)
    assert context_tokens_for(replace(pro, context_window=None)) == DEFAULT_CONTEXT_TOKENS == 32_000
    assert context_tokens_for(None) == 32_000


def test_max_turns_for_roles():
    d = Defaults()
    assert (max_turns_for(d, "builder"), max_turns_for(d, "reviewer-code")) == (80, 40)
    assert max_turns_for(Defaults(max_turns={"default": 7}), "builder") == 7
    with pytest.raises(ValueError):
        Defaults(max_turns={"builder": 3})


async def test_mint_failure_releases_reservation(env):
    env.fake.mint_error = httpx.ConnectError("down")
    with pytest.raises(httpx.ConnectError):
        await env.build()
    run = (await env.runs())[-1]
    assert (await env.reservation(run.id)).released_at is not None and env.fake.revoked == []
    env.fake.mint_error = None
    await env.deps.budgets.check_before_step(env.job, Role.BUILDER)


async def test_failed_revoke_still_releases(env, capsys):
    env.fake.revoke_error = httpx.ConnectError("down")
    env.turn(Role.BUILDER, 1, usage=USAGE)
    out = await env.build()
    r = await env.reservation(out.run_id)
    assert r.released_at is not None and r.actual_usd == (await env.run_row(out.run_id)).cost_usd
    assert "run key revoke failed" in capsys.readouterr().out


async def test_budget_gate_before_minting(env):
    await env.sql("update app.jobs set budget_usd = 10 where id = :j", j=env.job)
    async with env.sessions.begin() as s:
        rid, _ = await start_agent_run(
            s, job_id=env.job, role="spec-writer", round=1, engine="opencode", model_alias=PRO
        )
        from swfactory.db.repo import record_run_cost
        from swfactory.domain.types import CostSource

        await record_run_cost(s, rid, Decimal("9.95"), CostSource.GATEWAY)
    with pytest.raises(BudgetExceeded) as ei:
        await env.build()
    assert ei.value.gate_reason == "budget" and env.fake.minted == []


async def hold_daily(env, amount: str):
    async with env.sessions.begin() as s:
        other = (await enq(env.sessions, app="demo")).id
        j = await env.rows(__import__("swfactory.db.tables", fromlist=["Job"]).Job)
        row = next(r for r in j if r.id == other)
        s.add(
            BudgetReservation(
                run_id=UUID(int=1),
                job_id=other,
                app_id=row.app_id,
                role="builder",
                round=1,
                day=row.created_at.date(),
                month=row.created_at.date().replace(day=1),
                amount=Decimal(amount),
            )
        )


async def spend_today(env, amount: str):
    async with env.sessions.begin() as s:
        other = (await enq(env.sessions, app="demo")).id
        rid, _ = await start_agent_run(
            s, job_id=other, role="builder", round=1, engine="opencode", model_alias=PRO
        )
        from swfactory.db.repo import record_run_cost
        from swfactory.domain.types import CostSource

        await record_run_cost(s, rid, Decimal(amount), CostSource.GATEWAY)


async def test_daily_stop_held_by_reservations_waits(sessions, monkeypatch):
    env = await make_env(sessions, monkeypatch)
    # daily cap 150: 0.20 left, and another run holds 3.80 in an open reservation
    await spend_today(env, "146")
    await hold_daily(env, "3.8")
    released = []
    real = env.fake.call_log

    async def sleeper(seconds):
        env.slept.append(seconds)
        if not released:
            released.append(1)
            await env.sql("update app.budget_reservations set released_at = now()")

    monkeypatch.setattr(steps, "_sleep", sleeper)
    out = await env.build()
    assert env.slept[0] == DAILY_RECHECK_S == 30 and out.status == RunStatus.OK
    assert real is env.fake.call_log


async def test_daily_stop_held_too_long_gates(sessions, monkeypatch):
    env = await make_env(sessions, monkeypatch)
    await spend_today(env, "146")
    await hold_daily(env, "3.8")
    with pytest.raises(BudgetExceeded) as ei:
        await env.build()
    assert ei.value.gate_reason == "budget"
    assert env.slept.count(DAILY_RECHECK_S) == DAILY_WAIT_S // DAILY_RECHECK_S == 20


async def test_daily_stop_with_nothing_held_gates_at_once(sessions, monkeypatch):
    env = await make_env(sessions, monkeypatch)
    await spend_today(env, "150")
    with pytest.raises(BudgetExceeded):
        await env.build()
    assert DAILY_RECHECK_S not in env.slept


async def test_parallel_review_keys_within_job_remainder(env):
    await env.sql("update app.jobs set budget_usd = 1 where id = :j", j=env.job)
    for role in (Role.REVIEWER_SECURITY, Role.REVIEWER_CODE, Role.REVIEWER_UX, Role.REVIEWER_UI):
        env.turn(role, 1, output="VERDICT: PASS\nok", latency_s=1)  # all four run at once

    async def review(kind: str):
        return await run_agent_step(
            env.deps,
            job_id=env.job,
            role=Role(f"reviewer-{kind}"),
            round=1,
            inputs=[],
            output_name=f"round-1/review-{kind}.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_ONLY,
            fan_out=4,
        )

    outs = await asyncio.gather(*(review(k) for k in ("security", "code", "ux", "ui")))
    assert all(o.status == RunStatus.OK for o in outs) and len(env.fake.minted) == 4
    assert sum(m["max_budget_usd"] for m in env.fake.minted) <= Decimal("1")


async def test_gateway_budget_refusal_mid_step(env):
    env.turn(Role.BUILDER, 1, status="budget")
    with pytest.raises(BudgetExceeded) as ei:
        await env.build()
    assert ei.value.gate_reason == "budget" and env.fake.revoked
    assert (await env.runs())[0].status == "budget"


async def test_each_attempt_has_its_own_run_reservation_and_key(env):
    env.turn(Role.BUILDER, 1, status="failed", error="connect ECONNREFUSED", error_class="gateway")
    await env.build()
    runs = await env.runs()
    assert [r.attempt for r in runs] == [1, 2] and len({r.id for r in runs}) == 2
    res = await env.rows(BudgetReservation, BudgetReservation.job_id == env.job)
    assert {r.run_id for r in res} == {r.id for r in runs} and all(r.released_at for r in res)
    assert sorted(env.fake.revoked) == sorted(m["key"] for m in env.fake.minted)
    assert len(env.fake.minted) == 2


async def test_spend_read_after_batch_flush(env):
    env.fake.spend_reads = [Decimal("0.5")]
    env.turn(Role.BUILDER, 1, usage=USAGE)
    await env.build()
    log = env.fake.call_log
    i = next(n for n, c in enumerate(log) if c[0] == "key_spend")
    assert log[i - 1] == ("sleep", 6)
    assert (await env.runs())[0].cost_source == "gateway"


async def test_zero_spend_on_priced_alias_records_estimate(env):
    env.fake.spend_reads = [Decimal(0)]
    env.turn(Role.BUILDER, 1, usage=USAGE)
    await env.build()
    run = (await env.runs())[0]
    assert (run.cost_usd, run.cost_source) == (Decimal("0.800000"), "estimate")


async def test_zero_spend_on_local_alias_stays_gateway(env):
    from swfactory.domain.types import ModelRef, SelectionMode
    from swfactory.models.selection import ModelChoice

    local = next(i for i in CATALOG if i.alias == "local@ollama")
    pre = ModelChoice(
        "opencode", ModelRef(local.alias, local.provider, local.upstream_id), SelectionMode.FIXED,
        None, (await env.selector.choose(*await _pc(env))).decision_id, "pre",
    )  # fmt: skip
    env.fake.spend_reads = [Decimal(0)]
    env.turn(Role.BUILDER, 1, usage=USAGE)
    await env.build(choice=pre)
    run = (await env.runs())[0]
    assert (run.cost_usd, run.cost_source, run.model_alias) == (Decimal(0), "gateway", local.alias)
    assert env.selector.exclude_calls and len(env.selector.contexts) == 1  # step 4 was skipped


async def _pc(env):
    from swfactory.graph.steps import build_selection_context, pinned_definition
    from swfactory.models.profiles import resolve_profile

    job, defn, app = await pinned_definition(env.deps, env.job)
    profile, _ = resolve_profile(defn, app, override=None, triage_profile=None)
    return profile, await build_selection_context(env.deps, job, Role.BUILDER, 1, None)


async def test_error_handler_uses_gate_reason():
    from swfactory.models.selection import NoModelAvailable

    h = make_error_handler("build")
    exc = BudgetExceeded("job", Decimal(0), Decimal(1))
    assert h({}, exc).update == {  # type: ignore[arg-type]
        "gate_reason": "budget",
        "retry_node": "build",
        "gate_scope": "job",
    }
    assert h({}, NoModelAvailable("x")).update["gate_reason"] == "no model available"  # type: ignore[arg-type]
    assert h({}, RuntimeError("x")).update["gate_reason"] == "step failed: build"  # type: ignore[arg-type]
