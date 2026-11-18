import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from swfactory.gitops.fake import FakeGitService
from swfactory.gitops.service import Delivery, DiffStat
from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.fake import FakeSandboxProvider

job: Any = SimpleNamespace(id=uuid4())
app: Any = SimpleNamespace(name="demo")
Z = "0" * 40


async def _box():
    return await FakeSandboxProvider().ensure(job.id, RunnerSpec("s", "i", 1, 1, 1, ()))


async def test_fake_git():
    box = await _box()
    assert await FakeGitService().prepare(job, app, box) == Z
    assert await FakeGitService().deliver(job, app, box, draft=False, expected_head=Z) == Delivery(
        "merged", None, "f" * 40, "fake"
    )
    assert (
        await FakeGitService(conflict=True).deliver(job, app, box, draft=False, expected_head=Z)
    ).status == "conflict"
    msg = "your folder has uncommitted changes"
    assert await FakeGitService(deliver_failure=msg).deliver(
        job, app, box, draft=False, expected_head=Z
    ) == Delivery("failed", None, None, msg)
    with pytest.raises(RuntimeError):
        await FakeGitService(fail_on="prepare").prepare(job, app, box)


async def test_fake_git_diff_paths_and_calls():
    box = await _box()
    g = FakeGitService(diff_paths=["src/app/web.py", "README.md"])
    assert await g.diff_stat(box, Z) == DiffStat(2, 0, 0, ("src/app/web.py", "README.md"))
    await g.keep_branch(job, app, box)
    assert g.calls == ["diff_stat", "keep_branch"]


async def test_fake_git_state_path_makes_deliver_idempotent_across_instances(tmp_path):
    box = await _box()
    d1 = await FakeGitService(state_path=tmp_path / "g.json").deliver(
        job, app, box, draft=False, expected_head=Z
    )
    d2 = await FakeGitService(state_path=tmp_path / "g.json").deliver(
        job, app, box, draft=False, expected_head=Z
    )
    assert d1 == d2 and len(json.loads((tmp_path / "g.json").read_text())["delivered"]) == 1


async def test_fake_git_records_expected_head_and_base_head():
    box = await _box()
    g = FakeGitService()
    await g.deliver(job, app, box, draft=False, expected_head="a" * 40)
    assert (g.expected_heads, await g.base_head(job, app)) == (["a" * 40], Z)
