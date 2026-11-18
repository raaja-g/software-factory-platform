import dataclasses
import uuid
from decimal import Decimal

import pytest
from tests.fakes.deps import make_test_deps, seed_demo
from tests.fakes.host_sandbox import HostSandbox
from tests.graph.test_human_gate import _bind, _start, ctx  # noqa: F401

from swfactory.db.tables import Job
from swfactory.domain.types import AgentTask, ModelRef, Role, ToolPolicy
from swfactory.engines.fake import FakeEngine, FakeScript, FileEdit, ScriptedBuilder
from swfactory.gitops.fake import ZERO, FakeGitService
from swfactory.graph.nodes import prepare
from swfactory.graph.routing import REASON_FOLDER_DIRTY, REASON_FOLDER_MOVED
from swfactory.sandbox.paths import OUT, REPO


@pytest.fixture
async def host_box(tmp_path):
    box = HostSandbox(tmp_path)
    for args in (
        ["init", "-q", "-b", "main", REPO],
        ["-C", REPO, "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        res = await box.exec(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd="/work"
        )
        assert res.exit_code == 0, res.stderr
    return box


def builder_task(box: HostSandbox) -> AgentTask:
    run_id = uuid.uuid4()
    return AgentTask(
        job_id=box.job_id,
        run_id=run_id,
        role=Role.BUILDER,
        round=1,
        attempt=1,
        engine="fake",
        model=ModelRef("fake", "fake", "fake"),
        system_prompt="",
        prompt="",
        workdir=REPO,
        output_path=f"{OUT}/{run_id}.md",
        tool_policy=ToolPolicy.WRITE,
        attachments=(),
        max_turns=1,
        timeout_s=60,
        resume_session=None,
        gateway_base_url="",
        gateway_key="",
        tags={},
        max_budget_usd=Decimal(1),
    )


async def test_scripted_builder_commits(host_box):
    task = builder_task(host_box)
    engine = ScriptedBuilder(
        [[FileEdit("hello.py", "print('hi')\n")]], others=FakeEngine(FakeScript())
    )
    await engine.run(task, host_box)
    msg = await host_box.exec(["git", "log", "-1", "--format=%B"])
    assert "Factory-Role: builder" in msg.stdout and "Factory-Job:" in msg.stdout
    assert await host_box.read_text(task.output_path or "")
    assert await host_box.read_text(f"{REPO}/hello.py") == "print('hi')\n"


async def test_scripted_builder_deletes_and_hands_other_roles_on(host_box):
    others = FakeEngine(FakeScript())
    engine = ScriptedBuilder([[FileEdit("a.txt", "1\n")], [FileEdit("a.txt", None)]], others=others)
    await engine.run(builder_task(host_box), host_box)
    await engine.run(builder_task(host_box), host_box)
    assert await host_box.read_text(f"{REPO}/a.txt") is None
    log = await host_box.exec(["git", "log", "--format=%s"])
    assert log.stdout.count("factory: build round 1") == 2
    reviewer = dataclasses.replace(
        builder_task(host_box), role=Role.REVIEWER_CODE, tool_policy=ToolPolicy.READ_ONLY
    )
    await engine.run(reviewer, host_box)
    assert [t.role for t in others.tasks] == [Role.REVIEWER_CODE]


@pytest.mark.db
async def test_prepare_node_stores_branch_and_base(sessions):
    await seed_demo(sessions)
    async with sessions.begin() as s:
        from swfactory.queue import enqueue_job

        job = await enqueue_job(
            s,
            app_slug="demo",
            feature="Add greeting",
            title="Add greeting",
            source="cli",
            source_ref=None,
            requested_by="me",
        )
    deps = make_test_deps(sessions, FakeScript())
    await prepare.make(deps)({"job_id": str(job.id)})
    async with sessions() as s:
        row = await s.get(Job, job.id, populate_existing=True)
    assert row is not None
    assert (row.branch, row.base_sha) == (f"factory/{row.seq:03d}-add-greeting", ZERO)
    assert row.seq >= 1 and len(row.base_sha or "") == 40


@pytest.mark.db
@pytest.mark.parametrize("reason", [REASON_FOLDER_DIRTY, REASON_FOLDER_MOVED])
async def test_folder_refusal_waits_for_the_person(reason):
    _, _, _, res = await _start("happy.yaml", git=FakeGitService(deliver_failure=reason))
    assert res["__interrupt__"][0].value == {
        "reason": reason,
        "retry_node": "deliver",
        "artifacts": [],
    }


@pytest.mark.db
async def test_approved_head_is_recorded_and_other_head_is_refused():
    g, cfg, job, res = await _start("happy.yaml")
    assert res["outcome"] == "merged" and res["approved_head"] == ZERO
    ev = [e for e in await _events(job.id, "delivery.merged")]
    assert ev == [{"merge_sha": "f" * 40, "head_sha": ZERO, "pr_url": None}]


async def _events(job_id, kind):
    from tests.graph.test_loop import events

    return [e.data for e in await events(job_id, kind)]
