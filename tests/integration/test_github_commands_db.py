from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from tests.fakes.github import FakeGitHub
from tests.integration.test_github_intake_db import activate, make_poller

from swfactory.db.tables import Command
from swfactory.definition.models import AppDef
from swfactory.github.checks import CHECK_NAME
from swfactory.github.commands import comment_payload, handle_comment
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db

PR_URL = "https://github.com/acme/web/pull/1"
HEAD = "a" * 40
T0 = datetime.now(UTC) - timedelta(hours=1)


async def seed(sessions, *, waiting_head=HEAD, waiting_at=T0):
    app = await activate(sessions)
    async with sessions.begin() as s:
        job = await enqueue_job(
            s, app_slug="demo-gh", feature="x", title=None, source="cli",
            source_ref=None, requested_by="me",
        )  # fmt: skip
        await s.execute(
            text("update app.jobs set stage='needs_human', pr_url=:u where id=:j"),
            {"u": PR_URL, "j": job.id},
        )
        await s.execute(
            text(
                "insert into app.job_events (job_id, at, kind, data) "
                "values (:j, :t, 'human.waiting', cast(:d as jsonb))"
            ),
            {
                "j": job.id,
                "t": waiting_at,
                "d": f'{{"reason": "gate", "head_sha": "{waiting_head}"}}',
            },
        )
    gh = FakeGitHub()
    gh.prs[1] = dict(
        head="f/1", base="main", title="t", body="", draft=False, state="open",
        merged=False, merge_sha=None, head_sha=HEAD,
    )  # fmt: skip
    gh.check_runs[(HEAD, CHECK_NAME)] = {"id": 1}
    return app, gh, job.id


def comment(gh, body, *, id=9, login="ana", perm="write", created_at=None, action="created",
            sender_type="User"):  # fmt: skip
    c = gh.add_comment(1, body, login=login, id=id, perm=perm,
                       created_at=created_at or datetime.now(UTC))  # fmt: skip
    p = comment_payload(c, repo="acme/web", pr_url=PR_URL)
    p["action"] = action
    p["sender"]["type"] = sender_type
    return p


async def hc(sessions, app, gh, payload):
    async with sessions.begin() as s:
        return await handle_comment(s, gh, app=app, payload=payload)  # pyright: ignore[reportArgumentType]


async def cmds(sessions):
    async with sessions() as s:
        return list((await s.scalars(select(Command))).all())


async def test_rejections(sessions):
    app, gh, _ = await seed(sessions)
    assert (
        await hc(sessions, app, gh, comment(gh, "```\n/factory approve\n```", id=1, perm="admin"))
        is None
    )
    assert (
        await hc(
            sessions,
            app,
            gh,
            comment(gh, "/factory approve", id=2, sender_type="Bot", perm="admin"),
        )
        is None
    )
    assert (
        await hc(
            sessions, app, gh, comment(gh, "/factory approve", id=3, action="edited", perm="admin")
        )
        is None
    )
    assert await hc(sessions, app, gh, comment(gh, "/factory reject", id=4, perm="read")) is None
    old = comment(gh, "/factory approve", id=5, perm="admin", created_at=T0 - timedelta(seconds=60))
    assert await hc(sessions, app, gh, old) is None
    p = comment(gh, "/factory approve", id=6, login="ana", perm="admin")
    gh.comments[1][-1] = type(gh.comments[1][-1])(
        6, "mallory", "User", "/factory approve", gh.comments[1][-1].created_at, None, 1
    )
    assert await hc(sessions, app, gh, p) is None
    assert await cmds(sessions) == []


async def test_replay_and_row_fields(sessions):
    app, gh, _ = await seed(sessions)
    p = comment(gh, "/factory approve", perm="admin")
    assert await hc(sessions, app, gh, p) is not None
    assert await hc(sessions, app, gh, p) is None
    (c,) = await cmds(sessions)
    assert c.requested_by == "github:ana" and c.head_sha == HEAD and c.source_ref == "gh-comment:9"


async def test_permissions(sessions):
    app, gh, _ = await seed(sessions)
    assert await hc(sessions, app, gh, comment(gh, "/factory approve", id=1, perm="write")) is None
    raw = app.model_dump()
    raw["github"]["approvers"] = ["ana"]
    app2 = AppDef.model_validate(raw)
    assert (
        await hc(sessions, app2, gh, comment(gh, "/factory approve", id=2, perm="write"))
        is not None
    )


async def test_rework_needs_write_and_is_fenced(sessions):
    app, gh, _ = await seed(sessions)
    assert (
        await hc(sessions, app, gh, comment(gh, "/factory rework add x", perm="write")) is not None
    )
    (c,) = await cmds(sessions)
    assert c.note is not None and "UNTRUSTED DATA" in c.note and "add x" in c.note


async def test_approve_refused_when_head_changed(sessions):
    app, gh, _ = await seed(sessions)
    gh.prs[1]["head_sha"] = "b" * 40
    assert await hc(sessions, app, gh, comment(gh, "/factory approve", perm="admin")) is None
    assert gh.comments[1][-1].body.startswith("Not approved: the branch changed since the review")


async def test_approve_for_moved_head(sessions):
    app, gh, _ = await seed(sessions, waiting_head="b" * 40)
    assert await hc(sessions, app, gh, comment(gh, "/factory approve", perm="admin")) is None
    assert await cmds(sessions) == []
    assert "Not done" in gh.comments[1][-1].body


async def poller_for(sessions, gh):
    poller, _ = await make_poller(sessions, gh)
    return poller


async def test_poller_runs_missed_comment_once_and_ignores_edits(sessions):
    app, gh, _ = await seed(sessions)
    poller, _ = await make_poller(sessions, gh)  # activates again; job seed stays
    now = datetime.now(UTC)
    gh.add_comment(1, "/factory approve", id=31, login="ana", perm="admin", created_at=now)
    gh.add_comment(1, "/factory approve", id=32, login="ana", perm="admin",
                   created_at=now, updated_at=now + timedelta(seconds=50))  # fmt: skip
    await poller.tick()
    assert [c.source_ref for c in await cmds(sessions)] == ["gh-comment:31"]
    await poller.tick()
    assert len(await cmds(sessions)) == 1
    assert gh.calls["list_issue_comments"] >= 1


async def test_poller_one_comment_call_per_repo(sessions):
    app, gh, _ = await seed(sessions)
    poller, _ = await make_poller(sessions, gh)
    gh.calls.clear()
    await poller.tick()
    assert gh.calls["list_issue_comments"] == 1
