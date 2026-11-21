from types import SimpleNamespace

import pytest
from tests.unit.api.test_actions_auth import (  # noqa: F401
    client_approver,
    client_viewer,
    fake_artifacts,
)

from swfactory.api.routes import jobs as jobs_routes
from swfactory.db.repo import LabelQueue, QueueJob
from swfactory.scoring.calibration import Calibration

URL = "/api/labels/queue"


@pytest.fixture
def seen(client_approver, client_viewer, fake_artifacts, monkeypatch):  # noqa: F811
    """What the route asked the database for, with a canned queue and calibration table."""
    labels = [
        SimpleNamespace(value=v, score=s, description=d)
        for v, s, d in (("A", 1.0, "Could merge"), ("B", 0.7, "Small fixes"), ("C", 0.0, "Wrong"))
    ]
    scorer = SimpleNamespace(
        labels=labels, passing_score=0.7, rubric="Judge the diff.\nMore.", applies_to=("builder",),
        judge=None,
    )  # fmt: skip
    calls = {}

    async def active(self=None):
        apps = {
            "demo": SimpleNamespace(budget_usd=None, access=None),
            "other": SimpleNamespace(budget_usd=None, access=None),
        }
        return "sha", SimpleNamespace(
            factory=SimpleNamespace(apps=apps), scorers={"merge-readiness": scorer}
        )

    async def queue(s, name, sha, **kw):
        calls["queue"] = (name, sha, kw)
        jobs = [QueueJob("demo:021", "Add slugify", "merged", "demo")]
        return LabelQueue(jobs, total=7, awaiting_judge=3)

    async def table(s, scorers=None):
        cal = Calibration("merge-readiness", "qwen-27b@groq", "sha", "js", 12, 8, 4, 0.5, False)
        other = Calibration("merge-readiness", "x", "OLD", "js", 99, 50, 49, 0.9, True)
        return {("merge-readiness", "qwen-27b@groq", "sha", "js"): cal,
                ("merge-readiness", "x", "OLD", "js"): other}  # fmt: skip

    for c in (client_approver, client_viewer):
        c.deps.definitions.active = active
    monkeypatch.setattr(jobs_routes, "rubric_sha", lambda sd: "sha")
    monkeypatch.setattr(jobs_routes, "label_queue", queue)
    monkeypatch.setattr(jobs_routes, "calibration_table", table)
    return calls


def test_the_queue_needs_an_approver(client_viewer, seen):  # noqa: F811
    assert client_viewer.get(URL).status_code == 403
    assert seen == {}


def test_unknown_scorer_is_404(client_approver, seen):  # noqa: F811
    assert client_approver.get(URL, params={"scorer": "nope"}).status_code == 404


def test_the_queue_shape(client_approver, seen):  # noqa: F811
    r = client_approver.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert body["scorer"] == "merge-readiness"
    assert body["labels"] == [
        {"value": "A", "score": 1.0, "description": "Could merge", "passes": True},
        {"value": "B", "score": 0.7, "description": "Small fixes", "passes": True},
        {"value": "C", "score": 0.0, "description": "Wrong", "passes": False},
    ]
    assert body["jobs"] == [
        {"ref": "demo:021", "title": "Add slugify", "stage": "merged", "app": "demo"}
    ]
    assert (body["total"], body["awaiting_judge"]) == (7, 3)
    name, sha, kw = seen["queue"]
    assert (name, sha) == ("merge-readiness", "sha")
    assert kw["roles"] == ("builder",) and kw["limit"] == 50


def test_progress_is_for_the_current_rubric_only(client_approver, seen):  # noqa: F811
    p = client_approver.get(URL).json()["progress"]
    assert (p["n"], p["human_pass"], p["human_fail"], p["calibrated"]) == (12, 8, 4, False)
    assert (p["needed"], p["per_class"]) == (30, 5)
    assert p["judge_model"] == "qwen-27b@groq" and p["tag"].startswith("uncalibrated")


def test_progress_with_no_labels_yet(client_approver, seen, monkeypatch):  # noqa: F811
    async def empty(s, scorers=None):
        return {}

    monkeypatch.setattr(jobs_routes, "calibration_table", empty)
    p = client_approver.get(URL).json()["progress"]
    assert (p["n"], p["human_pass"], p["human_fail"], p["judge_model"]) == (0, 0, 0, None)
    assert p["tag"] == "uncalibrated (0 of 30 labels)"


def test_limit_and_app_are_passed_on(client_approver, seen):  # noqa: F811
    client_approver.get(URL, params={"limit": 5, "app": "demo"})
    assert seen["queue"][2]["limit"] == 5
    assert seen["queue"][2]["app_slugs"] == frozenset({"demo"})
    assert client_approver.get(URL, params={"app": "nope"}).status_code == 404
    assert client_approver.get(URL, params={"limit": 0}).status_code == 422
