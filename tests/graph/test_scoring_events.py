"""M10.1: the spine's decision.made and delivery.merged payloads carry the approved head."""

from __future__ import annotations

import pytest
from tests.graph.test_loop import ctx, events, run_job

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _bind(sessions):
    ctx.sessions = sessions


async def test_event_payloads():
    job, _ = await run_job("happy.yaml")
    d = (await events(job.id, "decision.made"))[-1].data
    m = (await events(job.id, "delivery.merged"))[-1].data
    assert set(d) == {"decision", "round", "head_sha"} and d["decision"] == "APPROVE"
    assert d["round"] == 1 and len(d["head_sha"]) == 40
    assert set(m) == {"merge_sha", "head_sha", "pr_url"}
    assert m["head_sha"] == d["head_sha"] and m["pr_url"] is None
