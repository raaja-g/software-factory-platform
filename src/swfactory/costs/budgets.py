"""Job, daily and app-monthly budgets, with run-level reservations."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.costs.pricing import STEP_MIN_USD
from swfactory.db import repo
from swfactory.db.tables import AgentRun, App, BenchResult, BenchRun, BudgetReservation, Job
from swfactory.definition.models import Definition
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import JobKind, Role

Scope = Literal["job", "daily", "app_month", "run", "bench_run"]
_ZERO = Decimal(0)


def utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetExceeded(Exception):
    gate_reason = "budget"

    def __init__(
        self, scope: Scope, remaining: Decimal, needed: Decimal, *, reserved: Decimal = _ZERO
    ) -> None:
        super().__init__(
            f"{scope} budget: {remaining} left, {needed} needed (open reservations: {reserved})"
        )
        self.scope = scope
        self.remaining = remaining
        self.needed = needed
        self.reserved = reserved


def _day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    return _day_start(now).replace(day=1)


class _Scopes:
    """The remainders of one job's scopes, each net of open reservations."""

    def __init__(self) -> None:
        # scope -> (remaining, reserved)
        self.items: list[tuple[Scope, Decimal, Decimal]] = []

    def add(self, scope: Scope, cap: Decimal, spent: Decimal, reserved: Decimal) -> None:
        self.items.append((scope, max(_ZERO, cap - spent - reserved), reserved))

    def smallest(self) -> tuple[Scope, Decimal, Decimal]:
        return min(self.items, key=lambda i: i[1])


class BudgetService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        definitions: DefinitionStore,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.definitions = definitions
        self.clock = clock

    async def _job(self, s: AsyncSession, job_id: UUID) -> tuple[Job, str | None]:
        row = (
            await s.execute(
                select(Job, App.slug).outerjoin(App, App.id == Job.app_id).where(Job.id == job_id)
            )
        ).one()
        return row[0], row[1]

    async def _scopes(
        self,
        s: AsyncSession,
        defn: Definition,
        job: Job,
        slug: str | None,
        *,
        only_job: bool = False,
    ) -> _Scopes:
        now = self.clock()
        out = _Scopes()
        out.add(
            "job",
            job.budget_usd,
            await repo.spend_for_job(s, job.id),
            await repo.open_reservations(s, job_id=job.id),
        )
        if job.kind == JobKind.BENCHMARK.value:
            await self._add_bench_run(s, out, job)
            return out
        if only_job:
            return out
        day = _day_start(now)
        out.add(
            "daily",
            defn.factory.budgets.daily_usd,
            await repo.spend_since(s, day, "benchmark"),
            await repo.open_reservations(s, since=day, exclude_kind="benchmark"),
        )
        cap = defn.factory.budgets.app_monthly_usd.get(slug) if slug else None
        if slug is not None and cap is not None:
            month = _month_start(now)
            out.add(
                "app_month",
                cap,
                await repo.spend_for_app_since(s, slug, month),
                await repo.open_reservations(s, app_slug=slug, since=month),
            )
        return out

    async def _add_bench_run(self, s: AsyncSession, out: _Scopes, job: Job) -> None:
        """The run's own cap: its budget_usd less every spend of its jobs, judges included."""
        run = await s.scalar(
            select(BenchRun)
            .join(BenchResult, BenchResult.run_id == BenchRun.id)
            .where(BenchResult.job_id == job.id)
        )
        if run is None or run.budget_usd is None:
            return
        like = f"bench:{run.id}:%"
        spent = await s.scalar(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0))
            .join(Job, Job.id == AgentRun.job_id)
            .where(Job.kind == JobKind.BENCHMARK.value, Job.source_ref.like(like))
        )
        held = await s.scalar(
            select(func.coalesce(func.sum(BudgetReservation.amount), 0))
            .join(Job, Job.id == BudgetReservation.job_id)
            .where(
                BudgetReservation.released_at.is_(None),
                Job.kind == JobKind.BENCHMARK.value,
                Job.source_ref.like(like),
            )
        )
        out.add("bench_run", run.budget_usd, Decimal(spent or 0), Decimal(held or 0))

    async def remaining_for_job(self, job_id: UUID) -> Decimal:
        _, defn = await self.definitions.active()
        async with self.sessions() as s:
            job, slug = await self._job(s, job_id)
            return (await self._scopes(s, defn, job, slug, only_job=True)).items[0][1]

    async def remaining_today(self) -> Decimal:
        _, defn = await self.definitions.active()
        day = _day_start(self.clock())
        async with self.sessions() as s:
            spent = await repo.spend_since(s, day, "benchmark")
            held = await repo.open_reservations(s, since=day, exclude_kind="benchmark")
        return max(_ZERO, defn.factory.budgets.daily_usd - spent - held)

    async def remaining_for_app_month(self, app_slug: str) -> Decimal | None:
        _, defn = await self.definitions.active()
        cap = defn.factory.budgets.app_monthly_usd.get(app_slug)
        if cap is None:
            return None
        month = _month_start(self.clock())
        async with self.sessions() as s:
            spent = await repo.spend_for_app_since(s, app_slug, month)
            held = await repo.open_reservations(s, app_slug=app_slug, since=month)
        return max(_ZERO, cap - spent - held)

    async def check_before_step(self, job_id: UUID, role: Role) -> None:
        _, defn = await self.definitions.active()
        need = STEP_MIN_USD[role]
        async with self.sessions() as s:
            job, slug = await self._job(s, job_id)
            scopes = await self._scopes(s, defn, job, slug)
        for scope, remaining, reserved in scopes.items:
            if remaining < need:
                raise BudgetExceeded(scope, remaining, need, reserved=reserved)

    async def reserve_run(
        self,
        job_id: UUID,
        *,
        run_id: UUID,
        role: Role,
        round: int,
        fan_out: int = 1,
        s: AsyncSession | None = None,
    ) -> Decimal:
        _, defn = await self.definitions.active()
        if s is not None:
            return await self._reserve(s, defn, job_id, run_id, role, round, fan_out)
        async with self.sessions() as own, own.begin():
            return await self._reserve(own, defn, job_id, run_id, role, round, fan_out)

    async def _reserve(
        self,
        s: AsyncSession,
        defn: Definition,
        job_id: UUID,
        run_id: UUID,
        role: Role,
        round: int,
        fan_out: int,
    ) -> Decimal:
        now = self.clock()
        day, month = _day_start(now), _month_start(now)
        _, slug = await self._job(s, job_id)
        # fixed lock order: day, app month, job
        await s.execute(
            text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"budget-day:{day.date()}"}
        )
        if slug is not None and slug in defn.factory.budgets.app_monthly_usd:
            await s.execute(
                text("select pg_advisory_xact_lock(hashtext(:k))"),
                {"k": f"budget-month:{slug}:{month.date()}"},
            )
        job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        scopes = await self._scopes(s, defn, job, slug)
        scope, remaining, reserved = scopes.smallest()
        need = STEP_MIN_USD[role]
        if remaining < need:
            raise BudgetExceeded(scope, remaining, need, reserved=reserved)
        others = 0
        if role.value.startswith("reviewer-"):
            others = (
                await s.scalar(
                    select(func.count(func.distinct(BudgetReservation.role))).where(
                        BudgetReservation.job_id == job_id,
                        BudgetReservation.round == round,
                        BudgetReservation.role.like("reviewer-%"),
                        BudgetReservation.role != role.value,
                    )
                )
            ) or 0
        share = remaining / max(1, fan_out - others)
        share = min(share, defn.factory.budgets.step_max_usd[role.value])
        share = share.quantize(Decimal("0.000001"))
        if share < need:
            raise BudgetExceeded("run", share, need, reserved=reserved)
        s.add(
            BudgetReservation(
                run_id=run_id,
                job_id=job_id,
                app_id=job.app_id,
                role=role.value,
                round=round,
                day=day.date(),
                month=month.date(),
                amount=share,
                created_at=now,
            )
        )
        await s.flush()
        return share

    async def release_run(self, run_id: UUID, actual_usd: Decimal) -> None:
        async with self.sessions() as s, s.begin():
            await s.execute(
                text(
                    "update app.budget_reservations set released_at = now(), actual_usd = :a "
                    "where run_id = :r and released_at is null"
                ),
                {"a": actual_usd, "r": run_id},
            )


__all__ = ["BudgetExceeded", "BudgetService", "utcnow"]
