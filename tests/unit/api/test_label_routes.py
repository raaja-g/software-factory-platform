from types import SimpleNamespace

import pytest
from tests.unit.api.test_actions_auth import (  # noqa: F401
    client_approver,
    client_viewer,
    fake_artifacts,
)

from swfactory.api.routes import jobs as jobs_routes

URL = "/api/jobs/demo:001/scores/merge-readiness/label"


@pytest.fixture
def defs(client_approver, client_viewer, fake_artifacts, monkeypatch):  # noqa: F811
    labels = [SimpleNamespace(value=v, score=s) for v, s in (("A", 1.0), ("B", 0.5), ("C", 0.0))]
    scorer = SimpleNamespace(labels=labels, passing_score=0.5, rubric="r", judge=None)
    stored = []

    async def active(self=None):
        return "sha", SimpleNamespace(
            factory=SimpleNamespace(apps={"todo": SimpleNamespace(budget_usd=None, access=None)}),
            scorers={"merge-readiness": scorer},
        )

    async def put(s, job_id, scorer_name, label, sha, by):
        stored.append((scorer_name, label, by))

    async def judge_scores(s, job_id):
        return []

    async def table(s, scorers=None):
        return {}

    for c in (client_approver, client_viewer):
        c.deps.definitions.active = active
    monkeypatch.setattr(jobs_routes, "rubric_sha", lambda sd: "sha")
    monkeypatch.setattr(jobs_routes, "put_judge_label", put)
    monkeypatch.setattr(jobs_routes, "job_judge_scores", judge_scores)
    monkeypatch.setattr(jobs_routes, "calibration_table", table)
    return stored


def test_label_needs_approver(client_viewer, defs):  # noqa: F811
    assert client_viewer.put(URL, json={"label": "A"}).status_code == 403
    assert defs == []


def test_label_must_exist(client_approver, defs):  # noqa: F811
    assert client_approver.put(URL, json={"label": "D"}).status_code == 422
    assert defs == []


def test_unknown_scorer_is_404(client_approver, defs):  # noqa: F811
    r = client_approver.put("/api/jobs/demo:001/scores/nope/label", json={"label": "A"})
    assert r.status_code == 404


def test_label_is_stored(client_approver, defs):  # noqa: F811
    r = client_approver.put(URL, json={"label": "A"})
    assert r.status_code == 200 and r.json()["stored"] is True
    assert defs == [("merge-readiness", "A", "approver@x.io")]
