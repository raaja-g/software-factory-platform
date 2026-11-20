from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.api.test_actions_auth import (  # noqa: F401
    JOB_ID,
    client_admin,
    client_approver,
    client_viewer,
    fake_artifacts,
)

from swfactory.api.routes import jobs as jobs_routes
from swfactory.scoring.service import ScoreSummary, ScoringService

SCORERS = {"merge-readiness": object(), "scope-creep": object(), "spec-adherence": object()}


@pytest.fixture
def scoring(fake_artifacts, monkeypatch):  # noqa: F811
    calls: list[dict] = []
    created: list[dict] = []
    state = {"scorers": dict(SCORERS)}

    async def score_job(self, deps, job_id, *, force=False, only=None):
        calls.append({"force": force, "only": only})
        return ScoreSummary(
            job_id,
            "good",
            9,
            0,
            tuple(f"{n}: judges run on the worker" for n in sorted(state["scorers"])),
            (),
        )

    async def pinned(deps, job_id):
        return None, SimpleNamespace(scorers=state["scorers"]), None

    async def create(s, **kw):
        created.append(kw)
        return uuid4()

    async def scores(request, job_id):
        return [{"scorer": "obj.gate", "label": "passed"}]

    monkeypatch.setattr(ScoringService, "score_job", score_job)
    monkeypatch.setattr(jobs_routes, "pinned_definition", pinned)
    monkeypatch.setattr(jobs_routes, "create_score_request", create)
    monkeypatch.setattr(jobs_routes, "_scores", scores)
    return SimpleNamespace(calls=calls, created=created, state=state)


def test_score_inserts_a_request_when_judges_would_run(client_admin, scoring):  # noqa: F811
    r = client_admin.post("/api/jobs/todo:1/score", json={"force": True})
    assert r.status_code == 202
    body = r.json()
    assert body["request_id"] and body["scores"] == [{"scorer": "obj.gate", "label": "passed"}]
    assert body["summary"]["judges_pending"] == ["merge-readiness", "scope-creep", "spec-adherence"]
    assert body["summary"]["quality"] == "good" and body["summary"]["judges_skipped"] == []
    assert scoring.created == [
        {"job_id": JOB_ID, "scorers": None, "force": True, "requested_by": "admin@x.io"}
    ]


def test_score_only_limits_the_pending_judges(client_admin, scoring):  # noqa: F811
    r = client_admin.post("/api/jobs/todo:1/score", json={"scorers": ["scope-creep"]})
    assert r.status_code == 202
    assert r.json()["summary"]["judges_pending"] == ["scope-creep"]
    assert scoring.created[0]["scorers"] == ["scope-creep"]


def test_score_without_judge_scorers_is_200_and_inserts_nothing(client_admin, scoring):  # noqa: F811
    scoring.state["scorers"] = {}
    r = client_admin.post("/api/jobs/todo:1/score", json={})
    assert r.status_code == 200
    assert "request_id" not in r.json() and r.json()["summary"]["judges_pending"] == []
    assert scoring.created == [] and scoring.calls == [{"force": False, "only": None}]


def test_score_only_naming_no_scorer_inserts_nothing(client_admin, scoring):  # noqa: F811
    r = client_admin.post("/api/jobs/todo:1/score", json={"scorers": ["nope"]})
    assert r.status_code == 200 and scoring.created == []


def test_score_needs_admin(client_approver, scoring):  # noqa: F811
    assert client_approver.post("/api/jobs/todo:1/score", json={}).status_code == 403
    assert scoring.created == []


def _view(status="done", job_id=JOB_ID):
    return SimpleNamespace(
        id=uuid4(), job_id=job_id, status=status, summary={"judges_run": 1}, detail=None
    )


@pytest.fixture
def request_row(fake_artifacts, monkeypatch):  # noqa: F811
    row = _view()

    async def get(s, rid):
        return row if str(rid) == str(row.id) else None

    async def resolve(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="x")

    monkeypatch.setattr(jobs_routes, "get_score_request", get)
    monkeypatch.setattr(jobs_routes, "resolve_job_ref", resolve)
    return row


def test_get_score_request(client_viewer, request_row):  # noqa: F811
    r = client_viewer.get(f"/api/score-requests/{request_row.id}")
    assert r.status_code == 200
    assert r.json() == {"status": "done", "summary": {"judges_run": 1}, "detail": None}


def test_get_score_request_unknown_or_malformed_is_404(client_viewer, request_row):  # noqa: F811
    assert client_viewer.get(f"/api/score-requests/{uuid4()}").status_code == 404
    assert client_viewer.get("/api/score-requests/not-a-uuid").status_code == 404
