"""run_agent_step: the only path from a graph node to an engine (idempotent, retrying)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from swfactory.db.repo import (
    AgentRunRow,
    fail_stale_runs,
    finish_agent_run,
    ok_runs,
    start_agent_run,
)
from swfactory.db.tables import App, Job
from swfactory.definition.models import AppDef, Defaults, Definition
from swfactory.domain.types import (
    AgentResult,
    AgentTask,
    Decision,
    ModelRef,
    Role,
    RunStatus,
    ToolPolicy,
    Usage,
    Verdict,
)
from swfactory.engines.registry import get_engine
from swfactory.graph.deps import Deps
from swfactory.logging import log_context
from swfactory.sandbox.base import RunnerSpec, Sandbox
from swfactory.sandbox.paths import OUT, REPO

log = structlog.get_logger(__name__)

MIN_ATTEMPT_S = 60
TIMEOUT_KEY: Mapping[Role, str] = {
    Role.REQUIREMENTS: "requirements",
    Role.SPEC_WRITER: "spec",
    Role.BUILDER: "build",
    Role.REVIEWER_SECURITY: "review",
    Role.REVIEWER_UX: "review",
    Role.REVIEWER_UI: "review",
    Role.REVIEWER_CODE: "review",
    Role.APPROVER: "approve",
    Role.TRIAGE: "triage",
    Role.JUDGE: "judge",
    Role.IMPROVER: "improve",
}
TRANSIENT_ERROR_CLASSES = frozenset({"gateway", "http_5xx", "rate_limit"})
_REVIEWERS = frozenset(
    {Role.REVIEWER_SECURITY, Role.REVIEWER_UX, Role.REVIEWER_UI, Role.REVIEWER_CODE}
)
_HEAD_ROLES = _REVIEWERS | {Role.APPROVER}  # reuse needs the same head
_RECORD_HEAD_ROLES = _HEAD_ROLES | {Role.SPEC_WRITER, Role.BUILDER}
_MAX_TURNS = 30


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class StepOutcome:
    run_id: UUID
    status: RunStatus
    text: str | None
    verdict: Verdict | None
    decision: Decision | None
    reused: bool  # a finished run was found (L7)
    diff_cut: bool = False  # the rendered prompt cut diff.patch (M3.6)


async def pinned_definition(deps: Deps, job_id: UUID) -> tuple[Job, Definition, AppDef]:
    """The job row, its pinned definition and its app; never the active definition."""
    async with deps.sessions() as s:
        job = await s.get(Job, job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")
    defn = await deps.definitions.get(job.definition_sha) if job.definition_sha else None
    if defn is None:
        raise RuntimeError(f"definition {job.definition_sha} of job {job_id} is missing")
    async with deps.sessions() as s:
        slug = await s.scalar(select(App.slug).where(App.id == job.app_id))
    if slug is None or slug not in defn.factory.apps:
        raise RuntimeError(f"app of job {job_id} is not in definition {job.definition_sha}")
    return job, defn, defn.factory.apps[slug]


def step_timeout_s(defn: Definition, key: str) -> int:
    return defn.factory.defaults.step_timeouts_min[key] * 60


def _backoff(defaults: Defaults, i: int) -> int:
    seq = defaults.engine_retry_backoff_s
    return seq[min(i, len(seq) - 1)]


def step_budget_s(defaults: Defaults, key: str) -> int:
    retries = defaults.engine_retries
    return defaults.step_timeouts_min[key] * 60 * (1 + retries) + sum(
        _backoff(defaults, i) for i in range(retries)
    )


def is_transient(
    result: AgentResult,
    *,
    is_model_unavailable: Callable[[AgentResult], bool] = lambda r: False,
) -> bool:
    if is_model_unavailable(result):
        return False
    if result.status == RunStatus.TIMEOUT:
        return True
    return (
        result.status == RunStatus.FAILED
        and result.meta.get("error_class") in TRANSIENT_ERROR_CLASSES
    )


# -- named seams of the spine's 15 steps; later milestones fill the no-ops ----------------


def _is_model_unavailable(deps: Deps, result: AgentResult) -> bool:
    return False  # M5.8 reads the catalog


async def _revoke_stale(deps: Deps, run_id: UUID) -> None:
    return None  # M5.8: GatewayClient.revoke_run


async def _check_budget(deps: Deps, job_id: UUID) -> None:
    return None  # M5


def _select_model(deps: Deps) -> tuple[str, ModelRef]:
    return "fake", ModelRef("fake", "fake", "fake")  # M5


async def _reserve_and_mint(deps: Deps, job_id: UUID) -> str:
    return ""  # M5


async def _release(deps: Deps, run_id: UUID) -> None:
    return None  # M5


def _check_contract(
    role: Role, text: str | None
) -> tuple[Verdict | None, Decision | None, str | None]:
    """(verdict, decision, problem). M3.6 replaces this with roles.contracts and repair."""
    if role not in _HEAD_ROLES:
        return None, None, None
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if role in _REVIEWERS:
        if first == "VERDICT: PASS":
            return Verdict.PASS, None, None
        if first == "VERDICT: CHANGES":
            return Verdict.CHANGES, None, None
        return None, None, "contract: first line must be 'VERDICT: PASS' or 'VERDICT: CHANGES'"
    if first in ("APPROVE", "ESCALATE"):
        return None, Decision(first), None
    return None, None, "contract: first line must be 'APPROVE' or 'ESCALATE'"


def _runner(defn: Definition, app: AppDef) -> RunnerSpec:
    r = defn.runners.get(app.runner)
    if r is None:
        return RunnerSpec(app.runner, "factory-runner", 2.0, 4096, 512, ())
    return RunnerSpec(app.runner, r.image, r.cpus, r.memory_mb, r.pids, r.allowed_hosts)


async def _head(box: Sandbox) -> str:
    res = await box.exec(["git", "rev-parse", "HEAD"], cwd=REPO)
    return res.stdout.strip()


async def _find_finished(
    deps: Deps, job_id: UUID, role: Role, round: int, occurrence: int, head: str | None
) -> AgentRunRow | None:
    async with deps.sessions() as s:
        runs = await ok_runs(s, job_id, role, round)
    if role in _HEAD_ROLES:
        runs = [r for r in runs if r.head_sha == head]
    return runs[occurrence - 1] if len(runs) >= occurrence else None


async def _cleanup_stale(
    deps: Deps, box: Sandbox, job_id: UUID, role: Role, round: int, tool_policy: ToolPolicy
) -> None:
    async with deps.sessions() as s, s.begin():
        stale = await fail_stale_runs(s, job_id, role, round)
    for row in stale:
        await _revoke_stale(deps, row.id)
    if stale and tool_policy == ToolPolicy.WRITE:
        await box.restart()
        start_sha = next((r.meta["start_sha"] for r in stale if r.meta.get("start_sha")), None)
        if start_sha:
            await box.exec(["git", "reset", "--hard", start_sha], cwd=REPO)
        await box.exec(["git", "clean", "-fdx"], cwd=REPO)


async def _outcome_of(
    deps: Deps, row: AgentRunRow, job_id: UUID, role: Role, *, reused: bool
) -> StepOutcome:
    text = await deps.artifacts.get_text(job_id, row.artifact_path) if row.artifact_path else None
    verdict, decision, _ = _check_contract(role, text)
    return StepOutcome(row.id, RunStatus(row.status), text, verdict, decision, reused)


async def run_agent_step(
    deps: Deps,
    *,
    job_id: UUID,
    role: Role,
    round: int,
    inputs: Sequence[str],
    output_name: str,
    workdir: str | None,
    tool_policy: ToolPolicy,
    attachments: Sequence[str] = (),
    resume: bool = False,
    occurrence: int = 1,
) -> StepOutcome:
    with log_context(role=role.value):
        _, defn, app = await pinned_definition(deps, job_id)
        defaults = defn.factory.defaults
        key = TIMEOUT_KEY[role]
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))

        head: str | None = await _head(box) if role in _HEAD_ROLES else None
        found = await _find_finished(deps, job_id, role, round, occurrence, head)
        if found is not None:
            return await _outcome_of(deps, found, job_id, role, reused=True)
        await _cleanup_stale(deps, box, job_id, role, round, tool_policy)

        deadline = _monotonic() + step_budget_s(defaults, key)
        outcome: StepOutcome | None = None
        for n in range(defaults.engine_retries + 1):
            if n > 0:
                await asyncio.sleep(_backoff(defaults, n - 1))
            left = int(deadline - _monotonic())
            if n > 0 and left < MIN_ATTEMPT_S:
                break
            outcome, result = await _attempt(
                deps,
                box,
                job_id=job_id,
                role=role,
                round=round,
                inputs=inputs,
                output_name=output_name,
                workdir=workdir,
                tool_policy=tool_policy,
                attachments=tuple(attachments),
                timeout_s=min(step_timeout_s(defn, key), left),
                head=head,
            )
            if result is None or not is_transient(
                result, is_model_unavailable=lambda r: _is_model_unavailable(deps, r)
            ):
                break
        assert outcome is not None
        return outcome


async def _attempt(
    deps: Deps,
    box: Sandbox,
    *,
    job_id: UUID,
    role: Role,
    round: int,
    inputs: Sequence[str],
    output_name: str,
    workdir: str | None,
    tool_policy: ToolPolicy,
    attachments: tuple[str, ...],
    timeout_s: int,
    head: str | None,
) -> tuple[StepOutcome, AgentResult | None]:
    await _check_budget(deps, job_id)
    engine_name, model = _select_model(deps)
    gateway_key = await _reserve_and_mint(deps, job_id)

    await deps.artifacts.materialize(job_id, box, list(inputs))
    meta: dict[str, Any] = {}
    run_head = head
    if tool_policy == ToolPolicy.WRITE or (role in _RECORD_HEAD_ROLES and run_head is None):
        run_head = await _head(box)
    if tool_policy == ToolPolicy.WRITE:
        meta["start_sha"] = run_head
    if role not in _RECORD_HEAD_ROLES:
        run_head = None
    async with deps.sessions() as s, s.begin():
        run_id, attempt = await start_agent_run(
            s,
            job_id=job_id,
            role=role.value,
            round=round,
            engine=engine_name,
            model_alias=model.alias,
            head_sha=run_head,
            meta=meta,
        )
    result: AgentResult | None = None
    try:
        with log_context(run_id=run_id):
            task = AgentTask(
                job_id=job_id,
                run_id=run_id,
                role=role,
                round=round,
                attempt=attempt,
                engine=engine_name,
                model=model,
                system_prompt="",
                prompt="",
                workdir=workdir,
                output_path=f"{OUT}/{run_id}.md",
                tool_policy=tool_policy,
                attachments=attachments,
                max_turns=_MAX_TURNS,
                timeout_s=timeout_s,
                resume_session=None,
                gateway_base_url="",
                gateway_key=gateway_key,
                tags={
                    "job": str(job_id),
                    "role": role.value,
                    "round": str(round),
                    "run": str(run_id),
                },
            )
            error: str | None = None
            text: str | None = None
            status: RunStatus
            try:
                result = await get_engine(engine_name).run(task, box)
                status, error = result.status, result.error
            except Exception as exc:  # recorded, never raised
                log.warning("engine.error", error=str(exc))
                status, error = RunStatus.FAILED, str(exc)
            if result is not None and status == RunStatus.OK:
                text = await box.read_text(task.output_path or "")
                if text is None:
                    text = result.output
            verdict: Verdict | None = None
            decision: Decision | None = None
            artifact_path: str | None = None
            if status == RunStatus.OK:
                verdict, decision, problem = _check_contract(role, text)
                if problem is not None:
                    status, error, verdict = RunStatus.FAILED, problem, Verdict.ERROR
                else:
                    await deps.artifacts.put(job_id, output_name, text or "", run_id=run_id)
                    artifact_path = output_name
            usage = result.usage if result is not None else Usage()
            async with deps.sessions() as s, s.begin():
                await finish_agent_run(
                    s,
                    run_id,
                    status=status,
                    verdict=verdict.value if verdict is not None else None,
                    artifact_path=artifact_path,
                    usage=usage,
                    error=error,
                    engine_version=result.engine_version if result is not None else None,
                    used_models=result.used_models if result is not None else (),
                )
            if status != RunStatus.OK:
                text = None
            outcome = StepOutcome(run_id, status, text, verdict, decision, False)
            return outcome, result
    finally:
        await _release(deps, run_id)
