"""Scripted fake engine for tests and the fake end-to-end flows."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from swfactory.domain.types import AgentResult, AgentTask, EngineSupport, Role, RunStatus, Usage
from swfactory.engines.base import ModelInfo, Sandbox

log = structlog.get_logger(__name__)

_DEFAULTS: dict[str, str] = {
    Role.TRIAGE: "ok",
    Role.REQUIREMENTS: "ok",
    Role.SPEC_WRITER: "# Spec\n\n## Acceptance criteria\n- [ ] The feature works\n",
    Role.BUILDER: "Implemented the change.",
    Role.REVIEWER_SECURITY: "VERDICT: PASS\nno findings",
    Role.REVIEWER_UX: "VERDICT: PASS\nno findings",
    Role.REVIEWER_UI: "VERDICT: PASS\nno findings",
    Role.REVIEWER_CODE: "VERDICT: PASS\nno findings",
    Role.APPROVER: "APPROVE\nlooks good",
    Role.JUDGE: "ok",
    Role.IMPROVER: "ok",
}


class FakeEngineError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FakeTurn:
    output: str | None
    status: RunStatus = RunStatus.OK
    latency_s: float = 0
    raise_: str | None = None
    error_class: str | None = None
    used_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Entry:
    role: str
    round: int | None
    attempt: int | None
    output: str | None
    has_output: bool
    status: RunStatus
    latency_s: float
    raise_: str | None
    error_class: str | None
    used_models: tuple[str, ...]


@dataclass(slots=True)
class FakeScript:
    default: Mapping[str, str] = field(default_factory=dict)
    entries: Sequence[_Entry] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FakeScript":
        default = {str(k): str(v) for k, v in (data.get("default") or {}).items()}
        entries: list[_Entry] = []
        for t in data.get("turns") or []:
            status = RunStatus(t.get("status", "ok"))
            entries.append(
                _Entry(
                    role=str(t["role"]),
                    round=t.get("round"),
                    attempt=t.get("attempt"),
                    output=t.get("output"),
                    has_output="output" in t,
                    status=status,
                    latency_s=float(t.get("latency_s", 0)),
                    raise_=t.get("raise"),
                    error_class=t.get("error_class"),
                    used_models=tuple(t.get("used_models") or ()),
                )
            )
        return cls(default, entries)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FakeScript":
        return cls.from_dict(yaml.safe_load(Path(path).read_text()) or {})

    def turn(self, role: str, round: int, attempt: int) -> FakeTurn:
        best: _Entry | None = None
        best_score = -1
        for e in self.entries:
            if e.role != role or (e.round is not None and e.round != round):
                continue
            if e.attempt is not None and e.attempt != attempt:
                continue
            score = (e.round is not None) + (e.attempt is not None)
            if score > best_score:
                best, best_score = e, score
        default = self.default.get(role, _DEFAULTS.get(role, "ok"))
        if best is None:
            return FakeTurn(default)
        output = best.output if best.has_output else default
        if best.status != RunStatus.OK and not best.has_output:
            output = None
        return FakeTurn(
            output, best.status, best.latency_s, best.raise_, best.error_class, best.used_models
        )


class FakeEngine:
    name = "fake"

    def __init__(self, script: FakeScript) -> None:
        self.script = script
        self.tasks: list[AgentTask] = []

    def support_for(self, model: ModelInfo) -> EngineSupport:
        return EngineSupport.OK

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        self.tasks.append(task)
        turn = self.script.turn(str(task.role), task.round, task.attempt)
        log.info("fake.turn", role=str(task.role), round=task.round, attempt=task.attempt)
        if turn.latency_s:
            await asyncio.sleep(turn.latency_s)
        if turn.raise_ is not None:
            raise FakeEngineError(turn.raise_)
        if turn.output is not None and box is not None and task.output_path:
            await box.write_text(task.output_path, turn.output)
        meta: dict[str, Any] = {}
        if turn.error_class:
            meta["error_class"] = turn.error_class
        return AgentResult(
            status=turn.status,
            output=turn.output,
            session_id=None,
            usage=Usage(),
            used_models=turn.used_models,
            reported_cost_usd=None,
            transcript=None,
            error=None if turn.status == RunStatus.OK else f"fake {turn.status}",
            engine_version="fake-1",
            meta=meta,
        )
