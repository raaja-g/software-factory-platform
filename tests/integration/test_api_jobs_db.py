import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text, update
from tests.support.db_helpers import activate, enq
from tests.unit.api.api_support import FakeKeys, server

from swfactory.api.app import ApiDeps, create_app
from swfactory.api.auth import hash_token
from swfactory.artifacts import ArtifactStore
from swfactory.commands import CommandRefused, submit_command
from swfactory.db.tables import Job
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import HumanAction, JobKind
from swfactory.events import EventBus, record_event
from swfactory.graph.build import GRAPH_VERSION
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db

WAITING_SHA = "a" * 40
EMAILS = ("ap@x.io", "sub@x.io", "admin@x.io")
ROLES = {"ap@x.io": "approver", "sub@x.io": "submitter", "admin@x.io": "admin"}


class Users:
    async def role_of(self, email):
        return ROLES.get(email)

    async def token_email(self, token_hash):
        for e in EMAILS:
            if hash_token("sfp_" + e) == token_hash:
                return e
        return None


class Api:
    def __init__(self, sessions, client, artifacts):
        self.sessions = sessions
        self.client = client
        self.artifacts = artifacts
        self.session = None

    @staticmethod
    def _h(as_email):
        return {"authorization": f"Bearer sfp_{as_email}"}

    async def get(self, url, as_email="ap@x.io"):
        return await self.client.get(url, headers=self._h(as_email))

    async def post_action(self, job_id, action, *, as_email="ap@x.io", **body):
        return await self.client.post(
            f"/api/jobs/{job_id}/actions",
            json={"action": action, **body},
            headers=self._h(as_email),
        )

    async def create(self, **body):
        r = await self.client.post(
            "/api/jobs", json={"app": "demo", "feature": "Add a thing", **body},
            headers=self._h("sub@x.io"),
        )  # fmt: skip
        assert r.status_code in (200, 201), r.text
        return r.json()

    async def one(self, sql, *params):
        async with self.sessions() as s:
            row = (await s.execute(text(_pf(sql)), _named(params))).first()
        return None if row is None else tuple(row)

    async def all(self, sql, *params):
        async with self.sessions() as s:
            rows = (await s.execute(text(_pf(sql)), _named(params))).all()
        return [tuple(r) for r in rows]

    async def count_jobs(self, *, source_ref):
        async with self.sessions() as s:
            return int(
                await s.scalar(
                    text("select count(*) from app.jobs where source_ref=:r"), {"r": source_ref}
                )
                or 0
            )

    async def last_event_kind(self, job_id):
        async with self.sessions() as s:
            return await s.scalar(
                text("select kind from app.job_events where job_id=:j order by id desc limit 1"),
                {"j": job_id},
            )

    async def latest_waiting_id(self, job_id):
        async with self.sessions() as s:
            return await s.scalar(
                text(
                    "select id from app.job_events where job_id=:j and kind='human.waiting' "
                    "order by id desc limit 1"
                ),
                {"j": job_id},
            )


def _pf(sql):  # "%s" placeholders -> named parameters
    out = sql.split("%s")
    return "".join(p + (f":p{i}" if i < len(out) - 1 else "") for i, p in enumerate(out))


def _named(params):
    return {f"p{i}": v for i, v in enumerate(params)}


@pytest.fixture
async def api_db(sessions, tmp_path):
    await activate(sessions, apps={"demo": {}})
    s = server()
    deps = ApiDeps(
        sessions=sessions,
        events=EventBus(sessions, ""),
        artifacts=ArtifactStore(sessions, tmp_path),
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=DefinitionStore(sessions),
        users=Users(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=s,
    )
    app = create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
        api = Api(sessions, c, deps.artifacts)
        async with sessions() as s2:
            api.session = s2
            yield api


async def _set(sessions, job_id, **values):
    async with sessions.begin() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(**values))


async def _waiting(sessions, job_id, **data):
    base = {
        "reason": "approver escalated",
        "retry_node": "verify",
        "artifacts": [],
        "head_sha": WAITING_SHA,
    }
    async with sessions.begin() as s:
        await record_event(s, job_id, "human.waiting", {**base, **data})


@pytest.fixture
async def needs_human_job(sessions, api_db):
    j = await enq(sessions, app="demo")
    await _set(sessions, j.id, stage="needs_human")
    await _waiting(sessions, j.id)
    return j.id


@pytest.fixture
async def escalated_job(sessions, api_db):
    j = await enq(sessions, app="demo")
    await _set(sessions, j.id, stage="needs_human")
    await _waiting(sessions, j.id, retry_node=None)
    return j.id


@pytest.fixture
async def building_job(sessions, api_db):
    j = await enq(sessions, app="demo")
    await _set(sessions, j.id, stage="build")
    return j.id


@pytest.fixture
async def failed_job(sessions, api_db):
    j = await enq(sessions, app="demo")
    await _set(sessions, j.id, stage="failed", graph_version=str(GRAPH_VERSION))
    return j.id


@pytest.fixture
async def old_failed_job(sessions, api_db):
    j = await enq(sessions, app="demo")
    await _set(sessions, j.id, stage="failed", graph_version=str(GRAPH_VERSION - 1))
    return j.id


@pytest.fixture
async def one_of_each_kind(sessions, api_db):
    ids = {}
    for kind in (JobKind.NORMAL, JobKind.BENCHMARK, JobKind.IMPROVER):
        async with sessions.begin() as s:
            j = await enqueue_job(
                s, app_slug="demo", feature="f", title=None, source="cli", source_ref=None,
                requested_by="me", kind=kind,
            )  # fmt: skip
        ids[kind.value] = j.id
    return SimpleNamespace(normal=ids["normal"])


async def test_approve_records_command_with_requested_by_and_head(api_db, needs_human_job):
    r = await api_db.post_action(
        needs_human_job, "approve", as_email="ap@x.io", head_sha=WAITING_SHA
    )
    assert r.status_code == 202, r.text
    row = await api_db.one(
        "select action, requested_by, head_sha from app.commands where job_id=%s", needs_human_job
    )
    assert row == ("approve", "ap@x.io", WAITING_SHA)
    assert await api_db.last_event_kind(needs_human_job) == "human.action"


async def test_approve_without_head_is_pinned_to_the_waiting_one(api_db, needs_human_job):
    assert (await api_db.post_action(needs_human_job, "approve")).status_code == 202
    row = await api_db.one("select head_sha from app.commands where job_id=%s", needs_human_job)
    assert row == (WAITING_SHA,)


async def test_approve_refused_when_head_moved(api_db, needs_human_job):
    r = await api_db.post_action(needs_human_job, "approve", head_sha="b" * 40)
    assert r.status_code == 409 and r.json()["detail"] == "the branch moved; reload"


async def test_submit_command_itself_refuses_moved_head(api_db, needs_human_job):
    async with api_db.sessions.begin() as s:
        with pytest.raises(CommandRefused, match="the branch moved"):
            await submit_command(
                s, job_id=needs_human_job, action=HumanAction.APPROVE, note=None,
                requested_by="github:ana", head_sha="b" * 40,
            )  # fmt: skip


async def test_submit_command_budget_amount_required(api_db, needs_human_job):
    async with api_db.sessions.begin() as s:
        with pytest.raises(CommandRefused, match="add_budget needs an amount"):
            await submit_command(
                s, job_id=needs_human_job, action=HumanAction.ADD_BUDGET, note=None,
                requested_by="x",
            )  # fmt: skip


async def test_approve_refused_unless_waiting(api_db, building_job):
    assert (await api_db.post_action(building_job, "approve")).status_code == 409


async def test_approve_refused_on_failed(api_db, failed_job):
    assert (await api_db.post_action(failed_job, "approve")).status_code == 409


async def test_retry_and_cancel_of_failed_jobs(api_db, failed_job, old_failed_job):
    assert (await api_db.post_action(failed_job, "retry")).status_code == 202
    async with api_db.sessions.begin() as s:
        with pytest.raises(CommandRefused, match="graph version no longer supported"):
            await submit_command(
                s, job_id=old_failed_job, action=HumanAction.RETRY, note=None,
                requested_by="ap@x.io",
            )  # fmt: skip
    assert (await api_db.post_action(old_failed_job, "cancel")).status_code == 202


async def test_retry_refused_when_graph_offers_none(api_db, escalated_job):
    assert (await api_db.post_action(escalated_job, "retry")).status_code == 409


async def test_second_gate_action_for_the_same_wait_refused(api_db, needs_human_job):
    assert (
        await api_db.post_action(needs_human_job, "rework", note="Add a test")
    ).status_code == 202
    r = await api_db.post_action(needs_human_job, "approve", head_sha=WAITING_SHA)
    assert (
        r.status_code == 409 and r.json()["detail"] == "an action is already pending for this wait"
    )
    assert (await api_db.post_action(needs_human_job, "cancel")).status_code == 202
    rows = await api_db.all(
        "select action, gate_event_id from app.commands where job_id=%s order by created_at",
        needs_human_job,
    )
    assert rows == [("rework", await api_db.latest_waiting_id(needs_human_job)), ("cancel", None)]


async def test_board_excludes_benchmark_and_improver_jobs(api_db, one_of_each_kind):
    body = (await api_db.get("/api/board")).json()
    assert {j["job_id"] for j in body["jobs"]} == {str(one_of_each_kind.normal)}
    assert {"jobs", "apps", "last_event_id", "generated_at"} <= body.keys()
    job = body["jobs"][0]
    assert job["column"] == "queued" and job["ref"].startswith("demo:")


async def test_board_waiting_job_shows_head_and_retry(api_db, needs_human_job):
    job = (await api_db.get("/api/board")).json()["jobs"][0]
    assert (job["column"], job["head_sha"], job["retry_offered"]) == (
        "needs_human",
        WAITING_SHA,
        True,
    )


async def test_jobs_list_excludes_benchmark_and_improver_jobs(api_db, one_of_each_kind):
    assert [j["job_id"] for j in (await api_db.get("/api/jobs")).json()] == [
        str(one_of_each_kind.normal)
    ]


async def test_job_detail_runs_events_artifacts(api_db, needs_human_job):
    r = await api_db.get(f"/api/jobs/{needs_human_job}")
    assert r.status_code == 200 and "approve" in r.json()["allowed_actions"]
    assert (await api_db.get(f"/api/jobs/{needs_human_job}/runs")).json() == []
    kinds = [e["kind"] for e in (await api_db.get(f"/api/jobs/{needs_human_job}/events")).json()]
    assert kinds == ["job.created", "human.waiting"]
    await api_db.artifacts.put(needs_human_job, "spec.md", "# spec")
    arts = (await api_db.get(f"/api/jobs/{needs_human_job}/artifacts")).json()
    assert [a["path"] for a in arts] == ["spec.md"]
    got = await api_db.get(f"/api/jobs/{needs_human_job}/artifacts/spec.md")
    assert got.text == "# spec" and got.headers["content-type"].startswith("text/plain")
    assert (await api_db.get("/api/jobs/demo:999")).status_code == 404


async def test_triage_correction_stored(api_db, needs_human_job):
    r = await api_db.client.post(
        f"/api/jobs/{needs_human_job}/triage-correction",
        json={"needs_ui": True}, headers=api_db._h("ap@x.io"),
    )  # fmt: skip
    assert r.status_code == 202
    got = await api_db.get(f"/api/jobs/{needs_human_job}/artifacts/triage-correction.json")
    assert got.json()["needs_ui"] is True and got.json()["by"] == "ap@x.io"
    assert await api_db.last_event_kind(needs_human_job) == "human.action"


async def test_create_job_dedupes_backlog_source_ref(api_db):
    a = await api_db.create(source="backlog", source_ref="b.md:3")
    b = await api_db.create(source="backlog", source_ref="b.md:3")
    assert a["job_id"] == b["job_id"]


async def test_create_job_dedupe_holds_under_concurrency(api_db):
    a, b = await asyncio.gather(
        api_db.create(source="backlog", source_ref="b.md:4"),
        api_db.create(source="backlog", source_ref="b.md:4"),
    )
    assert a["job_id"] == b["job_id"] and await api_db.count_jobs(source_ref="b.md:4") == 1


async def test_create_job_uses_default_budget_and_requester(api_db, sessions):
    out = await api_db.create()
    async with sessions() as s:
        j = await s.scalar(select(Job).where(Job.id == out["job_id"]))
    assert j is not None and j.requested_by == "sub@x.io" and j.budget_usd == Decimal("10")
