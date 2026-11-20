"""The scoring service: objective rows, sampled judges, the quality badge, one event."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from swfactory.db.repo import load_job_facts, replace_scores
from swfactory.db.tables import AgentRun
from swfactory.domain.types import JobKind, Role, RunStatus
from swfactory.graph.steps import pinned_definition
from swfactory.scoring.judge import run_judge, sampled
from swfactory.scoring.objective import OBJ_REVERTED, OBJ_SCORERS, ScoreRow, compute_signals

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.definition.store import DefinitionStore
    from swfactory.events import EventBus
    from swfactory.graph.deps import Deps
    from swfactory.models.catalog import Catalog

QUALITY = "obj.quality"
_DELIVERED = frozenset({"merged_unchanged", "merged", "delivered"})
_SCORE = {"good": Decimal("1.0"), "fair": Decimal("0.5"), "poor": Decimal("0.0")}


def quality_badge(rows: Sequence[ScoreRow]) -> ScoreRow:
    """good / fair / poor from the objective rows only: judges never count."""
    by = {r.scorer: r for r in rows if r.scorer.startswith("obj.")}

    def label(name: str) -> str | None:
        r = by.get(name)
        return None if r is None else r.label

    delivery = label("obj.delivery")
    reverted = label(OBJ_REVERTED) == "reverted"
    if reverted or delivery not in _DELIVERED | {"merged_changed"}:
        verdict, why = "poor", "reverted" if reverted else f"not delivered ({delivery})"
    elif (
        delivery in _DELIVERED
        and label("obj.decision") == "APPROVE"
        and label("obj.human") in (None, "none")
        and label("obj.fallback") in (None, "none")
    ):
        verdict, why = "good", "approved, delivered unchanged, no human action, no fallback"
    else:
        verdict, why = "fair", "delivered with a rework, override, change or fallback"
    anchor = by.get("obj.delivery")
    return ScoreRow(
        QUALITY,
        verdict,
        _SCORE[verdict],
        verdict == "good",
        why,
        None if anchor is None else anchor.run_id,
    )


@dataclass(frozen=True, slots=True)
class ScoreSummary:
    job_id: UUID
    quality: str | None
    rows: int
    judges_run: int
    judges_skipped: tuple[str, ...]
    errors: tuple[str, ...]


class ScoringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        events: EventBus,
        definitions: DefinitionStore,
        catalog: Catalog,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.definitions = definitions
        self.catalog = catalog

    async def score_job(
        self,
        deps: Deps,
        job_id: UUID,
        *,
        force: bool = False,
        only: Sequence[str] | None = None,
    ) -> ScoreSummary:
        """Score one job; every step catches and records its own errors, nothing is raised."""
        errors: list[str] = []
        skipped: list[str] = []
        rows = 0
        judges = 0
        quality: str | None = None
        kind = JobKind.NORMAL.value
        try:
            job, defn, app = await pinned_definition(deps, job_id)
            kind = str(job.kind)
            rnd = int(job.round or 1)
        except Exception as exc:
            errors.append(f"definition: {exc}")
            defn = app = None
            rnd = 1

        try:  # 1. objective rows
            policy = None
            if app is not None and getattr(app, "merge_policy", None) is not None:
                policy = str(app.merge_policy)
            async with self.sessions() as s, s.begin():
                obj_rows = compute_signals(await load_job_facts(s, job_id, merge_policy=policy))
                if kind == JobKind.NORMAL.value:
                    obj_rows.append(quality_badge(obj_rows))
                    quality = obj_rows[-1].label
                names = (OBJ_SCORERS | {QUALITY} | {r.scorer for r in obj_rows}) - {OBJ_REVERTED}
                await replace_scores(s, job_id, obj_rows, scorers=names)
                rows += len(obj_rows)
        except Exception as exc:
            errors.append(f"objective: {exc}")
            quality = None

        if defn is not None:  # 2. judges, one after another
            try:
                async with self.sessions() as s:
                    oks = list(
                        (
                            await s.execute(
                                select(AgentRun.role).where(
                                    AgentRun.job_id == job_id,
                                    AgentRun.status == RunStatus.OK.value,
                                )
                            )
                        ).scalars()
                    )
                    judge_ok = await s.scalar(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(
                            AgentRun.job_id == job_id,
                            AgentRun.role == Role.JUDGE.value,
                            AgentRun.status == RunStatus.OK.value,
                        )
                    )
            except Exception as exc:
                errors.append(f"judges: {exc}")
                oks, judge_ok = [], 0
            occurrence = int(judge_ok or 0) + 1
            for name in sorted(defn.scorers):
                scorer = defn.scorers[name]
                if only is not None and name not in only:
                    continue
                if getattr(deps, "roles", None) is None:  # the API process cannot run agents
                    skipped.append(f"{name}: judges run on the worker")
                    continue
                if scorer.applies_to and not any(r in oks for r in scorer.applies_to):
                    skipped.append(f"{name}: not applicable")
                    continue
                wanted = (
                    force
                    or only is not None
                    or sampled(
                        job_id,
                        name,
                        scorer.sampling_rate,
                        benchmark=kind == JobKind.BENCHMARK.value,
                    )
                )
                if not wanted:
                    skipped.append(f"{name}: not sampled")
                    continue
                try:
                    row = await run_judge(
                        deps, job_id, name, scorer, round=rnd, occurrence=occurrence
                    )
                    async with self.sessions() as s, s.begin():
                        await replace_scores(s, job_id, [row], scorers=[name])
                    rows += 1
                    if row.label == "skipped":
                        skipped.append(f"{name}: {row.reason}")
                    else:
                        judges += 1
                except Exception as exc:
                    errors.append(f"judge {name}: {exc}")

        try:  # 4. the event
            await self.events.emit(
                job_id,
                "score.recorded",
                {"quality": quality, "judges": judges, "errors": len(errors)},
            )
        except Exception as exc:
            errors.append(f"event: {exc}")

        if kind == JobKind.BENCHMARK.value:  # 5.
            try:
                await self._after_benchmark(job_id)
            except Exception as exc:
                errors.append(f"benchmark: {exc}")
        return ScoreSummary(job_id, quality, rows, judges, tuple(skipped), tuple(errors))

    async def _after_benchmark(self, job_id: UUID) -> None:
        """Seam for M10.11: a no-op until then."""
        return None
