import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from tests.support.db_helpers import activate, enq

from swfactory.db.repo import upsert_models
from swfactory.definition.models import TIER_ROLES, BenchConfigDef, Profile
from swfactory.domain.types import Role, SelectionMode
from swfactory.events import EventBus
from swfactory.models.catalog import Catalog, ModelInfo
from swfactory.models.profiles import ResolvedProfile, RoleModel, TierOption
from swfactory.models.router_client import LayaAnswer
from swfactory.models.selection import (
    ModelSelector,
    Pick,
    SelectionContext,
    gate_tier,
    selection_state,
    tier_question,
)
from swfactory.roles.render import estimate_tokens


def info(alias: str, upstream: str, **kw) -> ModelInfo:
    base = dict(
        alias=alias, provider="vertex", upstream_id=upstream, strength="mid",
        price_in_per_mtok=D(1), price_out_per_mtok=D(2), context_window=100000,
        supports_tools=True, messages_tools_ok=True, available=True, error_class=None,
        latency_ms=10, last_error=None,
    )  # fmt: skip
    base.update(kw)
    return ModelInfo(**base)  # type: ignore[arg-type]


LITE = info(
    "lite@vertex", "gemini-4-flash-lite", price_in_per_mtok=D("0.1"), price_out_per_mtok=D("0.4")
)
MID = info("mid@vertex", "gemini-4-flash")
PRO = info("pro@vertex", "gemini-4-pro")
BIG = info("big@vertex", "gemini-4-ultra", price_in_per_mtok=D(100), price_out_per_mtok=D(200))
INFOS = {i.alias: i for i in (LITE, MID, PRO, BIG)}
QUOTA = replace(LITE, available=False, error_class="quota")
INFOS_CLAUDE_CODE_ONLY = {**INFOS, "lite@vertex": info("lite@vertex", "gemini-4-flash-lite")}

TIERS = (
    TierOption("A", "lite@vertex", "small and mechanical"),
    TierOption("B", "mid@vertex", "typical work"),
    TierOption("C", "big@vertex", "hard work"),
)
TIERS_RAW = [{"label": t.label, "model": t.model, "description": t.description} for t in TIERS]


def RM(**kw) -> RoleModel:  # noqa: N802
    base = dict(
        engine="opencode", model="pro@vertex", mode=SelectionMode.LAYA, tiers=TIERS,
        fallback=(), sticky=False, allow_experimental=False,
    )  # fmt: skip
    base.update(kw)
    return RoleModel(**base)  # type: ignore[arg-type]


RM_LAYA = RM()
FIXED_PICK = Pick("pro@vertex", "primary", None, ())


def ans(choice, route, head: str | None = "v1") -> LayaAnswer:
    return LayaAnswer(choice, {"A": 0.7}, route, 5, None, head, "r1")


def err(e) -> LayaAnswer:
    return LayaAnswer(None, {}, None, 5, e)


def make_ctx(job_id=None, **kw) -> SelectionContext:
    from uuid import uuid4

    base = dict(
        job_id=job_id or uuid4(), app_slug="a", role=Role.BUILDER, round=1, feature_chars=900,
        spec_chars=2000, diff_files=3, diff_lines=40, prior_verdicts={"security": "PASS"},
        budget_left_usd=D(10), builder_family=None,
    )  # fmt: skip
    base.update(kw)
    return SelectionContext(**base)  # type: ignore[arg-type]


def test_tier_labels_are_opaque():
    q = tier_question(Role.BUILDER, TIERS)["tier.builder"]
    assert list(q["criteria"]) == ["A", "B", "C"]
    assert q["instructions"] == "Which capability tier should do this step?"
    assert "lite@vertex" not in json.dumps(q)


def test_state_under_512_tokens():
    assert estimate_tokens(json.dumps(selection_state(make_ctx()))) < 512


def test_typical_usage_covers_all_tier_roles():
    from swfactory.models.selection import TYPICAL_USAGE

    assert TYPICAL_USAGE[Role.BUILDER].input_tokens == 400_000
    assert TYPICAL_USAGE[Role.JUDGE].output_tokens == 2_000


@pytest.mark.parametrize(
    "answer,infos,budget,want",
    [
        (ans("A", "auto"), INFOS, D("5"), "laya: tier A"),
        (ans("A", "review"), INFOS, D("5"), "laya fallback: route review"),
        (ans("D", "auto"), INFOS, D("5"), "laya fallback: tier D not allowed"),
        (ans("A", "auto"), {**INFOS, "lite@vertex": QUOTA}, D("5"),
         "laya fallback: tier A model unavailable (quota)"),
        (ans("A", "auto"), INFOS_CLAUDE_CODE_ONLY, D("5"),
         "laya fallback: tier A engine support experimental"),
        (ans("C", "auto"), INFOS, D("0.05"), "laya fallback: tier C not affordable"),
        (ans("A", "auto", head=None), INFOS, D("5"), "laya fallback: no fitted head"),
        (err("timeout"), INFOS, D("5"), "laya fallback: router error (timeout)"),
    ],
)  # fmt: skip
def test_gate(answer, infos, budget, want):
    rm = RM_LAYA
    if "experimental" in want:
        # opencode + a non-tool-tested model is experimental; make that explicit
        infos = {**infos, "lite@vertex": replace(LITE, messages_tools_ok=None)}
        rm = RM(engine="claude_code")
    p, reason = gate_tier(rm, FIXED_PICK, answer, infos, budget, Role.BUILDER)
    assert reason.startswith(want), reason
    assert p.alias == ("lite@vertex" if want == "laya: tier A" else FIXED_PICK.alias)


def test_gate_experimental_allowed_when_opted_in():
    infos = {**INFOS, "lite@vertex": replace(LITE, messages_tools_ok=None)}
    rm = RM(engine="claude_code", allow_experimental=True)
    pick, reason = gate_tier(rm, FIXED_PICK, ans("A", "auto"), infos, D(5), Role.BUILDER)
    assert (pick.alias, reason) == ("lite@vertex", "laya: tier A")


class Spy:
    def __init__(self, answer: LayaAnswer | None = None, down: bool = False):
        self.answer, self.down, self.calls = answer, down, []

    async def decide(self, state, questions, *, pins=None):
        self.calls.append(type("C", (), {"state": state, "questions": questions, "pins": pins}))
        if self.down:
            raise RuntimeError("router down")
        return {q: self.answer for q in questions}


def fake(choice, route) -> Spy:
    return Spy(ans(choice, route))


PROFILE_SHADOW = ResolvedProfile(name="s", roles={Role.BUILDER: RM(mode=SelectionMode.SHADOW)})
PROFILE_LAYA = ResolvedProfile(name="l", roles={Role.BUILDER: RM()})
PROFILE_LAYA_STICKY = ResolvedProfile(name="l", roles={Role.BUILDER: RM(sticky=True)})


@pytest.fixture
async def env(sessions):
    async def clean():
        async with sessions.begin() as s:
            await s.execute(text("truncate app.model_decisions cascade"))

    await activate(sessions, {"a": {}})
    await clean()
    job_id = (await enq(sessions)).id
    async with sessions.begin() as s:
        await upsert_models(s, list(INFOS.values()), probed_at=datetime.now(UTC))
    yield sessions, job_id
    await clean()


def selector(sessions, router):
    return ModelSelector(
        sessions, Catalog(sessions, None, None), EventBus(sessions, ""), router=router
    )


async def decision(sessions, did):
    async with sessions() as s:
        r = await s.execute(text("select * from app.model_decisions where id = :i"), {"i": did})
        return r.one()._mapping


@pytest.mark.db
async def test_shadow_records_but_uses_fixed(env):
    sessions, job_id = env
    c = await selector(sessions, fake("A", "auto")).choose(PROFILE_SHADOW, make_ctx(job_id))
    assert c.mode == SelectionMode.SHADOW and c.model.alias == "pro@vertex" and c.tier is None
    row = await decision(sessions, c.decision_id)
    assert row["answer"]["choice"] == "A" and row["answer"]["state"]["role"] == "builder"
    assert row["reason"] == "shadow: fixed used" and row["router_version"] == "r1"
    assert row["head_version"] == "v1"


@pytest.mark.db
async def test_laya_uses_tier_when_gates_pass(env):
    sessions, job_id = env
    c = await selector(sessions, fake("A", "auto")).choose(PROFILE_LAYA, make_ctx(job_id))
    assert (c.model.alias, c.tier, c.reason) == ("lite@vertex", "A", "laya: tier A")
    assert c.mode == SelectionMode.LAYA


@pytest.mark.db
async def test_laya_fallback_keeps_fixed(env):
    sessions, job_id = env
    c = await selector(sessions, fake("A", "review")).choose(PROFILE_LAYA, make_ctx(job_id))
    assert (c.model.alias, c.tier) == ("pro@vertex", None)
    assert c.reason == "laya fallback: route review"


@pytest.mark.db
async def test_laya_never_fails_a_job(env):
    sessions, job_id = env
    c = await selector(sessions, Spy(down=True)).choose(PROFILE_LAYA, make_ctx(job_id))
    assert c.model.alias == "pro@vertex" and c.reason.startswith("laya fallback: router error")


@pytest.mark.db
async def test_no_router_configured(env):
    sessions, job_id = env
    c = await selector(sessions, None).choose(PROFILE_LAYA, make_ctx(job_id))
    assert c.model.alias == "pro@vertex" and c.reason == "laya fallback: router not configured"


@pytest.mark.db
async def test_sticky_skips_router(env):
    sessions, job_id = env
    sel = selector(sessions, spy := fake("A", "auto"))
    first = await sel.choose(PROFILE_LAYA_STICKY, make_ctx(job_id))
    assert first.tier == "A"  # round 1 asks the router
    spy.calls.clear()
    c = await sel.choose(PROFILE_LAYA_STICKY, make_ctx(job_id, round=2))
    assert c.reason == "sticky" and spy.calls == [] and c.model.alias == "lite@vertex"


@pytest.mark.db
async def test_fixed_mode_never_calls_router(env):
    sessions, job_id = env
    prof = ResolvedProfile(name="f", roles={Role.BUILDER: RM(mode=SelectionMode.FIXED)})
    spy = fake("A", "auto")
    c = await selector(sessions, spy).choose(prof, make_ctx(job_id))
    assert spy.calls == [] and c.reason == "primary"


@pytest.mark.db
async def test_pins_reach_router(env):
    sessions, job_id = env
    spy = fake("A", "auto")
    await selector(sessions, spy).choose(
        PROFILE_LAYA, make_ctx(job_id, laya_pins={"tier.builder": 2})
    )
    assert spy.calls[0].pins == {"tier.builder": 2}


def test_tier_modes_only_for_tier_roles():
    assert TIER_ROLES == {Role.BUILDER, Role.SPEC_WRITER}
    with pytest.raises(
        ValidationError,
        match="reviewer-security: mode laya is allowed only for builder and spec-writer",
    ):
        Profile.model_validate(
            {
                "roles": {
                    "reviewer-security": {
                        "engine": "direct",
                        "model": "pro@vertex",
                        "mode": "laya",
                        "tiers": TIERS_RAW,
                    }
                }
            }
        )
    with pytest.raises(
        ValidationError, match="approver: mode shadow is allowed only for builder and spec-writer"
    ):
        BenchConfigDef.model_validate(
            {"name": "x", "profile": "team-gemini", "roles": {"approver": {"mode": "shadow"}}}
        )
    Profile.model_validate(
        {
            "roles": {
                "builder": {
                    "engine": "direct",
                    "model": "pro@vertex",
                    "mode": "laya",
                    "tiers": TIERS_RAW,
                }
            }
        }
    )
