from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from tests.integration.db.test_queue import activate as activate_apps
from tests.integration.db.test_queue import claim, enq
from tests.integration.test_github_intake_db import activate as activate_gh

from swfactory.commands import CommandRefused
from swfactory.db.tables import IssueIntake, Job, JobEvent
from swfactory.github.followup import start_follow_up
from swfactory.github.intake import mark_merged
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db

PR = "https://github.com/acme/web/pull/1"
BASE = "b" * 40


async def make_job(sessions, *, stage, app="demo-gh", source_ref="acme/web#12"):
    async with sessions.begin() as s:
        job = await enqueue_job(
            s, app_slug=app, feature="Add X\nplease", title="Add X", source="github_issue",
            source_ref=source_ref, requested_by="github:ana",
        )  # fmt: skip
        await s.execute(
            text(
                "update app.jobs set stage=:st, pr_url=:u, base_sha=:b, branch='factory/001-add-x' "
                "where id=:j"
            ),
            {"st": stage, "u": PR, "b": BASE, "j": job.id},
        )
        return job.id


async def get_job(sessions, job_id):
    async with sessions() as s:
        return await s.get(Job, job_id, populate_existing=True)


async def events_of(sessions, job_id, kind):
    async with sessions() as s:
        rows = await s.scalars(
            select(JobEvent.data).where(JobEvent.job_id == job_id, JobEvent.kind == kind)
        )
        return list(rows)


async def test_follow_up_starts_a_job_on_the_pr_branch(sessions):
    await activate_gh(sessions)
    old = await make_job(sessions, stage="delivered")
    async with sessions.begin() as s:
        s.add(
            IssueIntake(
                repo="acme/web", issue_number=12, labeled_at=datetime.now(UTC), labeled_by="ana",
                author="ana", author_permission="write", labeler_permission="write", job_id=old,
            )
        )  # fmt: skip
    async with sessions.begin() as s:
        new = await start_follow_up(s, job_id=old, note="Rename X to Y", requested_by="ann@x.io")
    assert (new.branch, new.base_sha, new.follow_up_of, new.source_ref) == (
        "factory/001-add-x", BASE, old, "acme/web#12",
    )  # fmt: skip
    assert new.feature.splitlines()[-2:] == [
        f"Follow-up requested by ann@x.io on {PR}:",
        "Rename X to Y",
    ]
    assert new.title == "Follow-up: Add X" and new.stage == "queued"
    assert (await get_job(sessions, old)).stage == "closed"
    assert await events_of(sessions, old, "job.superseded") == [{"by": str(new.id)}]
    (act,) = await events_of(sessions, old, "human.action")
    assert act["action"] == "follow_up" and act["by"] == "ann@x.io"
    async with sessions() as s:
        ids = list(await s.scalars(select(IssueIntake.job_id)))
    assert ids == [new.id]


@pytest.mark.parametrize("stage", ["needs_human", "merged", "queued"])
async def test_follow_up_refused_unless_delivered(sessions, stage):
    await activate_gh(sessions)
    job = await make_job(sessions, stage=stage)
    with pytest.raises(CommandRefused, match="only a delivered job"):
        async with sessions.begin() as s:
            await start_follow_up(s, job_id=job, note="x", requested_by="ann@x.io")


async def test_follow_up_refused_twice(sessions):
    await activate_gh(sessions)
    job = await make_job(sessions, stage="delivered")
    async with sessions.begin() as s:
        await start_follow_up(s, job_id=job, note="x", requested_by="ann@x.io")
    with pytest.raises(CommandRefused):
        async with sessions.begin() as s:
            await start_follow_up(s, job_id=job, note="y", requested_by="ann@x.io")


async def test_branch_without_follow_up_of_is_rejected(sessions):
    await activate_gh(sessions)
    for branch, base in (("b", None), (None, BASE)):
        with pytest.raises(ValueError, match="only for benchmark jobs"):
            async with sessions.begin() as s:
                await enqueue_job(
                    s, app_slug="demo-gh", feature="f", title=None, source="cli",
                    source_ref=None, requested_by="me", branch=branch, base_sha=base,
                )  # fmt: skip


async def deliver_one(sessions, app):
    job = await enq(sessions, app=app, feature="f1")
    async with sessions.begin() as s:
        await s.execute(
            text("update app.jobs set stage='delivered', pr_url=:u where id=:j"),
            {"u": PR, "j": job.id},
        )
    return job


async def test_hold_slot_while_pr_open(sessions):
    await activate_apps(
        sessions, apps={"a": {"repo_kind": "github", "hold_slot_while_pr_open": True}}
    )
    first = await deliver_one(sessions, "a")
    await enq(sessions, app="a", feature="f2")
    assert await claim(sessions, "w1") is None
    async with sessions.begin() as s:
        assert await mark_merged(s, pr_url=PR, merge_sha="m" * 40, head_sha="h" * 40)
    assert (await claim(sessions, "w1")).seq == 2
    assert (await get_job(sessions, first.id)).stage == "merged"


async def test_delivered_job_frees_the_slot_by_default(sessions):
    await activate_apps(sessions, apps={"a": {"repo_kind": "github"}})
    await deliver_one(sessions, "a")
    await enq(sessions, app="a", feature="f2")
    assert (await claim(sessions, "w1")).seq == 2
