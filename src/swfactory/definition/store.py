"""Definition store: keeps the last valid definition active and mirrors apps into the database."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import App, Command, DefinitionRow, Job
from swfactory.definition.loader import LoadResult
from swfactory.definition.models import Definition
from swfactory.domain.types import ACTIVE_STAGES, FactoryMode, HumanAction, Stage
from swfactory.events import record_event

_CACHE: dict[str, Definition] = {}
_CANCEL_STAGES = frozenset(
    {s.value for s in ACTIVE_STAGES} | {Stage.NEEDS_HUMAN.value, Stage.FAILED.value}
)


class NoActiveDefinition(RuntimeError):
    pass


def _parse(sha: str, content: object) -> Definition:
    cached = _CACHE.get(sha)
    if cached is None:
        cached = Definition.model_validate(content)
        _CACHE[sha] = cached
    return cached


async def load_active(s: AsyncSession) -> tuple[str, Definition] | None:
    row = await s.scalar(select(DefinitionRow).where(DefinitionRow.active.is_(True)))
    if row is None:
        return None
    return row.sha, _parse(row.sha, row.content)


class DefinitionStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        mode: FactoryMode = FactoryMode.PERSONAL,
    ) -> None:
        self.sessions = sessions
        self.mode = mode

    async def save(self, result: LoadResult, *, activate: bool = True) -> bool:
        defn = result.definition
        valid = defn is not None and not result.errors
        content = defn.model_dump(mode="json", by_alias=True) if defn is not None else {}
        errors = [str(e) for e in result.errors]
        server_dirty = self.mode is FactoryMode.SERVER and result.sha.startswith("dirty-")
        do_activate = bool(valid and activate and not server_dirty)
        async with self.sessions() as s, s.begin():
            stmt = insert(DefinitionRow).values(
                sha=result.sha, source=result.source, content=content, errors=errors
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[DefinitionRow.sha],
                    set_={
                        "source": stmt.excluded.source,
                        "content": stmt.excluded.content,
                        "errors": stmt.excluded.errors,
                    },
                )
            )
            if not do_activate:
                return False
            assert defn is not None
            await s.execute(
                update(DefinitionRow).where(DefinitionRow.active.is_(True)).values(active=False)
            )
            await s.execute(
                update(DefinitionRow).where(DefinitionRow.sha == result.sha).values(active=True)
            )
            _CACHE[result.sha] = defn
            apps = await self.sync_apps(s, defn)
            await record_event(
                s,
                None,
                "definition.loaded",
                {"sha": result.sha, "source": result.source, "apps": apps},
            )
        return True

    async def active(self) -> tuple[str, Definition]:
        async with self.sessions() as s:
            found = await load_active(s)
        if found is None:
            raise NoActiveDefinition("no active definition")
        return found

    async def get(self, sha: str) -> Definition | None:
        async with self.sessions() as s:
            row = await s.get(DefinitionRow, sha)
            if row is None or not row.content:
                return None
            return _parse(sha, row.content)

    async def sync_apps(self, s: AsyncSession, defn: Definition) -> dict[str, str]:
        out: dict[str, str] = {}
        existing = {a.slug: a for a in (await s.scalars(select(App))).all()}
        for slug, d in defn.factory.apps.items():
            want = (d.repo_kind, d.repo_url, d.default_branch)
            a = existing.get(slug)
            if a is None:
                s.add(
                    App(
                        slug=slug,
                        repo_kind=d.repo_kind,
                        repo_url=d.repo_url,
                        default_branch=d.default_branch,
                    )
                )
                out[slug] = "added"
            elif a.archived_at is not None:
                a.repo_kind, a.repo_url, a.default_branch = want
                a.archived_at = None
                out[slug] = "updated"
            elif (a.repo_kind, a.repo_url, a.default_branch) != want:
                a.repo_kind, a.repo_url, a.default_branch = want
                out[slug] = "updated"
            else:
                out[slug] = "unchanged"
        for slug, a in existing.items():
            if slug in defn.factory.apps or a.archived_at is not None:
                continue
            a.archived_at = datetime.now(UTC)
            await s.flush()
            await self._stop_jobs(s, a)
            out[slug] = "archived"
        await s.flush()
        return out

    async def _stop_jobs(self, s: AsyncSession, a: App) -> None:
        jobs = (
            await s.scalars(select(Job).where(Job.app_id == a.id).order_by(Job.created_at))
        ).all()
        for j in jobs:
            if j.stage == Stage.QUEUED.value:
                j.stage = Stage.CANCELLED.value
                j.finished_at = datetime.now(UTC)
                await record_event(
                    s, j.id, "job.stage", {"stage": "cancelled", "reason": "app archived"}
                )
            elif j.stage in _CANCEL_STAGES:
                s.add(
                    Command(job_id=j.id, action=HumanAction.CANCEL.value, requested_by="definition")
                )
        await s.flush()
