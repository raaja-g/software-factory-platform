"""Async helpers shared by db tests that need an active definition, jobs and backlogs."""

import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from swfactory.backlogs import RequirementItem, import_backlog, refresh_backlog
from swfactory.db.tables import JobEvent
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef
from swfactory.definition.store import DefinitionStore
from swfactory.queue import claim_next_job, enqueue_job


async def activate(sessions, apps):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    defs = {
        k: AppDef.model_validate({**base.model_dump(mode="json"), **v}) for k, v in apps.items()
    }
    fac = r.definition.factory.model_copy(update={"apps": defs})
    sha = "sha-" + "-".join(sorted(apps)) + str(datetime.now(UTC).timestamp())
    r = replace(r, sha=sha, definition=r.definition.model_copy(update={"factory": fac}))
    assert await DefinitionStore(sessions).save(r)
    return sha


async def enq(sessions, app="a", feature="Add a /health endpoint"):
    async with sessions.begin() as s:
        return await enqueue_job(
            s,
            app_slug=app,
            feature=feature,
            title=None,
            source="cli",
            source_ref=None,
            requested_by="me",
        )


async def claim(sessions, w, lease_s=60) -> Any:
    async with sessions() as s:
        return await claim_next_job(s, worker_id=w, lease_s=lease_s)


async def job_events(sessions, kind):
    async with sessions() as s:
        return list(
            (
                await s.scalars(
                    select(JobEvent.data).where(JobEvent.kind == kind).order_by(JobEvent.id)
                )
            ).all()
        )


def items(*ids):
    out = []
    prev: tuple[str, ...] = ()
    for i in ids:
        out.append(RequirementItem(i, f"Item {i}", f"Build {i}.", prev, (f"{i} works",)))
        prev = (i,)
    return out


async def running(sessions, *ids):
    await activate(sessions, apps={"demo": {"concurrency": 3}})
    async with sessions.begin() as s:
        return await import_backlog(
            s, app_slug="demo", title="demo", items=items(*ids), requested_by="ann"
        )


async def put(sessions, job_id, stage):
    async with sessions.begin() as s:
        await s.execute(
            text("UPDATE app.jobs SET stage = :st WHERE id = :i"), {"st": stage, "i": job_id}
        )
        bid = await s.scalar(text("SELECT backlog_id FROM app.jobs WHERE id = :i"), {"i": job_id})
        await refresh_backlog(s, bid)


async def status(sessions, bl):
    async with sessions() as s:
        return await s.scalar(text("SELECT status FROM app.backlogs WHERE id = :i"), {"i": bl.id})
