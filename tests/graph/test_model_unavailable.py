"""Mid-step model failures: one fallback retry, never the same model again (M5.8, L15)."""

import json
from pathlib import Path

import pytest
from tests.support.gateway_env import FLASH, PRO, SONNET, make_env

from swfactory.domain.types import Role
from swfactory.models.selection import NoModelAvailable

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gateway"


def fixture(name: str) -> str:
    return json.dumps(json.loads((FIXTURES / name).read_text()))


def waits(env) -> list:
    """Backoff sleeps only: the spend-flush waits are not part of what these tests check."""
    return [s for s in env.slept if s not in (6, 2)]


@pytest.fixture
async def env(sessions, monkeypatch):
    # builder: pro, fallback flash (the starter profile); sonnet is the security reviewer's
    return await make_env(sessions, monkeypatch)


async def test_model_turns_unavailable_mid_job(env):
    await env.build(round=1)
    await env.catalog.mark_unavailable(PRO, "quota", "probe: 429")
    await env.build(round=2)
    assert [r.model_alias for r in await env.runs()] == [PRO, FLASH]
    fb = (await env.events("model.fallback"))[0]
    assert fb == {
        "role": "builder",
        "round": 2,
        "requested": PRO,
        "used": FLASH,
        "source": "selection",
    }


@pytest.mark.parametrize(
    "error,meta_cls,cls",
    [
        (fixture("errors/quota.json"), "rate_limit", "quota"),
        (fixture("errors/not_enabled.json"), "not_enabled", "not_enabled"),
        (fixture("errors/upstream_down.json"), "http_5xx", "other"),
    ],
)
async def test_engine_model_error_falls_back(env, error, meta_cls, cls):
    # engine_retries=2: rate_limit and gateway are transient classes, yet pro is never retried
    env.turn(Role.BUILDER, 1, status="failed", error=error, error_class=meta_cls)
    await env.build()
    runs = await env.runs()
    assert [(r.model_alias, r.status) for r in runs] == [(PRO, "failed"), (FLASH, "ok")]
    assert runs[0].error.startswith(f"model unavailable: {cls}")
    assert runs[1].attempt == runs[0].attempt + 1
    assert (await env.catalog.get(PRO)).error_class == cls
    dec = await env.decision(runs[1].id)
    assert dec is not None and dec.reason.startswith("fallback:")
    assert (await env.events("model.fallback"))[-1]["requested"] == PRO
    assert len(env.fake.revoked) == 2 and env.selector.exclude_calls[-1] == {PRO}
    assert waits(env) == ([10] if cls == "quota" else [])  # 429 backs off backoff[0] first


async def test_gateway_unreachable_retries_same_model(env):
    err = "connect ECONNREFUSED 172.18.0.4:4000"
    env.turn(Role.BUILDER, 1, status="failed", error=err, error_class="gateway")
    await env.build()
    runs = await env.runs()
    assert [(r.model_alias, r.status) for r in runs] == [(PRO, "failed"), (PRO, "ok")]
    assert (await env.catalog.get(PRO)).available is True
    assert await env.events("model.fallback") == [] and waits(env) == [10]


async def test_quota_backoff_capped_at_60s(sessions, monkeypatch):
    env = await make_env(sessions, monkeypatch, defaults={"engine_retry_backoff_s": [90, 120]})
    env.turn(Role.BUILDER, 1, status="failed", error=fixture("errors/quota.json"))
    await env.build()
    assert waits(env) == [60]  # min(backoff[0], MAX_QUOTA_BACKOFF_S)


async def test_model_error_retries_once_only(env):
    quota = fixture("errors/quota.json")
    env.turn(Role.BUILDER, 1, status="failed", error=quota)
    env.turn(Role.BUILDER, 2, status="failed", error=quota)
    with pytest.raises(NoModelAvailable) as ei:
        await env.build()
    assert ei.value.gate_reason == "no model available"
    assert [r.model_alias for r in await env.runs()] == [PRO, FLASH]  # nothing further tried


async def test_no_fallback_raises_no_model_available(sessions, monkeypatch):
    env = await make_env(sessions, monkeypatch)
    # only pro is available for the builder: flash is out of the catalog's reach
    await env.catalog.mark_unavailable(FLASH, "not_enabled", "off")
    env.turn(Role.BUILDER, 1, status="failed", error=fixture("errors/quota.json"))
    minted_before = len(env.fake.minted)
    with pytest.raises(NoModelAvailable):
        await env.build()
    assert [r.model_alias for r in await env.runs()] == [PRO]
    assert len(env.fake.minted) == minted_before + 1  # no key after the gate


async def test_preselected_choice_is_not_retried(env):
    from tests.support.gateway_env import CATALOG

    from swfactory.domain.types import ModelRef, SelectionMode
    from swfactory.models.selection import ModelChoice

    sel = await env.selector.choose(*await _pc(env))
    info = next(i for i in CATALOG if i.alias == SONNET)
    choice = ModelChoice(
        "opencode",
        ModelRef(info.alias, info.provider, info.upstream_id),
        SelectionMode.FIXED,
        None,
        sel.decision_id,
        "pre",
    )
    env.turn(Role.BUILDER, 1, status="failed", error=fixture("errors/quota.json"))
    with pytest.raises(NoModelAvailable):
        await env.build(choice=choice)
    assert [r.model_alias for r in await env.runs()] == [SONNET]


async def _pc(env):
    from swfactory.graph.steps import build_selection_context, pinned_definition
    from swfactory.models.profiles import resolve_profile

    job, defn, app = await pinned_definition(env.deps, env.job)
    profile, _ = resolve_profile(defn, app, override=None, triage_profile=None)
    return profile, await build_selection_context(env.deps, job, Role.BUILDER, 1, None)
