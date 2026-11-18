from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import board
from swfactory.engines.fake import FakeScript
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"


async def test_board_shape_after_two_rounds(sessions):
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    deps = make_test_deps(sessions, FakeScript.from_yaml(FIXTURES / "rework.yaml"))
    row, defn, _ = await pinned_definition(deps, job.id)
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    await build_job_graph(deps, InMemorySaver()).ainvoke(initial_state(row, defn), cfg)
    async with sessions() as s:
        j = (await board(s, app_slug="demo"))["jobs"][0]
    assert set(j) == {
        "id", "job_id", "title", "branch", "stage", "round", "reviews", "history",
        "decision", "files", "updated", "app", "cost", "models",
    }  # fmt: skip
    assert j["id"] == f"001-{job.slug}" and j["branch"] == f"factory/{j['id']}"
    assert j["app"] == "demo"
    assert j["reviews"] == {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"}
    assert j["history"][0] == {
        "round": 1,
        "reviews": {"security": "CHANGES", "ux": "PASS", "ui": "PASS", "code": "PASS"},
    }
    assert "round-2/review-security.md" in j["files"] and j["files"] == sorted(j["files"])
    assert j["cost"] == "0.000000" and j["models"]["builder"] == "fake"
    assert j["updated"].endswith("Z")


async def test_board_filters_by_app(sessions):
    await seed_demo(sessions, apps=("a", "b"))
    job_a = await enq(sessions, app="a")
    job_b = await enq(sessions, app="b")
    async with sessions() as s:
        assert {j["job_id"] for j in (await board(s, app_slug="a"))["jobs"]} == {str(job_a.id)}
        assert {j["job_id"] for j in (await board(s))["jobs"]} == {str(job_a.id), str(job_b.id)}
