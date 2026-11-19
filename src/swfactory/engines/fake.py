"""Scripted fake engine for tests and the fake end-to-end flows."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from swfactory.domain.types import AgentResult, AgentTask, EngineSupport, Role, RunStatus, Usage
from swfactory.engines.base import Engine, ModelInfo, Sandbox
from swfactory.gitops.service import SAFE_GIT_ENV, safe_git
from swfactory.sandbox.paths import REPO

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
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    session_id: str | None = None


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
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    session_id: str | None = None


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
                    error=t.get("error"),
                    usage=Usage(**(t.get("usage") or {})),
                    session_id=t.get("session_id"),
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
            output,
            best.status,
            best.latency_s,
            best.raise_,
            best.error_class,
            best.used_models,
            best.error,
            best.usage,
            best.session_id,
        )


class FakeEngine:
    name = "fake"

    def __init__(self, script: FakeScript, name: str | None = None) -> None:
        self.script = script
        self.tasks: list[AgentTask] = []
        if name is not None:
            self.name = name

    @property
    def last_task(self) -> AgentTask:
        return self.tasks[-1]

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
            session_id=turn.session_id,
            usage=turn.usage,
            used_models=turn.used_models,
            reported_cost_usd=None,
            transcript=None,
            error=turn.error
            if turn.error is not None
            else (None if turn.status == RunStatus.OK else f"fake {turn.status}"),
            engine_version="fake-1",
            meta=meta,
        )


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One file change of a scripted builder step; ``content=None`` deletes the file."""

    path: str
    content: str | None


class Hold:
    """Step item: the builder commits the edits before it, then waits until it is cancelled."""


HOLD = Hold()

_IDENTITY = ("-c", "user.name=factory-bot", "-c", "user.email=factory-bot@localhost")


class ScriptedBuilder:
    """A fake engine whose builder makes real file edits and commits (other roles: ``others``)."""

    name = "fake"

    def __init__(self, steps: Sequence[Sequence[FileEdit | Hold]], others: Engine) -> None:
        self.steps = [list(s) for s in steps]
        self.others = others
        self.builds = 0
        self.tasks: list[AgentTask] = []

    def support_for(self, model: ModelInfo) -> EngineSupport:
        return EngineSupport.OK

    async def _git(self, box: Sandbox, cwd: str, *args: str) -> None:
        res = await box.exec(safe_git(*args), cwd=cwd, env=SAFE_GIT_ENV)
        if res.exit_code != 0:
            raise FakeEngineError(
                f"git {args[0]} failed: {res.stderr.strip() or res.stdout.strip()}"
            )

    async def _commit(self, task: AgentTask, box: Sandbox, workdir: str) -> None:
        await self._git(box, workdir, "add", "-A")
        await self._git(
            box,
            workdir,
            *_IDENTITY,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"factory: build round {task.round}",
            "--trailer",
            f"Factory-Job: {task.job_id}",
            "--trailer",
            "Factory-Role: builder",
            "--trailer",
            f"Factory-Model: {task.model.alias}",
        )

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        if task.role != Role.BUILDER or box is None:
            return await self.others.run(task, box)
        self.tasks.append(task)
        step = self.steps[self.builds] if self.builds < len(self.steps) else []
        self.builds += 1
        workdir = task.workdir or REPO
        held = False
        edited: list[str] = []
        for item in step:
            if isinstance(item, Hold):
                held = True
                break
            target = f"{workdir}/{item.path}"
            if item.content is None:
                await box.remove(target)
            else:
                await box.write_text(target, item.content)
            edited.append(item.path)
        await self._commit(task, box, workdir)
        if held:
            await asyncio.Event().wait()  # only cancellation ends this
        text = "Implemented the change.\n" + "".join(f"- {p}\n" for p in edited)
        assert task.output_path is not None
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


class FakeSelector:
    """Model selection for the fake flows: always the ``fake`` engine and model.

    It still records a ``model_decisions`` row per choice, as the real selector does."""

    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions
        self.contexts: list[Any] = []
        self.exclude_calls: list[set[str]] = []

    @property
    def last_ctx(self) -> Any:
        return self.contexts[-1]

    async def choose(self, profile: Any, ctx: Any, *, exclude: Any = ()) -> Any:
        from swfactory.db.repo import insert_model_decision
        from swfactory.domain.types import ModelRef, SelectionMode
        from swfactory.models.selection import ModelChoice

        self.contexts.append(ctx)
        self.exclude_calls.append(set(exclude))
        async with self.sessions() as s, s.begin():
            decision_id = await insert_model_decision(
                s,
                job_id=ctx.job_id,
                role=ctx.role.value,
                round=ctx.round,
                mode=SelectionMode.FIXED.value,
                options=[{"alias": "fake"}],
                chosen="fake",
                reason="fake engine",
                latency_ms=0,
            )
        return ModelChoice(
            engine="fake",
            model=ModelRef("fake", "fake", "fake"),
            mode=SelectionMode.FIXED,
            tier=None,
            decision_id=decision_id,
            reason="fake engine",
        )


class FakeRunGateway:
    """Run keys for the fake flows: nothing is minted at a gateway and nothing is spent."""

    instant_spend = True  # no spend batch to wait for

    async def mint_run_key(self, *, run_id: Any, **_: Any) -> str:
        return f"sk-fake-{run_id}"

    async def key_spend(self, key: str) -> Any:
        from decimal import Decimal

        return Decimal(0)

    async def revoke(self, key: str) -> None:
        return None

    async def revoke_run(self, run_id: Any) -> None:
        return None

    async def run_spend(self, run_id: Any) -> Any:
        from decimal import Decimal

        return Decimal(0)
