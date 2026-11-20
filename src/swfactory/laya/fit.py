"""Fit a router head for one question (M11.8). A fitted head is a candidate, never active."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
FIT_TIMEOUT_S = 600.0  # a fit is not a 2 s decision


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


async def fit_question(
    s: AsyncSession,
    router_url: str,
    api_key: str,
    question: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LayaHead:
    rows = await export_rows(s, question)
    counts = _check_rows(question, rows)  # before any HTTP call
    definition = await question_definition(s, question)
    last = await s.scalar(select(func.max(LayaHead.version)).where(LayaHead.question == question))
    version = (last or 0) + 1
    csv_text = _csv_text(rows, question)
    if not router_url:
        raise RouterUnreachable("FACTORY_LAYA_URL is not set")
    body = {
        "question_id": question,
        "questions": {question: definition},
        "csv": csv_text,
        "version": version,
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
        head_sha = str(doc["head_sha256"])
        metrics = doc.get("metrics") or {}
    except (ValueError, KeyError, TypeError) as e:
        raise RouterRefused("router answered with a bad fit response") from e
    head = LayaHead(
        question=question,
        version=version,
        rows=len(rows),
        label_counts=dict(counts),
        questions_sha256=questions_sha256(definition),
        csv_sha256=hashlib.sha256(csv_text.encode()).hexdigest(),
        path=f"/heads/{question}/v{version}",
        router_version=resp.headers.get("x-factory-router-version"),
        metrics=metrics,
        active=False,
        head_sha256=head_sha,
        train_refs=sorted({r.source for r in rows}),
        source_counts=dict(source_counts(rows)),
    )
    s.add(head)
    await s.flush()
    return head
