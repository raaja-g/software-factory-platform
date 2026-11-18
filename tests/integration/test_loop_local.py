# ruff: noqa: E501
"""Whole jobs in Docker sandboxes: fake agents, the real gate and a local bare repo."""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import NotFound
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from tests.fakes.deps import make_test_deps
from tests.support.db_helpers import claim

import swfactory.sandbox.docker as docker_mod
import swfactory.worker as worker_mod
from swfactory.db.repo import insert_command
from swfactory.db.tables import AgentRun, Job, JobEvent
from swfactory.definition.loader import load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.engines.fake import HOLD, FakeEngine, FakeScript, FileEdit, ScriptedBuilder
from swfactory.engines.registry import register_engine
from swfactory.gitops.local import LocalGit
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition
from swfactory.queue import enqueue_job
from swfactory.sandbox.docker import DockerSandboxProvider
from swfactory.sandbox.paths import REPO
from swfactory.sandbox.reaper import reconcile

pytestmark = [pytest.mark.docker, pytest.mark.db]

HERE = Path(__file__).parents[1] / "fixtures"
DEFINITION = HERE / "definitions" / "m4"
IMAGES = ("swfactory/runner:0.1.0", "swfactory/runner-web:0.1.0")
APPS = ("pyapp", "nodeapp", "webapp")
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]

GREET = 'def greet(name: str) -> str:\n    return f"Hello, {name}"\n'
TEST = (
    "import sys\nimport unittest\n\nsys.path.insert(0, '.')\n\nfrom src.greet import greet\n\n\n"
    "class GreetTest(unittest.TestCase):\n    def test_greet(self):\n"
    "        self.assertEqual(greet('x'), 'Hello, x')\n"
)
WRONG = FileEdit("src/greet.py", 'def greet(name: str) -> str:\n    return "Hi"\n')
BROKEN = [WRONG, FileEdit("tests/test_greet.py", TEST)]
FIXED = [FileEdit("src/greet.py", GREET)]
CI_EDIT = FileEdit(".github/ci.yml", "on: push\n")
AGENTS_TRUE = FileEdit("AGENTS.md", "Test command: true\n")
BREAK_TEST = FileEdit(
    "tests/test_app.py",
    "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(False)\n",
)
SUM_BROKEN = FileEdit("src/sum.js", "exports.sum = (a, b) => a - b;\n")
SUM_OK = FileEdit(
    "src/sum.js", "function sum(a, b) {\n  return a + b;\n}\n\nmodule.exports = { sum };\n"
)
ABOUT = FileEdit("NOTES.md", "about\n")
GOOD = [FileEdit("src/greet.py", GREET), FileEdit("tests/test_greet.py", TEST)]
ESCALATE = {"role": "approver", "output": "ESCALATE\nneeds a person"}


def artifact(final: dict, path: str) -> str:
    return final["__artifacts__"][path]


def artifact_paths(final: dict) -> set[str]:
    return set(final["__artifacts__"])


def builder_runs(final: dict) -> list[tuple[int, int]]:
    return final["__builder_runs__"]


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


class Stack:
    def __init__(self, sessions: Any, client: Any, provider: Any, repos: Path, tmp: Path) -> None:
        self.sessions, self.client, self.provider, self.repos, self.tmp = (
            sessions,
            client,
            provider,
            repos,
            tmp,
        )
        self.git = LocalGit(repos)
        self.jobs: list[uuid.UUID] = []
        self.deps: Any = None
        self.saver = InMemorySaver()
        self.graph: Any = None
        self.cfg: RunnableConfig = {}
        self.job: Job | None = None
        self.task: asyncio.Task[Any] | None = None
        self.keeper = False  # headline tests keep repo and artifacts (KEEP_REPOS)
        self.recorder: FakeEngine | None = None  # stands in for the non-builder roles when set
        for slug in APPS:
            self.git.init_bare(slug, seed=HERE / "repos" / slug)

    # -- running jobs ----------------------------------------------------
    async def _prepare(self, app: str, title: str, builder: Any, turns: list[dict]) -> uuid.UUID:
        async with self.sessions.begin() as s:
            job = await enqueue_job(
                s,
                app_slug=app,
                feature=title,
                title=title,
                source="cli",
                source_ref=None,
                requested_by="t",
            )
        claimed = await claim(self.sessions, "w1")
        assert claimed is not None and claimed.id == job.id
        self.job = claimed
        self.jobs.append(job.id)
        deps = make_test_deps(self.sessions, FakeScript.from_dict({"turns": turns}))
        self.deps = replace(deps, sandboxes=self.provider, git=self.git)
        register_engine(
            ScriptedBuilder(
                builder or [[ABOUT]],
                others=self.recorder or FakeEngine(FakeScript.from_dict({"turns": turns})),
            )
        )
        self.graph = build_job_graph(self.deps, self.saver)
        self.cfg = {"configurable": {"thread_id": str(job.id)}}
        return job.id

    async def _initial(self) -> dict:
        assert self.job is not None
        row, defn, _ = await pinned_definition(self.deps, self.job.id)
        return dict(initial_state(row, defn))

    async def start_job(
        self, app: str, title: str, *, builder: Any = None, approver: Any = None
    ) -> uuid.UUID:
        jid = await self._prepare(app, title, builder, [approver] if approver else [])
        self.task = asyncio.ensure_future(self.graph.ainvoke(await self._initial(), self.cfg))
        return jid

    async def run_job(
        self, app: str, title: str, *, builder: Any = None, approver: Any = None
    ) -> dict:
        await self._prepare(app, title, builder, [approver] if approver else [])
        result = await self.graph.ainvoke(await self._initial(), self.cfg)
        return await self._settle(app, result)

    async def resume(self, payload: dict) -> dict:
        assert self.job is not None
        result = await self.graph.ainvoke(Command(resume=payload), self.cfg)
        return await self._settle(None, result)

    async def _settle(self, app: str | None, result: dict) -> dict:
        assert self.job is not None
        await worker_mod._settle(self.deps, self.job, result, "w1", self.job.lease_epoch)
        final = dict(result)
        arts = {}
        for m in await self.deps.artifacts.list(self.job.id):
            arts[m.path] = await self.deps.artifacts.get_text(self.job.id, m.path)
        final["__artifacts__"] = arts
        async with self.sessions() as s:
            rows = (
                await s.scalars(
                    select(AgentRun)
                    .where(AgentRun.job_id == self.job.id, AgentRun.role == "builder")
                    .order_by(AgentRun.round, AgentRun.attempt)
                )
            ).all()
        final["__builder_runs__"] = [(r.round, r.attempt) for r in rows]
        keep = os.environ.get("FACTORY_TEST_KEEP_REPOS")
        if keep and app is not None and self.keeper:
            out = Path(keep) / "artifacts" / app
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True, exist_ok=True)
            for path, text in arts.items():
                if text is not None:
                    (out / path).parent.mkdir(parents=True, exist_ok=True)
                    (out / path).write_text(text)
            for path in arts:
                if path.endswith(".png"):
                    blob = await self.deps.artifacts.get_bytes(self.job.id, path)
                    if blob is not None:
                        (out / path).write_bytes(blob)
        return final

    # -- observing -------------------------------------------------------
    def _res(self, kind: str) -> list[str]:
        out = []
        for jid in self.jobs:
            flt = {"label": f"factory.job_id={jid}"}
            if kind == "containers":
                out += [c.name for c in self.client.containers.list(all=True, filters=flt)]
            elif kind == "volumes":
                out += [v.name for v in self.client.volumes.list(filters=flt)]
            else:
                out += [n.name for n in self.client.networks.list(filters=flt)]
        return out

    def containers(self) -> list[str]:
        return self._res("containers")

    def volumes(self) -> list[str]:
        return self._res("volumes")

    def networks(self) -> list[str]:
        return self._res("networks")

    async def events(self, kind: str) -> list[dict]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(JobEvent.data).where(JobEvent.kind == kind).order_by(JobEvent.id)
            )
            return [dict(r) for r in rows]

    async def waiting(self) -> dict:
        return (await self.events("human.waiting"))[-1]

    async def current_stage(self, job_id: uuid.UUID | None = None) -> str:
        async with self.sessions() as s:
            row = await s.get(Job, job_id or self.jobs[-1], populate_existing=True)
            assert row is not None
            return row.stage

    def bare(self, app: str) -> Path:
        return self.repos / f"{app}.git"

    def merges(self, app: str) -> list[str]:
        out = git(self.bare(app), "log", "--merges", "--format=%s", "main")
        return out.splitlines()

    def main_sha(self, app: str) -> str:
        return git(self.bare(app), "rev-parse", "refs/heads/main")

    def base_sha(self, app: str) -> str:
        return git(self.bare(app), "rev-list", "--max-parents=0", "main").splitlines()[0]

    def rev_parse(self, app: str, ref: str) -> str:
        return git(self.bare(app), "rev-parse", ref)

    def commit_to_main(self, app: str, path: str, text: str) -> None:
        work = self.tmp / f"adv-{uuid.uuid4().hex[:6]}"
        git(self.tmp, "clone", "-q", str(self.bare(app)), str(work))
        (work / path).write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "advance main")
        git(work, "push", "-q", "origin", "main")

    async def wait_for_commit(
        self, job_id: uuid.UUID, *, trailer: str, timeout: float = 120
    ) -> str:
        _, defn, app = await pinned_definition(self.deps, job_id)
        from swfactory.graph.steps import _runner

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            box = await self.provider.ensure(job_id, _runner(defn, app))
            res = await box.exec(
                ["git", "log", "-1", "--format=%H", "--fixed-strings", f"--grep={trailer}"],
                cwd=REPO,
            )
            if res.exit_code == 0 and res.stdout.strip():
                return res.stdout.strip()
            await asyncio.sleep(0.5)
        raise TimeoutError(trailer)

    async def cancel(self, job_id: uuid.UUID) -> None:
        assert self.task is not None and self.job is not None
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        async with self.sessions.begin() as s:
            await insert_command(s, job_id=job_id, action="cancel", note=None, requested_by="t")
        await worker_mod._cancel_job(
            self.deps, self.saver, self.job, "w1", self.job.lease_epoch, None
        )

    async def teardown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for jid in self.jobs:
            await self.provider.destroy(jid)


KEEPERS = {
    "test_python_job_merges_to_local_repo": "pyapp",
    "test_node_job_gate_fails_then_passes": "nodeapp",
    "test_web_job_stores_screenshots": "webapp",
}


@pytest.fixture
async def stack(sessions, tmp_path, request):
    client = docker.from_env()
    for image in IMAGES:
        try:
            client.images.get(image)
        except NotFound:
            pytest.skip("build with: docker compose --profile build build runner runner-web")
    r = load_definition(str(DEFINITION))
    assert r.definition is not None, r.errors
    assert await DefinitionStore(sessions).save(replace(r, sha=f"sha-{uuid.uuid4().hex}"))
    edge = client.containers.run(
        IMAGES[0],
        ["sleep", "infinity"],
        detach=True,
        labels={"com.docker.compose.service": "gateway-edge"},
        name=f"edge-standin-{uuid.uuid4().hex[:8]}",
    )
    keep = os.environ.get("FACTORY_TEST_KEEP_REPOS")
    repos = tmp_path / "repos"  # always fresh: tests must not see each other's merges
    provider = DockerSandboxProvider(lambda: client, spool_dir=tmp_path / "spool")
    st = Stack(sessions, client, provider, repos, tmp_path)
    st.keeper = request.node.name in KEEPERS
    try:
        yield st
    finally:
        await st.teardown()
        edge.remove(force=True)
        if keep and st.keeper:
            Path(keep).mkdir(parents=True, exist_ok=True)
            slug = KEEPERS[request.node.name]
            shutil.rmtree(Path(keep) / f"{slug}.git", ignore_errors=True)
            shutil.copytree(repos / f"{slug}.git", Path(keep) / f"{slug}.git")
        shutil.rmtree(repos, ignore_errors=True)


async def test_python_job_merges_to_local_repo(stack):
    final = await stack.run_job("pyapp", "Add greeting", builder=[GOOD])
    assert final["outcome"] == "merged" and "Merge factory/001-add-greeting" in stack.merges(
        "pyapp"
    )
    assert artifact(final, "verify.md").startswith("# Verify") and "Status: PASSED" in artifact(
        final, "verify.md"
    )
    assert artifact(final, "diff.patch").startswith("diff --git")
    assert stack.containers() == [] and stack.volumes() == []


async def test_failing_test_gets_one_fix_attempt_then_passes(stack):
    final = await stack.run_job("pyapp", "Add greeting", builder=[BROKEN, FIXED])
    assert builder_runs(final) == [(1, 1), (1, 2)]
    assert (
        final["verify_status"] == "passed"
        and final["fix_attempts"] == 1
        and final["outcome"] == "merged"
    )


async def test_protected_path_edit_goes_to_human_gate(stack):
    final = await stack.run_job(
        "pyapp",
        "Edit CI",
        builder=[[CI_EDIT], [FileEdit(".github/ci.yml", "on: pull_request\n")]],
        approver=None,
    )
    assert (
        final["gate_reason"].startswith("checks failed: protected path")
        and await stack.current_stage() == "needs_human"
    )


async def test_test_command_swap_goes_to_human_gate(stack):
    final = await stack.run_job("pyapp", "Swap", builder=[[AGENTS_TRUE, BREAK_TEST]])
    assert final["gate_reason"] == "test command changed" and "FAILED" in artifact(
        final, "verify.md"
    )


async def test_node_job_gate_fails_then_passes(stack):
    final = await stack.run_job("nodeapp", "Add sum", builder=[[SUM_BROKEN], [SUM_OK]])
    assert (
        "npm test" in artifact(final, "verify.md")
        and final["fix_attempts"] == 1
        and final["outcome"] == "merged"
    )


async def test_web_job_stores_screenshots(stack):
    final = await stack.run_job("webapp", "Add notes", builder=[[ABOUT]])
    assert {"screenshots/home.png", "screenshots/about.png"} <= artifact_paths(final) and final[
        "screenshots"
    ] == ["home", "about"]


async def test_needs_human_past_ttl_still_merges(stack, monkeypatch):
    first = await stack.run_job("pyapp", "Add greeting", builder=[GOOD], approver=ESCALATE)
    assert first["__interrupt__"] and await stack.current_stage() == "needs_human"

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=8)

    monkeypatch.setattr(docker_mod, "datetime", Later)
    await reconcile(stack.provider, stack.sessions)
    monkeypatch.undo()
    # volumes: work, home and the verified tree
    assert stack.containers() == [] and len(stack.volumes()) == 3 and len(stack.networks()) == 1
    final = await stack.resume({"action": "approve", "by": "t"})
    assert final["outcome"] == "merged"


async def test_delivery_merged_event(stack):
    final = await stack.run_job("pyapp", "Add greeting", builder=[GOOD])
    ev = (await stack.events("delivery.merged"))[-1]
    assert ev == {
        "merge_sha": stack.main_sha("pyapp"),
        "head_sha": final["approved_head"],
        "pr_url": None,
    }


async def test_approve_of_other_head_is_refused(stack):
    await stack.run_job("pyapp", "Add greeting", builder=[GOOD], approver=ESCALATE)
    final = await stack.resume({"action": "approve", "by": "t", "head_sha": "b" * 40})
    assert (
        final["gate_reason"] == "branch head differs from the approved head"
        and stack.merges("pyapp") == []
    )


async def test_main_moved_after_approval_reverifies_then_merges(stack):
    await stack.run_job("pyapp", "Add greeting", builder=[GOOD], approver=ESCALATE)
    approved = (await stack.waiting())["head_sha"]
    stack.commit_to_main("pyapp", "NOTES.md", "x\n")
    final = await stack.resume({"action": "approve", "by": "t", "head_sha": approved})
    assert final["outcome"] == "merged" and "verify/r1-a2.json" in artifact_paths(final)
    merged = (await stack.events("delivery.merged"))[-1]["head_sha"]
    assert json.loads(artifact(final, "verify/r1-a2.json"))["head_sha"] == merged != approved


async def test_cancel_mid_build_keeps_commit(stack):
    job = await stack.start_job(
        "pyapp", "Add greeting", builder=[[FileEdit("src/greet.py", GREET), HOLD]]
    )
    sha = await stack.wait_for_commit(job, trailer="Factory-Role: builder")
    await stack.cancel(job)
    assert (
        await stack.current_stage(job) == "cancelled"
        and stack.rev_parse("pyapp", "factory/001-add-greeting") == sha
    )
    assert (
        stack.main_sha("pyapp") == stack.base_sha("pyapp")
        and stack.containers() == []
        and stack.networks() == []
    )


class AttachmentRecorder(FakeEngine):
    """Reads every attachment of a task from the box the engine was given."""

    def __init__(self) -> None:
        super().__init__(FakeScript())
        self.recorded: dict[str, list[bytes]] = {}

    async def run(self, task, box):  # type: ignore[no-untyped-def]
        for path in task.attachments:
            assert box is not None
            data = await box.read_bytes(path)
            self.recorded.setdefault(str(task.role).removeprefix("reviewer-"), []).append(
                data or b""
            )
        return await super().run(task, box)


async def test_web_review_attachments_are_pngs(stack):
    rec = AttachmentRecorder()
    stack.recorder = rec
    await stack.run_job("webapp", "Add notes", builder=[[ABOUT]])
    assert (
        rec.recorded["ux"]
        and rec.recorded["ui"]
        and all(b.startswith(b"\x89PNG") for b in rec.recorded["ux"] + rec.recorded["ui"])
        and "code" not in rec.recorded
        and stack.containers() == []
    )
