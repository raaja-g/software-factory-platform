"""Follow-up jobs: prepare adopts the PR head, deliver updates the same PR, /factory rework."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from tests.integration.test_github_commands_db import (  # noqa: F401
    HEAD,
    PR_URL,
    T0,
    comment,
    hc,
    seed,
)
from tests.unit.gitops.test_github_deliver import BRANCH, commit_in, sh, w  # noqa: F401

from swfactory.db import repo as dbrepo
from swfactory.db.tables import Job
from swfactory.definition.models import MergePolicy

pytestmark = pytest.mark.db


async def head(box) -> str:
    return (await box.exec(["git", "rev-parse", "HEAD"])).stdout.strip()


async def follow_up_job(w) -> Job:  # noqa: F811
    """A queued follow-up of w.job (delivered, PR 1 open on BRANCH), a person pushed H2."""
    app = w.app(MergePolicy.PR_MANUAL)
    d = await w.git.deliver(w.job, app, w.box, draft=False, expected_head=w.head)
    assert d.status == "pr_open"
    clone = w.tmp / "person"
    sh(w.tmp, "clone", "-q", str(w.origin), str(clone))
    sh(clone, "checkout", "-q", BRANCH)
    (clone / "BY_PERSON.md").write_text("hi\n")
    sh(clone, "add", "-A")
    sh(clone, "commit", "-q", "-m", "person")
    sh(clone, "push", "-q", "origin", BRANCH)
    w.h2 = sh(clone, "rev-parse", "HEAD")
    w.gh.branches[BRANCH] = w.h2
    w.gh.prs[1]["head_sha"] = w.h2
    new_id = uuid.uuid4()
    async with w.sessions.begin() as s:
        app_id = await s.scalar(select(Job.app_id).where(Job.id == w.job.id))
        s.add(
            Job(
                id=new_id, app_id=app_id, seq=2, slug="follow-up-add-x", title="Follow-up: Add x",
                feature="f", requested_by="me", budget_usd=1, follow_up_of=w.job.id, branch=BRANCH,
            )
        )  # fmt: skip
    return Job(
        id=new_id, seq=2, slug="follow-up-add-x", title="Follow-up: Add x", feature="f",
        follow_up_of=w.job.id, branch=BRANCH,
    )  # fmt: skip


async def test_follow_up_prepare_adopts_the_pr_head(w):  # noqa: F811
    job = await follow_up_job(w)
    app = w.app(MergePolicy.PR_MANUAL)
    box = await w.new_box("fu")
    await w.git.prepare(job, app, box)
    assert await head(box) == w.h2
    async with w.sessions() as s:
        assert (await dbrepo.delivery_state(s, job.id))[0] == w.h2
    cur = (await box.exec(["git", "branch", "--show-current"])).stdout.strip()
    assert cur == BRANCH
    new_head = await commit_in(box, "FU.md")
    before = w.gh.counts["create_pr"]
    d = await w.git.deliver(job, app, box, draft=False, expected_head=new_head)
    assert d.status == "pr_open" and w.gh.counts["create_pr"] == before
    assert d.pr_url and d.pr_url.endswith("/pull/1")
    assert w.gh.branches[BRANCH] == new_head and w.gh.push_leases[-1] == w.h2


async def test_follow_up_fails_when_the_pr_was_merged_meanwhile(w):  # noqa: F811
    job = await follow_up_job(w)
    app = w.app(MergePolicy.PR_MANUAL)
    box = await w.new_box("fu")
    await w.git.prepare(job, app, box)
    w.gh.merge_externally(1)
    new_head = await commit_in(box, "FU.md")
    pushes = w.gh.counts["push"]
    d = await w.git.deliver(job, app, box, draft=False, expected_head=new_head)
    assert (d.status, d.detail) == ("failed", "the PR was merged or closed during the follow-up")
    assert w.gh.counts["push"] == pushes


# -- /factory rework on a delivered PR ---------------------------------------------------


async def delivered(sessions):
    app, gh, job_id = await seed(sessions)
    async with sessions.begin() as s:
        await s.execute(text("update app.jobs set stage='delivered' where id=:j"), {"j": job_id})
        await s.execute(
            text(
                "insert into app.job_events (job_id, at, kind, data) "
                "values (:j, :t, 'job.stage', cast('{\"stage\": \"delivered\"}' as jsonb))"
            ),
            {"j": job_id, "t": T0},
        )
    return app, gh, job_id


async def follow_ups(sessions):
    async with sessions() as s:
        return list((await s.scalars(select(Job).where(Job.follow_up_of.is_not(None)))).all())


async def test_rework_comment_on_delivered_pr_starts_a_follow_up(sessions):
    app, gh, _ = await delivered(sessions)
    p = comment(gh, "/factory rework rename X", id=40, perm="write",
                created_at=T0 + timedelta(minutes=1))  # fmt: skip
    assert await hc(sessions, app, gh, p) is not None
    (fu,) = await follow_ups(sessions)
    assert fu.feature.endswith(f"Follow-up requested by github:ana on {PR_URL}:\nrename X")


async def test_approve_comment_on_delivered_pr_is_refused(sessions):
    app, gh, _ = await delivered(sessions)
    p = comment(gh, "/factory approve", perm="admin", created_at=datetime.now(UTC))
    assert await hc(sessions, app, gh, p) is None
    assert await follow_ups(sessions) == []
    assert "delivered job accepts only" in gh.comments[1][-1].body


async def test_rework_comment_older_than_delivery_is_ignored(sessions):
    app, gh, _ = await delivered(sessions)
    p = comment(gh, "/factory rework x", id=41, perm="write", created_at=T0 - timedelta(minutes=1))
    assert await hc(sessions, app, gh, p) is None
    assert await follow_ups(sessions) == []


async def test_rework_replay_gets_one_refusal_reply(sessions):
    app, gh, _ = await delivered(sessions)
    p = comment(gh, "/factory rework x", id=42, perm="write", created_at=T0 + timedelta(minutes=1))
    assert await hc(sessions, app, gh, p) is not None
    assert await hc(sessions, app, gh, p) is None
    assert len(await follow_ups(sessions)) == 1
