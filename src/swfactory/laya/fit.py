"""Fit a router head for one question (M11.8). A fitted head is a candidate, never active."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import LayaHead
from swfactory.definition.store import load_active
from swfactory.domain.types import Role
from swfactory.laya.export import export_rows, source_counts
from swfactory.models.profiles import TierOption
from swfactory.models.selection import tier_question
from swfactory.models.triage import TRIAGE_QUESTIONS

MIN_FIT_ROWS = 50
MIN_ROWS_PER_CLASS = 10
FIT_PATH = "/v1/factory/fit"
FIT_TIMEOUT_S = 1800.0  # a fit is not a 2 s decision: about 0.6 s per row on a CPU
# The router trains on a CPU at about 0.6 s per row (291 rows took 3 minutes; 9,700 imported size
# rows kept it at 700% for many minutes). Your own rows always stay; imported rows are sampled
# (balanced across labels, seeded) to fit this budget, which is about 11 minutes.
MAX_FIT_ROWS = 1000


class FitRefused(Exception):
    """The fit was not attempted (nothing was sent to the router)."""


class NotEnoughRows(FitRefused):
    pass


class RouterUnreachable(Exception):
    pass


class RouterRefused(Exception):
    pass


def questions_sha256(definition: Mapping[str, Any]) -> str:
    """Hash of one question definition as the router receives it (canonical JSON)."""
    blob = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


async def question_definition(s: AsyncSession, question: str) -> dict[str, Any]:
    """The question exactly as the orchestrator sends it to the router."""
    if question in TRIAGE_QUESTIONS:
        return dict(TRIAGE_QUESTIONS[question])
    role = Role(question.removeprefix("tier."))
    active = await load_active(s)
    if active is not None:
        for _, profile in sorted(active[1].profiles.items()):
            rp = profile.roles.get(role)
            if rp is not None and rp.tiers:
                tiers = [TierOption(t.label, t.model, t.description) for t in rp.tiers]
                return tier_question(role, tiers)[question]
    raise FitRefused(f"refused: no tiers are configured for {question}")


def _csv_text(rows: list[Any], question: str) -> str:
    out = io.StringIO(newline="")
    w = csv.writer(out)
    w.writerow(["state", question])
    for r in rows:
        w.writerow([r.state, r.label])
    return out.getvalue()


def _check_rows(question: str, rows: list[Any]) -> Counter[str]:
    counts: Counter[str] = Counter(r.label for r in rows)
    if len(rows) < MIN_FIT_ROWS:
        raise NotEnoughRows(
            f"{len(rows)} labelled rows for {question}, need at least {MIN_FIT_ROWS}"
        )
    big = [k for k, v in counts.items() if v >= MIN_ROWS_PER_CLASS]
    if len(big) < 2:
        n = len(counts)
        raise NotEnoughRows(
            f"{question} has {n} label class{'' if n == 1 else 'es'}, need at least 2 "
            f"with {MIN_ROWS_PER_CLASS} rows each"
        )
    return counts


def cap_rows(rows: Sequence[Any], max_rows: int, *, seed: int = 0) -> list[Any]:
    """At most ``max_rows`` rows: every row of your own stays, imported (``external:``) rows are
    sampled, balanced across their labels (a label with few rows keeps all of them), with a seeded
    shuffle so the same data gives the same fit. The order of the rows is kept."""
    rows = list(rows)
    if len(rows) <= max_rows:
        return rows
    ext = [i for i, r in enumerate(rows) if r.source.startswith("external:")]
    budget = max(0, max_rows - (len(rows) - len(ext)))
    by_label: dict[str, list[int]] = defaultdict(list)
    for i in ext:
        by_label[rows[i].label].append(i)
    rng = random.Random(seed)
    for idxs in by_label.values():
        rng.shuffle(idxs)
    keep: set[int] = set()
    pools = {k: list(v) for k, v in sorted(by_label.items())}
    while budget > 0 and any(pools.values()):  # round robin: balanced, small labels run dry first
        for k in list(pools):
            if budget > 0 and pools[k]:
                keep.add(pools[k].pop())
                budget -= 1
    return [r for i, r in enumerate(rows) if i not in set(ext) or i in keep]


@dataclass(frozen=True)
class FitPlan:
    """Everything a fit needs from the database, read once, so the router call holds no session."""

    question: str
    rows: list[Any]
    counts: Counter[str]
    definition: dict[str, Any]
    version: int
    csv_text: str


@dataclass(frozen=True)
class RouterFit:
    head_sha256: str
    metrics: dict[str, Any]
    router_version: str | None


async def plan_fit(s: AsyncSession, question: str, *, max_rows: int = MAX_FIT_ROWS) -> FitPlan:
    """Phase 1 (database read): the rows, the question, the next version. Raises before any HTTP."""
    rows = cap_rows(await export_rows(s, question), max_rows)
    counts = _check_rows(question, rows)
    definition = await question_definition(s, question)
    last = await s.scalar(select(func.max(LayaHead.version)).where(LayaHead.question == question))
    return FitPlan(question, rows, counts, definition, (last or 0) + 1, _csv_text(rows, question))


async def call_router(
    plan: FitPlan,
    router_url: str,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RouterFit:
    """Phase 2 (no database): train in the router. Can take minutes."""
    if not router_url:
        raise RouterUnreachable("FACTORY_LAYA_URL is not set")
    body = {
        "question_id": plan.question,
        "questions": {plan.question: plan.definition},
        "csv": plan.csv_text,
        "version": plan.version,
    }
    try:
        async with httpx.AsyncClient(timeout=FIT_TIMEOUT_S, transport=transport) as http:
            resp = await http.post(
                router_url.rstrip("/") + FIT_PATH,
                json=body,
                headers={"authorization": f"Bearer {api_key}"},
            )
    except httpx.TransportError as e:
        raise RouterUnreachable(f"router unreachable: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise RouterRefused(f"router answered http {resp.status_code}: {resp.text[:200]}")
    try:
        doc = resp.json()
        return RouterFit(
            str(doc["head_sha256"]),
            dict(doc.get("metrics") or {}),
            resp.headers.get("x-factory-router-version"),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise RouterRefused("router answered with a bad fit response") from e


async def record_fit(s: AsyncSession, plan: FitPlan, fit: RouterFit) -> LayaHead:
    """Phase 3 (database write): store the candidate head."""
    head = LayaHead(
        question=plan.question,
        version=plan.version,
        rows=len(plan.rows),
        label_counts=dict(plan.counts),
        questions_sha256=questions_sha256(plan.definition),
        csv_sha256=hashlib.sha256(plan.csv_text.encode()).hexdigest(),
        path=f"/heads/{plan.question}/v{plan.version}",
        router_version=fit.router_version,
        metrics=fit.metrics,
        active=False,
        head_sha256=fit.head_sha256,
        train_refs=sorted({r.source for r in plan.rows}),
        source_counts=dict(source_counts(plan.rows)),
    )
    s.add(head)
    await s.flush()
    return head


async def fit_with_sessions(
    sessions: async_sessionmaker[AsyncSession],
    router_url: str,
    api_key: str,
    question: str,
    *,
    max_rows: int = MAX_FIT_ROWS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LayaHead:
    """A fit with two short transactions and none open while the router trains (a transaction idle
    through a long router call is ended by Postgres's idle_in_transaction_session_timeout)."""
    async with sessions() as s, s.begin():
        plan = await plan_fit(s, question, max_rows=max_rows)
    fit = await call_router(plan, router_url, api_key, transport=transport)
    async with sessions() as s, s.begin():
        return await record_fit(s, plan, fit)


async def fit_question(
    s: AsyncSession,
    router_url: str,
    api_key: str,
    question: str,
    *,
    max_rows: int = MAX_FIT_ROWS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LayaHead:
    """The three phases on one caller-owned session. Use fit_with_sessions for a long fit."""
    plan = await plan_fit(s, question, max_rows=max_rows)
    fit = await call_router(plan, router_url, api_key, transport=transport)
    return await record_fit(s, plan, fit)
