import time
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from swfactory.domain.types import AgentTask, EngineSupport, ModelRef, Role, RunStatus, ToolPolicy
from swfactory.engines.fake import FakeEngine, FakeEngineError, FakeScript
from swfactory.engines.registry import get_engine, register_engine
from swfactory.sandbox.base import Sandbox

OUT = "/work/.factory/out/r.md"


class Box:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def write_text(self, path: str, text: str) -> None:
        self.files[path] = text

    async def read_text(self, path: str) -> str | None:
        return self.files.get(path)


def task(role: Role, round: int = 1, attempt: int = 1) -> AgentTask:
    return AgentTask(
        job_id=UUID(int=1),
        run_id=UUID(int=2),
        role=role,
        round=round,
        attempt=attempt,
        engine="fake",
        model=ModelRef("m@p", "p", "m"),
        system_prompt="s",
        prompt="p",
        workdir="/work",
        output_path=OUT,
        tool_policy=ToolPolicy.NONE,
        attachments=(),
        max_turns=5,
        timeout_s=60,
        resume_session=None,
        gateway_base_url="http://gw",
        gateway_key="k",
        tags={},
        max_budget_usd=Decimal(1),
    )


def fe(turns: list[dict[str, Any]]) -> FakeEngine:
    return FakeEngine(FakeScript.from_dict({"turns": turns}))


@pytest.fixture
def box() -> Sandbox:
    return cast(Sandbox, Box())


async def test_scripted_turn_writes_output_file(box: Sandbox):
    e = fe([{"role": "reviewer-security", "round": 1, "output": "VERDICT: CHANGES\nfix x"}])
    r = await e.run(task(Role.REVIEWER_SECURITY), box)
    assert r.status == RunStatus.OK and (await box.read_text(OUT)) == "VERDICT: CHANGES\nfix x"


async def test_role_defaults(box: Sandbox):
    e = fe([])
    assert "## Acceptance criteria\n- [ ] " in (await e.run(task(Role.SPEC_WRITER), box)).output  # type: ignore[operator]
    assert (await e.run(task(Role.REVIEWER_UX), box)).output.startswith("VERDICT: PASS")  # type: ignore[union-attr]
    assert (await e.run(task(Role.APPROVER), box)).output.startswith("APPROVE")  # type: ignore[union-attr]


async def test_attempt_scoped_raise(box: Sandbox):
    e = fe([{"role": "reviewer-ux", "round": 1, "attempt": 1, "raise": "boom"}])
    with pytest.raises(FakeEngineError, match="boom"):
        await e.run(task(Role.REVIEWER_UX, attempt=1), box)
    assert (await e.run(task(Role.REVIEWER_UX, attempt=2), box)).output.startswith("VERDICT: PASS")  # type: ignore[union-attr]


async def test_latency_and_timeout(box: Sandbox):
    e = fe(
        [
            {"role": "builder", "round": 1, "latency_s": 0.05},
            {"role": "builder", "round": 2, "status": "timeout"},
        ]
    )
    t0 = time.monotonic()
    await e.run(task(Role.BUILDER, round=1), box)
    assert time.monotonic() - t0 >= 0.05
    r = await e.run(task(Role.BUILDER, round=2), box)
    assert (r.status, r.output) == (RunStatus.TIMEOUT, None)


async def test_reports_fake_identity(box: Sandbox):
    e = fe([])
    r = await e.run(task(Role.BUILDER), box)
    assert (r.used_models, r.engine_version, e.support_for(object())) == (
        (),
        "fake-1",
        EngineSupport.OK,
    )
    assert e.tasks == [task(Role.BUILDER)]


async def test_scripted_used_models_and_error_class(box: Sandbox):
    r = await fe([{"role": "builder", "round": 1, "used_models": ["fake", "other"]}]).run(
        task(Role.BUILDER), box
    )
    assert r.used_models == ("fake", "other")
    r = await fe(
        [{"role": "builder", "round": 1, "status": "failed", "error_class": "gateway"}]
    ).run(task(Role.BUILDER), box)
    assert (r.status, r.meta["error_class"]) == (RunStatus.FAILED, "gateway")


def test_registry():
    e = fe([])
    register_engine(e)
    assert get_engine("fake") is e
    with pytest.raises(KeyError):
        get_engine("nope")


def test_fixtures_load():
    from pathlib import Path

    d = Path(__file__).parents[1] / "fixtures" / "fake"
    for n in ["happy", "rework", "escalate", "reviewer-error", "slow-build", "slow-review"]:
        FakeScript.from_yaml(d / f"{n}.yaml")
    s = FakeScript.from_yaml(d / "rework.yaml")
    assert s.turn("reviewer-security", 1, 1).output.startswith("VERDICT: CHANGES")  # type: ignore[union-attr]
