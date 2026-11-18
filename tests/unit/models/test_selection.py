from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from tests.support.db_helpers import activate, enq, job_events

from swfactory.db.repo import upsert_models
from swfactory.domain.types import Role, SelectionMode
from swfactory.events import EventBus
from swfactory.models.catalog import Catalog, ModelInfo
from swfactory.models.profiles import ResolvedProfile, RoleModel
from swfactory.models.selection import (
    ModelSelector,
    NoModelAvailable,
    SelectionContext,
    pick_model,
)


def info(alias: str, upstream: str, **kw) -> ModelInfo:
    base = dict(
        alias=alias, provider="vertex", upstream_id=upstream, strength="mid",
        price_in_per_mtok=Decimal(1), price_out_per_mtok=Decimal(2), context_window=100000,
        supports_tools=True, messages_tools_ok=True, available=True, error_class=None,
        latency_ms=10, last_error=None,
    )  # fmt: skip
    base.update(kw)
    return ModelInfo(**base)  # type: ignore[arg-type]


SONNET = info("sonnet@vertex", "claude-sonnet-4-6")
FLASH = info("flash@vertex", "gemini-4-flash", messages_tools_ok=None)
PRO = info("pro@vertex", "gemini-4-pro")
QWEN = info("qwen@ollama", "qwen/qwen3-coder", messages_tools_ok=False)
INFOS = {i.alias: i for i in (SONNET, FLASH, PRO, QWEN)}
QUOTA = replace(PRO, available=False, error_class="quota")
NOT_ENABLED = replace(SONNET, available=False, error_class="not_enabled")


def RM(**kw) -> RoleModel:  # noqa: N802
    base = dict(
        engine="opencode", model="pro@vertex", mode=SelectionMode.FIXED, tiers=(),
        fallback=(), sticky=False, allow_experimental=False,
    )  # fmt: skip
    base.update(kw)
    return RoleModel(**base)  # type: ignore[arg-type]


def test_review_focus_3_quota_falls_back():
    p = pick_model(
        RM(model="pro@vertex", fallback=("sonnet@vertex", "flash@vertex")),
        {**INFOS, "pro@vertex": QUOTA},
        sticky_alias=None,
    )
    assert (p.alias, p.fallback_from) == ("sonnet@vertex", "pro@vertex")
    assert p.reason == "fallback: pro@vertex unavailable (quota)"
    assert len(p.options) == 3


def test_primary_reason():
    p = pick_model(RM(), INFOS, sticky_alias=None)
    assert (p.alias, p.reason, p.fallback_from) == ("pro@vertex", "primary", None)


def test_excluded_alias_falls_back():
    p = pick_model(
        RM(model="pro@vertex", fallback=("sonnet@vertex",)),
        INFOS,
        sticky_alias=None,
        exclude=frozenset({"pro@vertex"}),
    )
    assert (p.alias, p.fallback_from, p.reason) == (
        "sonnet@vertex",
        "pro@vertex",
        "fallback: pro@vertex failed in this step",
    )


def test_skips_no_and_unallowed_experimental():
    rm = RM(engine="claude_code", model="qwen@ollama", fallback=("flash@vertex", "sonnet@vertex"))
    assert pick_model(rm, INFOS, sticky_alias=None).alias == "sonnet@vertex"
    assert (
        pick_model(replace(rm, allow_experimental=True), INFOS, sticky_alias=None).alias
        == "flash@vertex"
    )


def test_sticky_reuses_prior_model():
    p = pick_model(RM(model="pro@vertex", sticky=True), INFOS, sticky_alias="sonnet@vertex")
    assert (p.alias, p.reason) == ("sonnet@vertex", "sticky")


def test_sticky_model_unavailable_falls_back():
    p = pick_model(
        RM(model="pro@vertex", sticky=True),
        {**INFOS, "sonnet@vertex": NOT_ENABLED},
        sticky_alias="sonnet@vertex",
    )
    assert p.fallback_from == "sonnet@vertex" and p.alias == "pro@vertex"


def test_nothing_available():
    p = pick_model(RM(model="pro@vertex", fallback=()), {"pro@vertex": QUOTA}, sticky_alias=None)
    assert p.alias is None


# -- db ----------------------------------------------------------------------


def profile(name: str, **roles: RoleModel) -> ResolvedProfile:
    return ResolvedProfile(
        name=name, roles={Role(k.replace("_", "-")): v for k, v in roles.items()}
    )


PROFILE = profile(
    "p", builder=RM(model="pro@vertex", fallback=("sonnet@vertex", "flash@vertex"), sticky=True)
)
PROFILE_NO_FALLBACK = profile("q", builder=RM(model="pro@vertex"))
TEAM_GEMINI = profile("g", reviewer_security=RM(model="sonnet@vertex", fallback=("pro@vertex",)))


def make_ctx(job_id, **kw) -> SelectionContext:
    base = dict(
        job_id=job_id, app_slug="a", role=Role.BUILDER, round=1, feature_chars=10,
        spec_chars=10, diff_files=0, diff_lines=0, prior_verdicts={},
        budget_left_usd=Decimal(10), builder_family=None,
    )  # fmt: skip
    base.update(kw)
    return SelectionContext(**base)  # type: ignore[arg-type]


@pytest.fixture
async def env(sessions):
    await activate(sessions, {"a": {}})
    job_id = (await enq(sessions)).id
    async with sessions.begin() as s:
        await upsert_models(
            s,
            [
                QUOTA,
                SONNET,
                FLASH,
                replace(SONNET, available=False, error_class="not_enabled", alias="x@y"),
            ],
            probed_at=datetime.now(UTC),
        )
    sel = ModelSelector(sessions, Catalog(sessions, None, None), EventBus(sessions, ""))
    return sel, job_id, sessions


async def rows(sessions):
    async with sessions() as s:
        r = await s.execute(text("select * from app.model_decisions order by created_at, id"))
        return [dict(x._mapping) for x in r.all()]


@pytest.mark.db
async def test_review_focus_3_selector_records(env):
    sel, job_id, sessions = env
    choice = await sel.choose(PROFILE, make_ctx(job_id, round=2))
    assert choice.model.alias == "sonnet@vertex" and choice.mode == SelectionMode.FIXED
    row = (await rows(sessions))[-1]
    assert row["id"] == choice.decision_id
    assert row["reason"].startswith("fallback:") and len(row["options"]) == 3
    assert row["purpose"] == "model" and row["answer"] is None
    assert (await job_events(sessions, "model.fallback"))[-1] == {
        "role": "builder",
        "round": 2,
        "requested": "pro@vertex",
        "used": "sonnet@vertex",
        "source": "selection",
    }
    # sticky: the next round reuses the prior model
    again = await sel.choose(PROFILE, make_ctx(job_id, round=3))
    assert again.model.alias == "sonnet@vertex" and again.reason == "sticky"
    with pytest.raises(NoModelAvailable):
        await sel.choose(PROFILE_NO_FALLBACK, make_ctx(job_id, role=Role.BUILDER, round=2))
    last = (await rows(sessions))[-1]
    assert last["chosen"] is None and last["reason"] == "no model available"


@pytest.mark.db
async def test_d17_security_same_family_recorded(env):
    sel, job_id, sessions = env
    prof = profile("g", reviewer_security=RM(model="sonnet@vertex", fallback=("flash@vertex",)))
    # claude unavailable: the probe row for sonnet says not enabled
    async with sessions.begin() as s:
        await upsert_models(
            s,
            [replace(SONNET, available=False, error_class="not_enabled")],
            probed_at=datetime.now(UTC),
        )
    choice = await sel.choose(
        prof,
        make_ctx(job_id, role=Role.REVIEWER_SECURITY, builder_family="gemini"),
    )
    assert choice.model.alias == "flash@vertex"
    row = (await rows(sessions))[-1]
    assert row["reason"].endswith("; same family as builder (gemini), D17")
