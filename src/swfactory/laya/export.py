"""Outcome labels for tier decisions (M11.6) and the training-data export (M11.7)."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.commands import format_job_ref
from swfactory.db.tables import (
    AgentRun,
    App,
    BenchResult,
    BenchRun,
    CatalogModel,
    Command,
    DefinitionRow,
    Job,
    JobEvent,
    LayaDataset,
    LayaImportedRow,
    ModelDecision,
)
from swfactory.definition.models import Definition
from swfactory.domain.types import Decision, HumanAction, Role, Verdict
from swfactory.models.triage import TOUCHES_UI_LABELS, triage_state

try:  # defined with the tier-choice rules (M11.5)
    from swfactory.definition.models import TIER_ROLES  # pyright: ignore[reportAttributeAccessIssue]
except ImportError:
    TIER_ROLES: frozenset[Role] = frozenset({Role.BUILDER, Role.SPEC_WRITER})

_PASSING = (Verdict.PASS.value, Verdict.SKIPPED.value)


@dataclass(frozen=True)
class JobOutcome:
    job_id: UUID
    passed_r1: bool
    human_rework: bool
    tiers: Mapping[str, str | None]  # role -> tier label used


def cheapest_passing_tier(candidates: Sequence[tuple[str, Decimal, bool]]) -> str | None:
    """The label of the cheapest candidate (price per Mtok in+out) that passed, else None."""
    passed = [(price, label) for label, price, ok in candidates if ok]
    return min(passed)[1] if passed else None


@dataclass(frozen=True)
class _Tier:
    label: str
    model: str
    price: Decimal | None


async def _definition_content(s: AsyncSession, sha: str | None) -> Mapping[str, Any]:
    row = await s.get(DefinitionRow, sha) if sha else None
    if row is None:
        row = await s.scalar(select(DefinitionRow).where(DefinitionRow.active.is_(True)))
    content = row.content if row is not None else None
    return content if isinstance(content, dict) else {}


async def _role_tiers(s: AsyncSession, job: Job, role: str) -> list[_Tier]:
    content = await _definition_content(s, job.definition_sha)
    profile = (content.get("profiles") or {}).get(job.profile_name or "") or {}
    raw = ((profile.get("roles") or {}).get(role) or {}).get("tiers") or []
    models = list(
        await s.scalars(
            select(CatalogModel).where(CatalogModel.alias.in_([t["model"] for t in raw]))
        )
    )
    price: dict[str, Decimal | None] = {
        m.alias: (
            None
            if m.price_in_per_mtok is None or m.price_out_per_mtok is None
            else m.price_in_per_mtok + m.price_out_per_mtok
        )
        for m in models
    }
    return [_Tier(t["label"], t["model"], price.get(t["model"])) for t in raw]


def _next_up(tiers: Sequence[_Tier], used: _Tier | None) -> str | None:
    """The cheapest tier priced above the one used; None when there is none or no price."""
    if used is None or used.price is None:
        return None
    above = [(t.price, t.label) for t in tiers if t.price is not None and t.price > used.price]
    return min(above)[1] if above else None


async def _passed_r1(s: AsyncSession, job: Job) -> bool:
    runs = (
        await s.scalars(
            select(AgentRun)
            .where(AgentRun.job_id == job.id, AgentRun.round == 1, AgentRun.verdict.is_not(None))
            .order_by(AgentRun.attempt)
        )
    ).all()
    latest: dict[str, str | None] = {}
    for r in runs:  # a retried attempt replaces the earlier one
        latest[r.role] = r.verdict
    return job.decision == Decision.APPROVE.value and all(v in _PASSING for v in latest.values())


async def _human_rework(s: AsyncSession, job_id: UUID) -> bool:
    found = await s.scalar(
        select(Command.id)
        .where(Command.job_id == job_id, Command.action == HumanAction.REWORK.value)
        .limit(1)
    )
    return found is not None


async def _decisions(s: AsyncSession, job_id: UUID) -> list[ModelDecision]:
    """The first model decision of each tier role (the choice that shaped round 1)."""
    rows = (
        await s.scalars(
            select(ModelDecision)
            .where(
                ModelDecision.job_id == job_id,
                ModelDecision.purpose == "model",
                ModelDecision.role.in_([r.value for r in TIER_ROLES]),
            )
            .order_by(ModelDecision.round.nulls_first(), ModelDecision.created_at, ModelDecision.id)
        )
    ).all()
    first: dict[str | None, ModelDecision] = {}
    for r in rows:
        first.setdefault(r.role, r)
    return list(first.values())


@dataclass(frozen=True)
class _Facts:
    job: Job
    passed_r1: bool
    human_rework: bool
    decisions: list[ModelDecision]

    @property
    def passed(self) -> bool:
        return self.passed_r1 and not self.human_rework


async def _facts(s: AsyncSession, job_id: UUID) -> _Facts | None:
    job = await s.get(Job, job_id)
    if job is None:
        return None
    return _Facts(
        job,
        await _passed_r1(s, job),
        await _human_rework(s, job_id),
        await _decisions(s, job_id),
    )


def _outcome(f: _Facts, tier: _Tier | None, label: str | None, source: str) -> dict[str, Any]:
    return {
        "passed_r1": f.passed_r1,
        "human_rework": f.human_rework,
        "tier": tier.label if tier else None,
        "label": label,
        "source": source,
    }


def _used(tiers: Sequence[_Tier], d: ModelDecision) -> _Tier | None:
    return next((t for t in tiers if t.model == d.chosen), None)


async def job_outcome(s: AsyncSession, job_id: UUID) -> JobOutcome | None:
    f = await _facts(s, job_id)
    if f is None:
        return None
    used = {
        d.role: (u.label if (u := _used(await _role_tiers(s, f.job, d.role), d)) else None)
        for d in f.decisions
        if d.role
    }
    return JobOutcome(job_id, f.passed_r1, f.human_rework, used)


async def label_job_outcomes(s: AsyncSession, job_id: UUID) -> int:
    """Write the outcome of each tier decision of one job; returns the rows updated."""
    f = await _facts(s, job_id)
    if f is None:
        return 0
    for d in f.decisions:
        tiers = await _role_tiers(s, f.job, d.role or "")
        used = _used(tiers, d)
        label = (used.label if f.passed else _next_up(tiers, used)) if used else None
        d.outcome = _outcome(f, used, label, "job")
    await s.flush()
    return len(f.decisions)


async def label_bench_outcomes(s: AsyncSession, bench_run_id: UUID) -> int:
    """Label every task of a bench run as a group: the cheapest tier that passed any trial."""
    results = (
        await s.scalars(
            select(BenchResult).where(
                BenchResult.run_id == bench_run_id, BenchResult.job_id.is_not(None)
            )
        )
    ).all()
    by_task: dict[str, list[UUID]] = defaultdict(list)
    for r in results:
        if r.job_id is not None:
            by_task[r.task].append(r.job_id)
    updated = 0
    for job_ids in by_task.values():
        facts = [f for j in job_ids if (f := await _facts(s, j)) is not None]
        for role in {d.role for f in facts for d in f.decisions if d.role}:
            trials: list[tuple[_Facts, ModelDecision, list[_Tier], _Tier | None]] = []
            for f in facts:
                for d in f.decisions:
                    if d.role == role:
                        tiers = await _role_tiers(s, f.job, role)
                        trials.append((f, d, tiers, _used(tiers, d)))
            priced = [
                (u.label, u.price, f.passed) for f, _, _, u in trials if u and u.price is not None
            ]
            label = cheapest_passing_tier(priced)
            if label is None and priced:  # nobody passed: one tier above the dearest tried
                dearest = max(
                    (u for _, _, _, u in trials if u and u.price is not None),
                    key=lambda u: u.price or 0,
                )
                label = _next_up(trials[0][2], dearest)
            for f, d, _, u in trials:
                d.outcome = _outcome(f, u, label, "bench")
                updated += 1
    await s.flush()
    return updated


# --- export (M11.7) ---------------------------------------------------------------------

QUESTIONS_TRIAGE = ("kind", "size", "risk", "touches_ui")


def question_ids(defn: Definition) -> list[str]:
    """Triage questions plus ``tier.<role>`` for each tier role that has tiers in some profile."""
    roles = {
        role.value
        for profile in defn.profiles.values()
        for role, rp in profile.roles.items()
        if role in TIER_ROLES and rp.tiers
    }
    return [*QUESTIONS_TRIAGE, *(f"tier.{r}" for r in sorted(roles))]


def known_question(question: str) -> bool:
    """Whether ``question`` could ever be exported, without looking at a definition."""
    if question in QUESTIONS_TRIAGE:
        return True
    return question.startswith("tier.") and question[5:] in {r.value for r in TIER_ROLES}


class ExportRow(NamedTuple):
    state: str
    label: str
    source: str


def _dump(state: Any) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


async def _latest_correction(s: AsyncSession, job_id: UUID) -> dict[str, Any] | None:
    content = await s.scalar(
        text(
            "SELECT content FROM app.artifacts_latest "
            "WHERE job_id = :j AND path = 'triage-correction.json'"
        ),
        {"j": job_id},
    )
    try:
        doc = json.loads(content) if content else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


async def _triage_rows(s: AsyncSession, question: str) -> list[tuple[Any, ExportRow]]:
    jobs = (
        await s.execute(
            select(Job, App)
            .join(App, App.id == Job.app_id)
            .where(
                select(JobEvent.id)
                .where(
                    JobEvent.job_id == Job.id,
                    JobEvent.kind == "human.action",
                    JobEvent.data["action"].astext == "correct_triage",
                )
                .exists()
            )
            .order_by(Job.created_at, Job.id)
        )
    ).all()
    out: list[tuple[Any, ExportRow]] = []
    for job, app in jobs:
        doc = await _latest_correction(s, job.id)
        if doc is None or question not in doc:
            continue
        value = doc[question]
        if question == "touches_ui":
            label = next((k for k, v in TOUCHES_UI_LABELS.items() if v is value), None)
        else:
            label = str(value) if value is not None else None
        if not label:
            continue
        state: Any = None
        for d in await s.scalars(
            select(ModelDecision)
            .where(ModelDecision.job_id == job.id, ModelDecision.purpose == "triage")
            .order_by(ModelDecision.created_at, ModelDecision.id)
        ):
            ans = d.answer if isinstance(d.answer, dict) else {}
            if ans.get("question") == question and ans.get("state"):
                state = ans["state"]
                break
        if state is None:
            state = triage_state(job, app)
        ref = format_job_ref(app.slug, job.seq)
        out.append((job.created_at, ExportRow(_dump(state), label, f"job:{ref}")))
    return out


async def _tier_rows(s: AsyncSession, role: str) -> list[tuple[Any, ExportRow]]:
    rows = (
        await s.execute(
            select(ModelDecision, Job, App)
            .join(Job, Job.id == ModelDecision.job_id)
            .outerjoin(App, App.id == Job.app_id)
            .where(
                ModelDecision.purpose == "model",
                ModelDecision.role == role,
                ModelDecision.outcome.is_not(None),
            )
            .order_by(Job.created_at, ModelDecision.created_at, ModelDecision.id)
        )
    ).all()
    out: list[tuple[Any, ExportRow]] = []
    for d, job, app in rows:
        ans = d.answer if isinstance(d.answer, dict) else {}
        outcome = d.outcome if isinstance(d.outcome, dict) else {}
        label, state = outcome.get("label"), ans.get("state")
        if not label or not state:
            continue
        if outcome.get("source") == "bench":
            hit = (
                await s.execute(
                    select(BenchRun.suite, BenchResult.task)
                    .join(BenchRun, BenchRun.id == BenchResult.run_id)
                    .where(BenchResult.job_id == job.id)
                )
            ).first()
            source = f"bench:{hit[0]}/{hit[1]}" if hit else f"job:{format_job_ref(*_ref(job, app))}"
        else:
            source = f"job:{format_job_ref(*_ref(job, app))}"
        out.append((job.created_at, ExportRow(_dump(state), str(label), source)))
    return out


def _ref(job: Job, app: App | None) -> tuple[str, int]:
    return (app.slug if app is not None else "improver", job.seq)


async def export_rows(s: AsyncSession, question: str) -> list[ExportRow]:
    """Training rows for one question, ordered by job creation time."""
    if question in QUESTIONS_TRIAGE:
        pairs = await _triage_rows(s, question)
    elif question.startswith("tier."):
        pairs = await _tier_rows(s, question.removeprefix("tier."))
    else:
        raise ValueError(f"unknown question '{question}'")
    own = [row for _, row in sorted(pairs, key=lambda p: p[0])]
    imported = (
        await s.execute(
            select(LayaImportedRow.state, LayaImportedRow.label, LayaDataset.name)
            .join(LayaDataset, LayaDataset.id == LayaImportedRow.dataset_id)
            .where(LayaImportedRow.question == question)
            .order_by(LayaDataset.imported_at, LayaDataset.name, LayaImportedRow.id)
        )
    ).all()
    return own + [ExportRow(st, lb, f"external:{name}") for st, lb, name in imported]


def source_counts(rows: Sequence[ExportRow]) -> Counter[str]:
    """Rows per source kind: ``own`` (job:), ``bench`` (bench:) and each ``external:<name>``."""
    out: Counter[str] = Counter()
    for r in rows:
        if r.source.startswith("bench:"):
            out["bench"] += 1
        elif r.source.startswith("external:"):
            out[r.source] += 1
        else:
            out["own"] += 1
    return out


def format_source_counts(counts: Mapping[str, int]) -> str:
    order = sorted(counts, key=lambda k: (k not in ("own", "bench"), k != "own", k))
    return ", ".join(f"{k}: {counts[k]}" for k in order)


def write_csv(rows: Sequence[Sequence[str]], path: Path, question: str) -> Counter[str]:
    """Write ``state,<question>`` rows; returns the count per label."""
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["state", question])
        for r in rows:
            w.writerow([r[0], r[1]])
            counts[r[1]] += 1
    return counts
