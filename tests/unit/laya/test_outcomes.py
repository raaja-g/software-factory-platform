"""M11.6: outcome labels written to model_decisions after each job and bench run."""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from tests.support.pg import truncate_app_tables

from swfactory.db.tables import (
    AgentRun,
    BenchResult,
    BenchRun,
    CatalogModel,
    Command,
    DefinitionRow,
    Job,
    ModelDecision,
)
from swfactory.laya.export import (
    cheapest_passing_tier,
    label_bench_outcomes,
    label_job_outcomes,
)

SHA = "sha-m11-6"
SEQ = itertools.count(1)
TIER_MODELS = {"lite@t": D("0.4"), "mid@t": D("2.8"), "big@t": D("12")}
TIERS = [
    {"label": "A", "model": "lite@t", "description": "a"},
    {"label": "B", "model": "mid@t", "description": "b"},
    {"label": "C", "model": "big@t", "description": "c"},
]


def test_cheapest_passing():
    assert (
        cheapest_passing_tier([("C", D("12"), True), ("A", D("0.4"), False), ("B", D("2.8"), True)])
        == "B"
    )


def test_none_passed():
    assert cheapest_passing_tier([("A", D("0.4"), False)]) is None


@pytest.fixture
async def db(sessions):
    async with sessions() as s, s.begin():
        s.add(
            DefinitionRow(
                sha=SHA,
                source="t",
                content={
                    "profiles": {"p": {"roles": {"builder": {"model": "big@t", "tiers": TIERS}}}}
                },
            )
        )
        for alias, price in TIER_MODELS.items():
            s.add(
                CatalogModel(
                    alias=alias,
                    provider="x",
                    upstream_id=alias,
                    price_in_per_mtok=price,
                    price_out_per_mtok=D(0),
                )
            )
    yield sessions
    async with sessions.kw["bind"].connect() as conn:
        await truncate_app_tables(conn)


async def make_job(
    s, model, *, verdicts=("PASS",), decision="APPROVE", rework=False, role="builder"
) -> uuid.UUID:
    jid = uuid.uuid4()
    s.add(
        Job(
            id=jid, seq=next(SEQ), slug="j", title="j", feature="f", requested_by="me",
            budget_usd=1,
            definition_sha=SHA, profile_name="p", decision=decision, kind="improver",
        )
    )  # fmt: skip
    await s.flush()
    for i, v in enumerate(verdicts):
        s.add(AgentRun(job_id=jid, role=f"reviewer-{i}", round=1, engine="fake", verdict=v))
    if rework:
        s.add(Command(job_id=jid, action="rework", requested_by="me"))
    s.add(
        ModelDecision(
            job_id=jid, role=role, round=1, purpose="model", mode="laya", options=[], chosen=model
        )
    )
    await s.flush()
    return jid


async def outcome(s, jid, role="builder"):
    s.expire_all()
    return await s.scalar(
        select(ModelDecision.outcome).where(ModelDecision.job_id == jid, ModelDecision.role == role)
    )


@pytest.mark.db
async def test_job_labels(db):
    async with db() as s, s.begin():
        passing = await make_job(s, "big@t", verdicts=("PASS", "SKIPPED"))
        reworked = await make_job(s, "big@t", rework=True)
        odd = await make_job(s, "other@t")
        assert await label_job_outcomes(s, passing) == 1
        assert await outcome(s, passing) == {
            "passed_r1": True, "human_rework": False, "tier": "C", "label": "C", "source": "job",
        }  # fmt: skip
        await label_job_outcomes(s, reworked)
        assert (await outcome(s, reworked))["label"] is None  # nothing above the dearest tier
        await label_job_outcomes(s, odd)
        assert (await outcome(s, odd))["tier"] is None


@pytest.mark.db
async def test_error_verdict_is_not_a_pass(db):
    async with db() as s, s.begin():
        j = await make_job(s, "lite@t", verdicts=("PASS", "ERROR"))
        await label_job_outcomes(s, j)
        o = await outcome(s, j)
        assert o["passed_r1"] is False and o["label"] == "B"


@pytest.mark.db
async def test_bench_groups_by_task(db):
    async with db() as s, s.begin():
        a = await make_job(s, "lite@t", verdicts=("CHANGES",), decision="ESCALATE")
        b = await make_job(s, "mid@t")
        run = BenchRun(suite="x", configurations=[])
        s.add(run)
        await s.flush()
        for cfg, j in (("tier-a", a), ("tier-b", b)):
            s.add(BenchResult(run_id=run.id, task="t1", configuration=cfg, job_id=j))
        await s.flush()
        assert await label_bench_outcomes(s, run.id) == 2
        assert {(await outcome(s, j))["label"] for j in (a, b)} == {"B"}
        assert (await outcome(s, a))["source"] == "bench"


@pytest.mark.db
async def test_idempotent(db):
    async with db() as s, s.begin():
        j = await make_job(s, "big@t")
        await label_job_outcomes(s, j)
        first = await outcome(s, j)
        assert await label_job_outcomes(s, j) == 1
        assert await outcome(s, j) == first


@pytest.mark.db
async def test_failure_labels_next_tier_up(db):
    async with db() as s, s.begin():
        j = await make_job(s, "lite@t", verdicts=("CHANGES",), decision="ESCALATE")
        await label_job_outcomes(s, j)
        assert (await outcome(s, j))["label"] == "B"


@pytest.mark.db
async def test_reviewer_decisions_are_not_labelled(db):
    async with db() as s, s.begin():
        j = await make_job(s, "big@t", role="reviewer-code")
        assert await label_job_outcomes(s, j) == 0
        assert await outcome(s, j, "reviewer-code") is None
