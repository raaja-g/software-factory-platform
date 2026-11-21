"""The improver graph end to end: real git and a real database, a scripted engine and fake bench."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from tests.fakes.deps import make_test_deps
from tests.fakes.github import FakeGitHub
from tests.fakes.host_sandbox import HostSandbox
from tests.support.pg import truncate_app_tables

from swfactory.bench.report import ConfigSummary
from swfactory.db.tables import BenchRun, Improvement, Job
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.domain.types import (
    AgentResult,
    AgentTask,
    EngineSupport,
    JobKind,
    Role,
    RunStatus,
    ToolPolicy,
    Usage,
)
from swfactory.engines.fake import FakeScript
from swfactory.engines.registry import register_engine
from swfactory.improve.graph import (
    IMPROVE_NODES,
    IMPROVER_QUEUE_DEADLINE_H,
    CycleCapReached,
    ImproveDeps,
    build_improve_graph,
    expire_queued,
    improve_initial_state,
)
from swfactory.improve.proposal import REGRESSIONS_HEADING
from swfactory.improve.workspace import DefinitionWorkspace, improve_branch
from swfactory.sandbox.paths import REPO

pytestmark = pytest.mark.db

CREATED = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
MERGE_CALLS = ("merge_async", "merge_async_status", "enable_auto_merge", "mark_ready")
PROPOSAL = "Tighten the builder guidance.\n\n" + REGRESSIONS_HEADING + "\n- job a: fixed\n"
SLUG = "acme/factory-definition"


def redraft(old: str | None) -> str:
    assert old is not None
    head, _, _ = old.partition("\n---\n")
    return head + "\n---\nRedrafted: keep commits small and run the tests before you stop.\n"


class ImproverEngine:
    """Applies scripted edits to the sandbox repo on a write step; analysis is plain text."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tasks: list[AgentTask] = []
        self.edits: dict[str, Callable[[str | None], str | None]] = {}
        # edits the agent commits in the sandbox's own git before the visible ones (history forging)
        self.committed_edits: dict[str, Callable[[str | None], str | None]] = {}
        self.proposal = PROPOSAL

    def support_for(self, model):
        return EngineSupport.OK

    async def run(self, task: AgentTask, box):
        self.tasks.append(task)
        text = "analysis: stale guidance"
        if task.tool_policy == ToolPolicy.WRITE:
            assert box is not None and task.output_path
            await self._apply(box, self.committed_edits)
            if self.committed_edits:
                git = ["git", "-c", "user.name=a", "-c", "user.email=a@a"]
                await box.exec([*git, "commit", "-qam", "forged"], cwd=REPO)
            await self._apply(box, self.edits)
            text = self.proposal
            await box.write_text(task.output_path, text)
        return AgentResult(
            status=RunStatus.OK,
            output=text,
            session_id=None,
            usage=Usage(),
            used_models=(),
            reported_cost_usd=None,
            transcript=None,
            error=None,
            engine_version="fake-1",
            meta={},
        )

    @staticmethod
    async def _apply(box, edits) -> None:
        for path, fn in edits.items():
            target = f"{REPO}/{path}"
            new = fn(await box.read_text(target))
            if new is None:
                await box.remove(target)
            else:
                await box.write_text(target, new)


class HostProvider:
    """Hands out one real-process sandbox per job."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.boxes: dict[UUID, HostSandbox] = {}

    async def ensure(self, job_id, runner):
        if job_id not in self.boxes:
            self.boxes[job_id] = HostSandbox(self.root / str(job_id))
        return self.boxes[job_id]

    async def destroy(self, job_id):
        self.boxes.pop(job_id, None)


def summary(name: str, rate: float, cost: str) -> ConfigSummary:
    return ConfigSummary(
        name=name,
        trials=1,
        completed=1,
        not_run=0,
        hidden_pass_rate=rate,
        judge_pass_rate=rate,
        judge_pass_rate_uncalibrated=rate,
        quality=rate,
        mean_rounds=1.0,
        total_cost_usd=Decimal(cost),
        mean_cost_usd=Decimal(cost),
        median_duration_s=10.0,
        task_pass={"t1": rate},
    )


class FakeBench:
    def __init__(self, sessions) -> None:
        self.sessions = sessions
        self.started: list[tuple[str, str]] = []
        self.runs: dict[tuple[str, str], UUID] = {}
        self.state: dict[UUID, str] = {}
        self.committed: dict[UUID, Decimal] = {}
        self.suite_budget = Decimal("40")
        self.cost_fails: set[UUID] = set()

    async def add_run(self, suite: str, sha: str, state: str = "done") -> UUID:
        async with self.sessions.begin() as s:
            run = BenchRun(
                suite=suite,
                definition_sha=sha,
                budget_usd=self.suite_budget,
                status="done" if state == "done" else "running",
            )
            s.add(run)
            await s.flush()
            run_id = run.id
        self.runs[(suite, sha)] = run_id
        self.state[run_id] = state
        return run_id

    async def find_run(self, suite, definition_sha):
        return self.runs.get((suite, definition_sha))

    async def start(self, suite, *, definition_sha, cycle_id, max_cycle_usd):
        committed = self.committed.get(cycle_id, Decimal(0))
        if committed + self.suite_budget > max_cycle_usd:
            raise CycleCapReached(committed, self.suite_budget, max_cycle_usd)
        self.started.append((suite, definition_sha))
        return await self.add_run(suite, definition_sha, "running")

    async def status(self, run_id):
        return self.state[run_id]

    async def summary(self, run_id):
        rate = 0.5 if run_id == self.runs.get(self.started_base) else 0.8
        return (summary("gemini", rate, "2"),)

    started_base: tuple[str, str] = ("", "")

    async def cost(self, run_id):
        if run_id in self.cost_fails:
            raise RuntimeError("report is gone")
        return Decimal("1.5")


class GhWorkspace(DefinitionWorkspace):
    """A GitHub-source workspace whose transport is a local origin (the real git still runs)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.slug = SLUG

    async def checkout(self, base_sha: str):
        self.slug = None  # the origin is a local directory
        try:
            return await super().checkout(base_sha)
        finally:
            self.slug = SLUG

    async def push(self, branch: str, sha: str) -> None:
        await asyncio.to_thread(self._push_local, branch, sha)


@pytest.fixture
async def world(sessions, tmp_path, monkeypatch):
    origin = tmp_path / "def"
    init_definition_repo(origin)
    result = load_definition(str(origin))
    assert result.definition is not None
    base_sha = result.sha
    script = FakeScript()
    core = make_test_deps(sessions, script)
    core = replace(core, sandboxes=HostProvider(tmp_path / "boxes"))  # pyright: ignore
    assert await core.definitions.save(result)
    fake_engine = ImproverEngine("fake")
    register_engine(fake_engine)
    register_engine(ImproverEngine("direct"))
    async with sessions.begin() as s:
        job = Job(
            app_id=None,
            seq=1,
            slug="improve",
            title="improve",
            feature="improve",
            kind=JobKind.IMPROVER.value,
            requested_by="improver",
            definition_sha=base_sha,
            budget_usd=Decimal("10"),
        )
        s.add(job)
        await s.flush()
        row = Improvement(
            trigger="manual",
            role_group="builder",
            base_sha=base_sha,
            suite="v1-parity",
            job_id=job.id,
            cycle_id=uuid4(),
            created_at=CREATED,
            evidence={
                "signals": [
                    {
                        "job_id": str(uuid4()),
                        "job_ref": "demo#1",
                        "role": "builder",
                        "kind": "failed",
                        "detail": "tests did not pass",
                        "at": CREATED.isoformat(),
                    }
                ]
            },
        )
        s.add(row)
        await s.flush()
        improvement_id, job_id = row.id, job.id
    bench = FakeBench(sessions)
    base_run = await bench.add_run("v1-parity", base_sha, "done")
    bench.started_base = ("v1-parity", base_sha)
    gh = FakeGitHub()
    saved: list[str] = []
    real_save = core.definitions.save

    async def save_spy(result, **kw):
        saved.append(result.sha)
        return await real_save(result, **kw)

    monkeypatch.setattr(core.definitions, "save", save_spy)
    ws_root = tmp_path / "ws"
    ws_job = uuid4()
    w = SimpleNamespace(
        sessions=sessions,
        origin=origin,
        base_sha=base_sha,
        core=core,
        engine=fake_engine,
        bench=bench,
        base_run=base_run,
        gh=gh,
        saved=saved,
        improvement_id=improvement_id,
        job_id=job_id,
        ws_root=ws_root,
        ws_job=ws_job,
        checkpointer=InMemorySaver(),
    )

    def graph(*, github: bool = True):
        cls = GhWorkspace if github else DefinitionWorkspace
        return build_improve_graph(
            ImproveDeps(
                core=core,
                bench=bench,  # pyright: ignore
                workspace=lambda: cls(str(origin), ws_root, job_id=ws_job, auth=None, gitops=None),
                github=(lambda slug: gh) if github else None,  # pyright: ignore
                clock=lambda: CREATED,
            ),
            w.checkpointer,
        )

    w.graph = graph
    yield w
    for name in MERGE_CALLS:  # on every test path the improver never touched a merge API
        assert gh.calls[name] == 0
    async with sessions() as s:
        conn = await s.connection()
        await truncate_app_tables(conn)
    shutil.rmtree(tmp_path, ignore_errors=True)


async def row(w) -> Improvement:
    async with w.sessions() as s:
        found = await s.get(Improvement, w.improvement_id)
    assert found is not None
    return found


async def run(w, *, thread: str | None = None, github: bool = True, edits=None):
    if edits is not None:
        w.engine.edits = edits
    g = w.graph(github=github)
    cfg = {"configurable": {"thread_id": thread or str(w.improvement_id)}}
    state = improve_initial_state(await row(w))
    return await g.ainvoke(state, cfg), g, cfg


def builder_edit():
    return {"roles/builder.md": redraft}


def test_nodes_are_the_planned_ones():
    assert IMPROVE_NODES == (
        "analyze",
        "propose",
        "validate",
        "benchmark",
        "await_bench",
        "open_pr",
        "record",
    )


async def test_happy_path_opens_pr(world):
    s, g, cfg = await run(world, edits=builder_edit())
    assert s["status"] == "benchmarking"
    assert s["__interrupt__"]
    assert world.bench.started == [("v1-parity", s["proposed_sha"])]  # the base run was reused
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    s = await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    assert s["status"] == "pr_open"
    pr = world.gh.prs[1]
    assert pr["head"] == improve_branch(CREATED.date(), Role.BUILDER, world.improvement_id)
    assert pr["head"].startswith("improve/20261111-builder-")
    assert "## Untrusted agent text" in pr["body"]
    assert pr["draft"] is False and REGRESSIONS_HEADING in pr["body"]
    final = await row(world)
    assert (final.status, final.pr_url, final.proposed_sha) == (
        "pr_open",
        s["pr_url"],
        s["proposed_sha"],
    )
    assert final.bench_run_before == world.base_run and final.bench_run_after is not None
    assert len(final.evidence["started_bench_runs"]) == 1
    assert final.cost_usd == Decimal("3.0")  # two reports at 1.5 and an improver job that cost 0
    async with world.sessions() as db:
        stage = await db.scalar(select(Job.stage).where(Job.id == world.job_id))
    assert stage == "delivered"
    assert world.saved == [s["proposed_sha"]]
    # the branch exists in the definition repo and only touches the one role file
    out = subprocess.run(
        ["git", "-C", str(world.origin), "diff", "--name-only", "main", s["branch"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == ["roles/builder.md"]


async def test_disallowed_path_rejected(world):
    edits = {**builder_edit(), "runners/standard.yaml": lambda old: (old or "") + "# x\n"}
    s, _, _ = await run(world, edits=edits)
    assert s["status"] == "rejected" and s["violations"][0]["rule"] == "path"
    assert world.gh.prs == {} and world.bench.started == []
    assert world.saved == []
    assert (await row(world)).evidence["violations"][0]["rule"] == "path"


async def test_removing_security_from_sensitive_app_rejected(world):
    def drop_security(old):
        assert old is not None
        return "\n".join(ln for ln in old.splitlines() if "reviewer-security" not in ln) + "\n"

    s, _, _ = await run(world, edits={"profiles/team-gemini.yaml": drop_security})
    assert s["status"] == "rejected"
    assert world.gh.prs == {} and world.bench.started == []


async def test_no_change(world):
    s, _, _ = await run(world, edits={})
    assert s["status"] == "no_change"
    assert (await row(world)).status == "no_change"


async def test_proposal_without_regressions_section_rejected(world):
    world.engine.proposal = "Just a summary.\n"
    s, _, _ = await run(world, edits=builder_edit())
    assert (s["status"], s["violations"][0]["rule"]) == ("rejected", "invalid")
    assert world.bench.started == []


async def test_other_role_file_rejected(world):
    edits = {**builder_edit(), "roles/reviewer-security.md": redraft}
    s, _, _ = await run(world, edits=edits)
    assert (s["status"], s["violations"][0]["rule"]) == ("rejected", "scope")
    assert world.bench.started == []


async def test_invalid_definition_not_saved(world):
    # an edit inside the builder's own profile entry that breaks the whole definition (an alias
    # that does not exist) is in scope, so it is the definition loader that rejects it. A broken
    # role file is stopped earlier now, as `scope`, because role frontmatter is locked.
    def break_builder_model(old: str) -> str:
        block = "  builder:\n    engine: opencode\n    model: gemini-pro@vertex\n"
        assert block in old
        return old.replace(block, block.replace("gemini-pro@vertex", "no-such-model@vertex"), 1)

    s, _, _ = await run(world, edits={"profiles/team-gemini.yaml": break_builder_model})
    assert s["status"] == "rejected"
    assert s["violations"][0]["rule"] == "invalid", s["violations"]
    assert world.saved == []
    assert world.bench.started == []


async def test_rerun_is_idempotent(world):
    s, g, cfg = await run(world, edits=builder_edit())
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    # a replay: same improvement, a new thread, so every node runs again
    s2, _, _ = await run(world, thread="replay")
    assert s2["status"] == "pr_open"
    assert len(world.gh.prs) == 1
    assert len(world.bench.started) == 1
    assert world.gh.calls["create_pr"] == 1


async def test_local_definition_branch_ready(world):
    s, g, cfg = await run(world, github=False, edits=builder_edit())
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    s = await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    assert (s["status"], s["pr_url"]) == ("branch_ready", None)
    assert world.gh.prs == {}
    async with world.sessions() as db:
        stage = await db.scalar(select(Job.stage).where(Job.id == world.job_id))
    assert stage == "delivered"


async def test_cycle_cap_stops_new_benchmark_runs(world):
    # this cycle already committed $100 of a $120 cycle cap; a $40 suite run would pass it
    cycle = (await row(world)).cycle_id
    world.bench.committed[cycle] = Decimal("100")
    s, _, _ = await run(world, edits=builder_edit())
    assert (s["status"], world.bench.started) == ("failed", [])
    err = (await row(world)).error
    assert err is not None and err.startswith("cycle benchmark cap reached")
    assert "$100 committed" in err and "$40 more would pass $120" in err


async def test_failed_bench_run_fails_the_improvement(world):
    s, g, cfg = await run(world, edits=builder_edit())
    for run_id in world.bench.state:
        world.bench.state[run_id] = "failed"
    s = await g.ainvoke(Command(resume={"bench_done": False}), cfg)
    assert s["status"] == "failed"
    assert world.gh.prs == {}


async def test_node_error_is_logged_and_recorded(world, monkeypatch):
    async def boom(self, base_sha):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(DefinitionWorkspace, "checkout", boom)
    s, _, _ = await run(world, edits=builder_edit())
    assert s["status"] == "failed"
    err = (await row(world)).error
    assert err is not None and "propose failed" in err and "clone failed" in err


async def test_no_merge_api_is_ever_called(world):
    s, g, cfg = await run(world, edits=builder_edit())
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    assert sum(world.gh.calls[n] for n in MERGE_CALLS) == 0
    assert set(world.gh.calls) <= {"find_pr", "create_pr"}


# -- review follow-ups (database): forged history, fail-closed gaps, queue expiry ---------------


async def test_forged_sandbox_history_is_rejected(world):
    """The agent commits a forbidden edit in the sandbox's git, then makes a small allowed one."""
    team = "profiles/team-gemini.yaml"

    def retarget_reviewer_code(old):
        assert old is not None
        return old.replace(
            "reviewer-code:     { engine: opencode, model: gemini-flash@vertex }",
            "reviewer-code:     { engine: opencode, model: claude-sonnet@vertex }",
        )

    world.engine.committed_edits = {team: retarget_reviewer_code}
    s, _, _ = await run(world, edits=builder_edit())
    assert s["status"] == "rejected"
    assert any("roles.reviewer-code" in v["message"] for v in s["violations"])
    assert world.gh.prs == {} and world.bench.started == [] and world.saved == []


async def test_a_missing_definition_fails_closed_without_a_pr(world, monkeypatch):
    s, g, cfg = await run(world, edits=builder_edit())
    proposed = s["proposed_sha"]
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    real_get = world.core.definitions.get

    async def get(sha):
        return None if sha == proposed else await real_get(sha)

    monkeypatch.setattr(world.core.definitions, "get", get)
    s = await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    assert s["status"] == "failed" and world.gh.prs == {}
    err = (await row(world)).error
    assert err is not None and "definition" in err and "missing" in err


async def test_an_unreadable_bench_cost_is_recorded_as_unknown_not_zero(world):
    s, g, cfg = await run(world, edits=builder_edit())
    after = UUID(s["bench_after"])
    for run_id in world.bench.state:
        world.bench.state[run_id] = "failed"
    world.bench.cost_fails.add(after)
    s = await g.ainvoke(Command(resume={"bench_done": False}), cfg)
    assert s["status"] == "failed"
    final = await row(world)
    assert final.evidence["cost_unknown"] == ["bench_after"]
    assert final.cost_usd == Decimal("1.5")  # the known part only, flagged as a lower bound


async def test_a_closed_pr_on_the_branch_is_not_reused(world):
    branch = improve_branch(CREATED.date(), Role.BUILDER, world.improvement_id)
    world.gh.prs[9] = {
        "head": branch,
        "base": "main",
        "title": "old",
        "body": "",
        "draft": False,
        "state": "closed",
        "merged": False,
        "merge_sha": None,
        "head_sha": "a" * 40,
    }
    world.gh._next = 10
    s, g, cfg = await run(world, edits=builder_edit())
    for run_id in world.bench.state:
        world.bench.state[run_id] = "done"
    s = await g.ainvoke(Command(resume={"bench_done": True}), cfg)
    assert s["status"] == "pr_open" and world.gh.calls["create_pr"] == 1
    assert world.gh.prs[10]["state"] == "open"


async def test_queued_improvements_expire_when_nobody_runs_them(world):
    now = CREATED + timedelta(hours=IMPROVER_QUEUE_DEADLINE_H + 1)
    async with world.sessions.begin() as db:
        assert await expire_queued(db, CREATED + timedelta(hours=1)) == []  # still fresh
        assert await expire_queued(db, now) == [world.improvement_id]
    final = await row(world)
    assert final.status == "failed" and final.error == (
        f"no improver worker picked this up in {IMPROVER_QUEUE_DEADLINE_H} h"
    )
    async with world.sessions() as db:
        stage = await db.scalar(select(Job.stage).where(Job.id == world.job_id))
    assert stage == "failed"
