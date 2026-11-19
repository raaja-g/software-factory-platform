import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text, update
from tests.support.db_helpers import activate, enq
from tests.unit.api.api_support import settings

from swfactory.db.tables import Job, JobEvent
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.events import EventBus
from swfactory.notify import notify_once
from swfactory.watchdog import watchdog_tick

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)
SETTINGS = settings()


def _defn():
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    return r.definition


DEFN = _defn()


class Rec:
    def __init__(self):
        self.sent: list[tuple[str, str, str | None]] = []

    async def send(self, kind, text, *, link=None):
        self.sent.append((kind, text, link))


@pytest.fixture(autouse=True)
def _disk(monkeypatch):
    monkeypatch.setattr("swfactory.watchdog.disk_usage_pct", lambda paths: (10, "/"))


@pytest.fixture
async def s(sessions):
    async with sessions() as x:
        yield x


@pytest.fixture
def bus(sessions):
    return EventBus(sessions, "")


@pytest.fixture
def rec():
    return Rec()


@pytest.fixture
def kinds(rec):
    return lambda: [k for k, _, _ in rec.sent]


@pytest.fixture
def heartbeat_at(sessions):
    async def go(at):
        async with sessions.begin() as x:
            await x.execute(
                text(
                    "insert into app.worker_heartbeats (worker_id, at, jobs, version) "
                    "values ('w1', :at, 0, 'x') on conflict (worker_id) do update set at = :at"
                ),
                {"at": at},
            )

    return go


@pytest.fixture
def bare_tick(s, bus, rec):
    async def go(at):
        return await watchdog_tick(s, bus, rec, defn=DEFN, settings=SETTINGS, now=at)

    return go


@pytest.fixture
def tick(bare_tick, heartbeat_at):
    async def go(at):
        await heartbeat_at(at)
        return await bare_tick(at)

    return go


@pytest.fixture
async def job(sessions):
    await activate(sessions, apps={"demo": {}})
    return (await enq(sessions, app="demo")).id


@pytest.fixture
async def waiting_job(sessions, job):
    async with sessions.begin() as x:
        await x.execute(update(Job).where(Job.id == job).values(stage="needs_human"))
        await x.execute(text("delete from app.job_events where job_id = :j"), {"j": job})
        x.add(JobEvent(job_id=job, kind="human.waiting", data={}, at=NOW))
    return job


@pytest.fixture
async def build_job_quiet_40m(sessions, job):
    async with sessions.begin() as x:
        await x.execute(update(Job).where(Job.id == job).values(stage="build"))
        await x.execute(text("delete from app.job_events where job_id = :j"), {"j": job})
        x.add(JobEvent(job_id=job, kind="run.started", data={}, at=NOW - timedelta(minutes=40)))
    return job


@pytest.fixture
def spend_today(sessions, job):
    n = iter(range(1, 100))

    async def go(usd):
        async with sessions.begin() as x:
            await x.execute(
                text(
                    "insert into app.agent_runs (id, job_id, role, attempt, engine, status, "
                    "cost_usd, started_at) values (gen_random_uuid(), :j, 'builder', :n, "
                    "'fake', 'ok', :c, now())"
                ),
                {"j": job, "c": usd, "n": next(n)},
            )

    return go


@pytest.fixture
def model_row(sessions):
    async def go(alias, *, available, error_class, last_probe_at):
        async with sessions.begin() as x:
            await x.execute(
                text(
                    "insert into app.models (alias, provider, upstream_id, available, "
                    "error_class, last_probe_at) values (:a, 'vertex', 'u', :v, :e, :t)"
                ),
                {"a": alias, "v": available, "e": error_class, "t": last_probe_at},
            )

    return go


async def count_events(s, job, kind):
    return await s.scalar(
        text("select count(*) from app.job_events where job_id = :j and kind = :k"),
        {"j": job, "k": kind},
    )


async def test_notify_once_per_key(s, rec):
    assert await notify_once(s, rec, key="k1", kind="job.failed", text="x") is True  # type: ignore[arg-type]
    assert await notify_once(s, rec, key="k1", kind="job.failed", text="x") is False  # type: ignore[arg-type]
    assert rec.sent == [("job.failed", "x", None)]


async def test_event_kinds_notified_once(s, bus, rec, job, tick, kinds):
    await bus.emit(job, "job.failed", {"error": "boom"})
    await tick(NOW)
    await tick(NOW + timedelta(minutes=1))
    assert kinds() == ["job.failed"] and "boom" in rec.sent[0][1]
    assert (rec.sent[0][2] or "").endswith(f"/#/job/{job}")


async def test_tick_runs_the_stuck_detector(s, bus, rec, build_job_quiet_40m, tick, kinds):
    await tick(NOW)
    assert await count_events(s, build_job_quiet_40m, "job.stuck") == 1
    assert kinds() == ["job.stuck"]


async def test_waiting_reminders(s, bus, rec, waiting_job, tick, kinds):
    for h in (3, 4, 5, 28, 29):
        await tick(NOW + timedelta(hours=h))
    assert kinds() == ["human.waiting", "human.waiting"]


async def test_expiry_warned_two_days_before(s, bus, rec, waiting_job, tick, kinds):
    await tick(NOW + timedelta(days=11, hours=23))
    assert "job.expiring" not in kinds()
    await tick(NOW + timedelta(days=12))
    await tick(NOW + timedelta(days=12, hours=1))
    assert kinds().count("job.expiring") == 1


async def test_no_live_worker_for_five_minutes(s, bus, rec, heartbeat_at, bare_tick, kinds):
    await heartbeat_at(NOW)
    await bare_tick(NOW + timedelta(minutes=4))
    assert kinds() == []
    await bare_tick(NOW + timedelta(minutes=6))
    await bare_tick(NOW + timedelta(minutes=9))
    assert kinds() == ["workers.none"]


async def test_daily_budget_thresholds(s, bus, rec, spend_today, tick, kinds):
    await spend_today(119)
    await tick(NOW)
    assert kinds() == []
    await spend_today(2)
    await tick(NOW)
    await spend_today(30)
    await tick(NOW)
    await tick(NOW)
    assert [t for k, t, _ in rec.sent if k == "budget.daily"] == [
        "80% of today's $150 budget used",
        "100% of today's $150 budget used",
    ]


async def test_failed_probe_notified_once_a_day(s, bus, rec, model_row, tick):
    await model_row(
        "gemini-pro@vertex", available=False, error_class="not_enabled", last_probe_at=NOW
    )
    await tick(NOW)
    await tick(NOW + timedelta(hours=1))
    await tick(NOW + timedelta(days=1))
    assert [t for k, t, _ in rec.sent if k == "model.unavailable"] == [
        "gemini-pro@vertex unavailable: not_enabled"
    ] * 2


async def test_disk_above_high_water_once_a_day(s, bus, rec, monkeypatch, tick, kinds):
    monkeypatch.setattr("swfactory.watchdog.disk_usage_pct", lambda paths: (91, "/"))
    await tick(NOW)
    await tick(NOW + timedelta(minutes=1))
    assert kinds() == ["disk.high"] and rec.sent[0][1] == "/ at 91% (high-water 85%)"
