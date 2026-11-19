"""report_outcomes: unlabel and comment once per ended job (M8.5b)."""

import pytest
from sqlalchemy import text
from tests.fakes.github import FakeGitHub
from tests.fakes.github_git import DeliveryGitHub, SimulatedCrash
from tests.integration.test_github_intake_db import (
    activate,
    finish,
    gh_with,
    run_intake,
)

from swfactory.github.intake import report_outcomes

pytestmark = pytest.mark.db


async def intake_row(s, repo, number):
    return (
        await s.execute(
            text(
                "select reported_at, job_id from app.issue_intakes "
                "where repo = :r and issue_number = :n"
            ),
            {"r": repo, "n": number},
        )
    ).one()


def markers(gh: FakeGitHub, number: int, marker: str):
    return [c for c in gh.comments.get(number, []) if marker in c.body]


@pytest.fixture
async def world(sessions):
    app = await activate(sessions)
    gh = gh_with()
    gh.__class__ = DeliveryGitHub
    DeliveryGitHub.__init__(gh)  # type: ignore[misc]
    gh.permissions.update({"ana": "write"})
    from tests.integration.test_github_intake_db import T1

    gh.label_events[12] = [("labeled", "factory", "ana", T1)]
    gh.labels[12] = {"factory"}
    job_id = await run_intake(sessions, gh, app)
    assert job_id is not None
    return gh, job_id


async def test_ended_job_unlabels_and_comments_once(sessions, world):
    gh, job = world
    await finish(sessions, "acme/web#12", "merged")
    async with sessions() as s:
        assert await report_outcomes(s, lambda repo: gh) == 1  # type: ignore[arg-type,return-value]
        assert await report_outcomes(s, lambda repo: gh) == 0  # type: ignore[arg-type,return-value]
    assert "factory" not in gh.labels[12]
    (c,) = markers(gh, 12, f"factory:outcome:{job}")
    assert "demo-gh:001 was merged" in c.body
    async with sessions() as s:
        assert (await intake_row(s, "acme/web", 12)).reported_at is not None


async def test_running_job_is_not_reported(sessions, world):
    gh, job = world
    await finish(sessions, "acme/web#12", "build")
    async with sessions() as s:
        assert await report_outcomes(s, lambda repo: gh) == 0  # type: ignore[arg-type,return-value]
    assert "factory" in gh.labels[12] and gh.comments.get(12, []) == []


async def test_expired_job_says_how_long_it_waited(sessions, world):
    gh, job = world
    await finish(sessions, "acme/web#12", "closed")
    async with sessions.begin() as s:
        await s.execute(
            text(
                "insert into app.job_events (job_id, kind, data) "
                "values (:j, 'job.expired', cast(:d as jsonb))"
            ),
            {"j": job, "d": '{"stage": "needs_human", "waited_days": 14}'},
        )
    async with sessions() as s:
        assert await report_outcomes(s, lambda repo: gh) == 1  # type: ignore[arg-type,return-value]
    assert "was closed after waiting 14 days for a person" in gh.comments[12][0].body


async def test_crash_after_the_comment_posts_it_once(sessions, world):
    gh, job = world
    assert isinstance(gh, DeliveryGitHub)
    await finish(sessions, "acme/web#12", "merged")
    gh.crash_once_after("comment_once")
    async with sessions() as s:
        with pytest.raises(SimulatedCrash):
            await report_outcomes(s, lambda repo: gh)  # type: ignore[arg-type,return-value]
    async with sessions() as s:
        await report_outcomes(s, lambda repo: gh)  # type: ignore[arg-type,return-value]
    assert len(markers(gh, 12, f"factory:outcome:{job}")) == 1
    async with sessions() as s:
        assert (await intake_row(s, "acme/web", 12)).reported_at is not None
