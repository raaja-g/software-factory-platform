from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.api.test_actions_auth import (  # noqa: F401
    JOB_ID,
    client_approver,
    client_submitter,
    fake_commands,
    make,
    slug_of,
)

from swfactory.api import access as access_mod
from swfactory.api.routes import jobs as jobs_routes
from swfactory.commands import CommandRefused

URL = "/api/jobs/todo:1/follow-up"


@pytest.fixture
def follow(monkeypatch):
    calls = []
    outcome: dict = {}

    async def resolve(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="x")

    async def fake(s, **kw):
        calls.append(kw)
        if "refuse" in outcome:
            raise CommandRefused(outcome["refuse"])
        return SimpleNamespace(id=uuid4(), seq=2)

    monkeypatch.setattr(access_mod, "resolve_job_ref", resolve)
    monkeypatch.setattr(access_mod, "app_slug_of", slug_of)
    monkeypatch.setattr(jobs_routes, "start_follow_up", fake)
    return calls, outcome


def test_follow_up_route_roles_and_note(client_submitter, client_approver, follow):  # noqa: F811
    assert client_submitter.post(URL, json={"note": "x"}).status_code == 403
    assert client_approver.post(URL, json={"note": ""}).status_code == 422
    assert client_approver.post(URL, json={"note": "  "}).status_code == 422
    assert client_approver.post(URL, json={"note": "x", "extra": 1}).status_code == 422
    assert follow[0] == []


def test_follow_up_created_and_refused(client_approver, follow):  # noqa: F811
    calls, outcome = follow
    r = client_approver.post(URL, json={"note": "rename X"})
    assert r.status_code == 201 and r.json()["ref"] == "todo:002"
    assert calls[0]["requested_by"] == "approver@x.io" and calls[0]["note"] == "rename X"
    outcome["refuse"] = "only a delivered job with an open PR can get a follow-up"
    r = client_approver.post(URL, json={"note": "again"})
    assert r.status_code == 409 and "delivered" in r.json()["detail"]
