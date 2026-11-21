import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from tests.support.db_helpers import claim, items
from tests.unit.api.api_support import FakeKeys, server

from swfactory.api.app import ApiDeps, create_app
from swfactory.api.auth import hash_token
from swfactory.artifacts import ArtifactStore
from swfactory.backlogs import create_backlog, import_backlog, refresh_backlog
from swfactory.db.tables import Command
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef
from swfactory.definition.store import DefinitionStore
from swfactory.events import EventBus, record_event

pytestmark = pytest.mark.db

ROLES = {"viewer": "v@x.io", "submitter": "s@x.io", "approver": "a@x.io"}


class Users:
    async def role_of(self, email):
        return {v: k for k, v in ROLES.items()}.get(email)

    async def token_email(self, token_hash):
        return next((e for e in ROLES.values() if hash_token("sfp_" + e) == token_hash), None)


class Api:
    def __init__(self, sessions, http):
        self.sessions, self.http = sessions, http

    def client(self, role):
        h = {"authorization": f"Bearer sfp_{ROLES[role]}"}
        http = self.http

        async def get(url):
            r = await http.get(url, headers=h)
            return r.status_code, r.json()

        async def post(url, body):
            r = await http.post(url, json=body, headers=h)
            return r.status_code, r.json()

        return SimpleNamespace(get=get, post=post)

    async def commands(self):
        async with self.sessions() as s:
            return list((await s.scalars(select(Command).order_by(Command.created_at))).all())

    async def claim(self, worker):
        return await claim(self.sessions, worker)


async def _activate(sessions):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    app = AppDef.model_validate({**base.model_dump(mode="json"), "concurrency": 3})
    defaults = r.definition.factory.defaults.model_copy(update={"max_backlog_items": 2})
    fac = r.definition.factory.model_copy(update={"apps": {"todo": app}, "defaults": defaults})
    r = replace(
        r,
        sha="sha-todo-" + str(datetime.now(UTC).timestamp()),
        definition=r.definition.model_copy(update={"factory": fac}),
    )
    assert await DefinitionStore(sessions).save(r)


@pytest.fixture
async def api_db(sessions, tmp_path):
    await _activate(sessions)
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7080"
    ) as c:
        yield Api(sessions, c)


async def running(api, *ids):
    async with api.sessions.begin() as s:
        return await import_backlog(
            s, app_slug="todo", title="todo", items=items(*ids), requested_by="ann"
        )


async def put(api, job_id, stage):
    async with api.sessions.begin() as s:
        await s.execute(
            text("UPDATE app.jobs SET stage = :st WHERE id = :i"), {"st": stage, "i": job_id}
        )
        bid = await s.scalar(text("SELECT backlog_id FROM app.jobs WHERE id = :i"), {"i": job_id})
        await refresh_backlog(s, bid)


async def put_waiting(api, job_id, *, retry_node, reason="approver escalated"):
    async with api.sessions.begin() as s:
        await s.execute(
            text("UPDATE app.jobs SET stage = 'needs_human', gate_reason = :g WHERE id = :i"),
            {"i": job_id, "g": reason},
        )
        await record_event(
            s,
            job_id,
            "human.waiting",
            {"reason": reason, "retry_node": retry_node, "artifacts": []},
        )
        bid = await s.scalar(text("SELECT backlog_id FROM app.jobs WHERE id = :i"), {"i": job_id})
        await refresh_backlog(s, bid)


async def draft(api):
    async with api.sessions.begin() as s:
        bl, job = await create_backlog(
            s, app_slug="todo", requirement="Build a todo app", requested_by="ann"
        )
    await put_waiting(api, job.id, retry_node="requirements", reason="approve the backlog")
    return bl, job


async def test_list_and_show_a_backlog(api_db):
    bl, jobs = await running(api_db, "a", "b")
    st, body = await api_db.client("viewer").get("/api/backlogs?app=todo")
    assert st == 200
    assert [(b["ref"], b["status"], b["jobs_total"], b["jobs_merged"]) for b in body] == [
        ("todo:b001", "running", 2, 0)
    ]
    st, body = await api_db.client("viewer").get("/api/backlogs/todo:b001")
    assert [(i["pos"], i["id"], i["title"], i["job_ref"], i["column"]) for i in body["items"]] == [
        (1, "a", "Item a", "todo:001", "queued"),
        (2, "b", "Item b", "todo:002", "queued"),
    ]
    assert body["blocked_job"] is None and body["requirements_job"] is None


async def test_show_a_backlog_at_its_gate(api_db):
    await draft(api_db)
    _, body = await api_db.client("viewer").get("/api/backlogs/todo:b001")
    assert body["status"] == "awaiting_approval"
    assert body["requirements_job"] == {
        "ref": "todo:001",
        "stage": "needs_human",
        "gate_reason": "approve the backlog",
        "retry_offered": True,
    }


async def test_a_bad_ref_is_404(api_db):
    assert (await api_db.client("viewer").get("/api/backlogs/nonsense"))[0] == 404
    assert (await api_db.client("viewer").get("/api/backlogs/todo:b009"))[0] == 404


async def test_a_paused_backlog_names_its_blocked_job(api_db):
    _, jobs = await running(api_db, "a", "b")
    await put(api_db, jobs[0].id, "closed")
    _, body = await api_db.client("viewer").get("/api/backlogs/todo:b001")
    assert (body["status"], body["blocked_job"]) == ("paused", "todo:001")


async def test_resume_retries_a_failed_blocker(api_db):
    _, jobs = await running(api_db, "a", "b")
    await put(api_db, jobs[0].id, "failed")
    st, _ = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "resume"}
    )
    assert st == 200
    assert [(c.job_id, c.action) for c in await api_db.commands()] == [(jobs[0].id, "retry")]


async def test_resume_refuses_a_closed_blocker(api_db):
    _, jobs = await running(api_db, "a", "b")
    await put(api_db, jobs[0].id, "closed")
    st, body = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "resume"}
    )
    assert st == 409 and "skip it or cancel the backlog" in body["detail"]


async def test_resume_on_a_waiting_blocker_without_retry_points_to_the_job(api_db):
    _, jobs = await running(api_db, "a", "b")
    await put_waiting(api_db, jobs[0].id, retry_node=None)
    st, body = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "resume"}
    )
    assert st == 409 and "waiting for a person" in body["detail"]


async def test_skip_cancels_the_blocked_job_and_lets_the_next_go(api_db):
    _, jobs = await running(api_db, "a", "b")
    await put_waiting(api_db, jobs[0].id, retry_node=None)
    st, body = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "skip"}
    )
    assert st == 200 and body["status"] == "running"
    assert [(c.job_id, c.action) for c in await api_db.commands()] == [(jobs[0].id, "cancel")]
    assert (await api_db.claim("w1")).id == jobs[1].id


async def test_cancel_cancels_every_unmerged_job(api_db):
    _, jobs = await running(api_db, "a", "b", "c")
    await put(api_db, jobs[0].id, "merged")
    st, body = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "cancel"}
    )
    assert (st, body["status"]) == (200, "cancelled")
    assert {c.job_id for c in await api_db.commands()} == {jobs[1].id, jobs[2].id}


async def test_skip_on_a_running_backlog_is_409(api_db):
    await running(api_db, "a")
    st, body = await api_db.client("approver").post(
        "/api/backlogs/todo:b001/actions", {"action": "skip"}
    )
    assert st == 409 and "not paused" in body["detail"]


async def test_import_creates_a_running_backlog(api_db):
    items_ = [
        {"id": "1", "title": "a", "feature": "Do a"},
        {"id": "2", "title": "b", "feature": "Do b", "acceptance": ["b works"]},
    ]
    st, body = await api_db.client("submitter").post(
        "/api/backlogs/import", {"app": "todo", "title": "v1", "items": items_}
    )
    assert st == 201 and body["ref"] == "todo:b001" and body["status"] == "running"
    assert [j["pos"] for j in body["jobs"]] == [1, 2]


async def test_import_refuses_more_than_the_limit_and_an_empty_list(api_db):
    c = api_db.client("submitter")
    item = {"id": "1", "title": "a", "feature": "f"}
    three = [item, {**item, "id": "2"}, {**item, "id": "3"}]
    assert (await c.post("/api/backlogs/import", {"app": "todo", "title": "v", "items": three}))[
        0
    ] == 422
    assert (await c.post("/api/backlogs/import", {"app": "todo", "title": "v", "items": []}))[
        0
    ] == 422


async def test_board_and_job_detail_carry_the_backlog(api_db):
    await running(api_db, "a", "b")
    _, board = await api_db.client("viewer").get("/api/board")
    assert {(j["ref"], j["kind"], j["backlog_ref"], j["backlog_pos"]) for j in board["jobs"]} == {
        ("todo:001", "normal", "todo:b001", 1),
        ("todo:002", "normal", "todo:b001", 2),
    }
    _, job = await api_db.client("viewer").get("/api/jobs/todo:002")
    assert (job["kind"], job["backlog_ref"], job["backlog_pos"]) == ("normal", "todo:b001", 2)


async def test_a_requirements_job_is_on_the_board_in_spec_then_needs_human(api_db):
    await draft(api_db)
    _, board = await api_db.client("viewer").get("/api/board")
    assert [(j["kind"], j["column"]) for j in board["jobs"]] == [("requirements", "needs_human")]
