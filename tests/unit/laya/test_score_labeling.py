"""M11.6: the score node labels outcomes and never fails the job when labeling breaks."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from swfactory.graph.nodes import score
from swfactory.laya import export


class _Scoring:
    async def score_job(self, deps, job_id):
        return None


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self


async def test_score_labels_after_scoring(monkeypatch):
    seen = []

    async def label(s, job_id):
        seen.append(job_id)
        return 1

    monkeypatch.setattr(export, "label_job_outcomes", label)
    deps: Any = SimpleNamespace(scoring=_Scoring(), sessions=lambda: _Session())
    jid = uuid.uuid4()
    assert await score.make(deps)({"job_id": str(jid)}) == {}
    assert seen == [jid]


async def test_labeling_error_is_swallowed(monkeypatch):
    async def boom(s, job_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(export, "label_job_outcomes", boom)
    deps: Any = SimpleNamespace(scoring=_Scoring(), sessions=lambda: _Session())
    assert await score.make(deps)({"job_id": str(uuid.uuid4())}) == {}
