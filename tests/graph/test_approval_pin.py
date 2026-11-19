"""M8.4d: delivery is pinned to the approved head, re-verified when a rebase moves it.

The git service is a fake that models a branch head: ``rebase`` moves it once when the base
advanced, ``deliver`` refuses any head other than ``expected_head`` before it merges or pushes.
The verify gate that deliver runs again is replaced by a recorder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from tests.fakes.deps import make_test_deps, seed_demo
from tests.graph.test_loop import FIXTURES
from tests.support.db_helpers import enq

from swfactory.db.tables import Job
from swfactory.domain.types import Stage, VerifyStatus
from swfactory.engines.fake import FakeScript
from swfactory.gitops.fake import FakeGitService
from swfactory.gitops.service import REASON_HEAD_MISMATCH, Delivery
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.nodes import deliver as deliver_node
from swfactory.graph.routing import verify_gate_reason
from swfactory.graph.steps import pinned_definition
from swfactory.verify.gate import GateResult

pytestmark = pytest.mark.db

HEAD_A = "a" * 40
HEAD_B = "b" * 40
REBASED = "c" * 40
GITHUB = {
    "repo_kind": "github",
    "repo_url": "https://github.com/acme/web",
    "merge_policy": "pr_auto_merge",
    "github": {"ci_safe": True},
}


class _Crash(BaseException):  # a process death: the graph's step error handler must not see it
    pass


class Harness(SimpleNamespace):
    @property
    def merge_or_push_calls(self) -> int:
        return len(self.merged_or_pushed_shas)


class HeadGit(FakeGitService):
    """FakeGitService whose rebase and deliver act on the sandbox's head."""

    def __init__(self, h) -> None:  # noqa: ANN001
        super().__init__()
        self.h = h

    async def prepare(self, job, app, box):  # type: ignore[no-untyped-def]  # noqa: ANN001
        await super().prepare(job, app, box)
        box.head = HEAD_A  # pyright: ignore[reportAttributeAccessIssue]
        return "0" * 40

    async def rebase(self, job, app, box):  # type: ignore[no-untyped-def]
        self._call("rebase")
        if self.h.base_moved:
            self.h.base_moved = False
            box.head = REBASED  # pyright: ignore[reportAttributeAccessIssue]
        return True

    async def deliver(self, job, app, box, *, draft, expected_head):  # type: ignore[no-untyped-def]
        self._call("deliver")
        if box.head != expected_head:  # pyright: ignore[reportAttributeAccessIssue]
            return Delivery("conflict", None, None, REASON_HEAD_MISMATCH)
        if draft:
            return Delivery("pr_open", "https://github.com/acme/web/pull/1", None, "draft")
        self.h.check_crash()
        self.h.merged_or_pushed_shas.append(box.head)  # pyright: ignore[reportAttributeAccessIssue]
        return Delivery("merged", None, "f" * 40, "merged")


@pytest.fixture(params=["local", "github"])
def approval_graph(request, sessions, monkeypatch):
    h = Harness(
        base_moved=False,
        breaks_tests=False,
        fails_once=False,
        crash_kind=None,
        crash_armed=False,
        verify_gate_heads=[],
        merged_or_pushed_shas=[],
        box=None,
        job_id=None,
        graph=None,
        cfg=None,
        payload=None,
    )

    def check_crash() -> None:
        if h.crash_armed:
            h.crash_armed = False
            h.crash_kind = None
            raise _Crash("crash after the rebase event committed")

    h.check_crash = check_crash
    real_record = deliver_node.record_event

    async def record_event(s, job_id, kind, data):  # type: ignore[no-untyped-def]
        await real_record(s, job_id, kind, data)
        if kind == h.crash_kind:
            h.crash_armed = True  # raised by the next step, after the transaction commits

    class RecordingGate:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        async def run(self, job, app, box, **kw):  # type: ignore[no-untyped-def]
            h.check_crash()
            h.verify_gate_heads.append(box.head)
            failing = h.breaks_tests or h.fails_once
            h.fails_once = False
            if failing:
                return GateResult(VerifyStatus.FAILED, "tests failed", "t", 1, (), (), "r")
            return GateResult(VerifyStatus.PASSED, None, "t", 0, (), (), "r")

    monkeypatch.setattr(deliver_node, "record_event", record_event)
    monkeypatch.setattr(deliver_node, "VerifyGate", RecordingGate)

    async def start() -> None:
        app = GITHUB if request.param == "github" else {}
        await seed_demo(sessions, app=app, defaults={"engine_retry_backoff_s": [0, 0]})
        job = await enq(sessions, app="demo")
        h.job_id = job.id
        script = FakeScript.from_yaml(FIXTURES / "reviewer-error.yaml")
        deps = make_test_deps(sessions, script, git=HeadGit(h))
        row, defn, _ = await pinned_definition(deps, job.id)
        h.graph = build_job_graph(deps, InMemorySaver())
        h.cfg = RunnableConfig(configurable={"thread_id": str(job.id)})
        h.box = None
        st = await h.graph.ainvoke(initial_state(row, defn), h.cfg)
        assert st["__interrupt__"][0].value["reason"] == "reviewer failed"
        h.box = deps.sandboxes.boxes[job.id]  # pyright: ignore[reportAttributeAccessIssue]
        assert h.box.head == HEAD_A

    def _settled(st: dict) -> dict:
        if st.get("__interrupt__"):
            return {**st, "gate_reason": st["__interrupt__"][0].value["reason"]}
        return st

    async def _resume(payload: dict) -> dict:
        cfg: RunnableConfig = h.cfg
        try:
            return _settled(await h.graph.ainvoke(Command(resume=payload), cfg))
        except _Crash:
            return _settled(await h.graph.ainvoke(None, cfg))  # restart from the checkpoint

    def act(action: str) -> dict:
        return {"action": action, "note": "", "by": "ann", "command_id": "", "head_sha": HEAD_A}

    async def wait_for_gate() -> None:
        await start()

    async def approve() -> None:
        h.payload = act("approve")  # the person approved head A

    async def resume() -> dict:
        return await _resume(h.payload)

    async def approve_and_resume() -> dict:
        await approve()
        return await resume()

    async def retry() -> dict:
        return await _resume(act("retry"))

    h.wait_for_gate, h.approve, h.resume = wait_for_gate, approve, resume
    h.approve_and_resume, h.retry = approve_and_resume, retry
    h.builder_commit_in_sandbox = lambda: setattr(h.box, "head", HEAD_B)
    h.advance_base = lambda breaks_tests=False: (
        setattr(h, "base_moved", True),
        setattr(h, "breaks_tests", breaks_tests),
    )
    h.verify_fails_once = lambda: setattr(h, "fails_once", True)
    h.crash_after_event = lambda kind: setattr(h, "crash_kind", kind)
    h.head_sha = lambda: h.box.head
    return h


async def test_head_changed_after_approval_goes_back_to_gate(approval_graph):
    await approval_graph.wait_for_gate()
    await approval_graph.approve()
    approval_graph.builder_commit_in_sandbox()
    final = await approval_graph.resume()
    assert (
        final["gate_reason"] == REASON_HEAD_MISMATCH == "branch head differs from the approved head"
    )
    assert approval_graph.merge_or_push_calls == 0


async def test_unchanged_head_delivers(approval_graph):
    await approval_graph.wait_for_gate()
    final = await approval_graph.approve_and_resume()
    assert final["outcome"] in {Stage.MERGED, Stage.DELIVERED}
    assert approval_graph.verify_gate_heads == []  # base did not move: no re-verify
    assert approval_graph.merged_or_pushed_shas == [HEAD_A]


async def test_rebase_that_moves_head_reverifies_then_merges_that_head(approval_graph):
    await approval_graph.wait_for_gate()
    approval_graph.advance_base()
    final = await approval_graph.approve_and_resume()
    rebased = approval_graph.head_sha()
    assert rebased != HEAD_A and approval_graph.verify_gate_heads == [rebased]
    assert final["outcome"] in {Stage.MERGED, Stage.DELIVERED}
    assert approval_graph.merged_or_pushed_shas == [rebased]


async def test_rebase_stores_the_new_head_on_the_job(approval_graph, sessions):
    await approval_graph.wait_for_gate()
    approval_graph.advance_base()
    await approval_graph.approve_and_resume()
    async with sessions() as s:
        assert (await s.get_one(Job, approval_graph.job_id)).head_sha == REBASED


async def test_reverify_failure_goes_to_gate(approval_graph):
    await approval_graph.wait_for_gate()
    approval_graph.advance_base(breaks_tests=True)
    final = await approval_graph.approve_and_resume()
    assert final["verify_status"] == "failed" and final["retry_node"] == "deliver"
    assert final["gate_reason"] == verify_gate_reason("failed", final.get("verify_reason"))
    assert approval_graph.merge_or_push_calls == 0


async def test_crash_after_moving_rebase_reverifies_and_delivers(approval_graph):
    await approval_graph.wait_for_gate()
    approval_graph.advance_base()
    approval_graph.crash_after_event("delivery.rebased")
    final = await approval_graph.approve_and_resume()
    rebased = approval_graph.head_sha()
    assert final["outcome"] in {Stage.MERGED, Stage.DELIVERED}
    assert approval_graph.merged_or_pushed_shas == [rebased]
    assert approval_graph.verify_gate_heads == [rebased]  # never a mismatch for its own rebase


async def test_retry_after_failed_reverify_reverifies_rebased_head(approval_graph):
    await approval_graph.wait_for_gate()
    approval_graph.advance_base()
    approval_graph.verify_fails_once()
    gate = await approval_graph.approve_and_resume()
    assert gate["retry_node"] == "deliver"
    await approval_graph.retry()
    rebased = approval_graph.head_sha()
    assert approval_graph.verify_gate_heads == [rebased, rebased]
    assert approval_graph.merged_or_pushed_shas == [rebased]
