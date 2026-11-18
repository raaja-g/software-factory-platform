"""run_agent_step: the only path from a graph node to an engine (idempotent, retrying)."""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, text

from swfactory.costs.budgets import BudgetExceeded
from swfactory.costs.pricing import estimate_cost
from swfactory.db.repo import (
    AgentRunRow,
    fail_stale_runs,
    finish_agent_run,
    last_model_decision,
    latest_reviewer_verdicts,
    lock_job,
    ok_runs,
    record_run_cost,
    start_agent_run,
)
from swfactory.db.tables import AgentRun, App, Job
from swfactory.definition.models import AppDef, Defaults, Definition, max_turns_for
from swfactory.domain.types import (
    AgentResult,
    AgentTask,
    CostSource,
    Decision,
    Role,
    RunStatus,
    ToolPolicy,
    Usage,
    Verdict,
)
from swfactory.engines.registry import get_engine
from swfactory.gateway.client import model_error_class
from swfactory.gateway.render import SPEND_BATCH_S
from swfactory.graph.deps import Deps
from swfactory.logging import log_context
from swfactory.models.catalog import ModelInfo
from swfactory.models.profiles import model_family, resolve_profile
from swfactory.models.selection import ModelChoice, NoModelAvailable, SelectionContext
from swfactory.roles.contracts import ContractError, check_output, parse_decision, parse_verdict
from swfactory.roles.fence import fence_untrusted
from swfactory.roles.library import RoleSpec, effective_tool_policy
from swfactory.roles.render import (
    DEFAULT_CONTEXT_TOKENS,
    PromptContext,
    diff_was_cut,
    render_prompt,
)
from swfactory.sandbox.base import RunnerSpec, Sandbox, running
from swfactory.sandbox.paths import OUT, REPO
from swfactory.verify.smoke import screenshot_path

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
CONTRACT_REPAIRS = 1
MAX_QUOTA_BACKOFF_S = 60
DAILY_RECHECK_S = 30
DAILY_WAIT_S = 600
SPEND_RETRY_S = 2
REPAIR_PROMPT = (
    "Your previous output did not follow the output contract: {problem}\n"
    "Write {target} again now. {rule} Keep the rest of the format. Do not do any other work."
)
_RULES: Mapping[str, str] = {
    "verdict": "Its first non-empty line must be exactly `VERDICT: PASS` or `VERDICT: CHANGES`.",
    "decision": "Its first non-empty line must be exactly `APPROVE` or `ESCALATE`.",
    "requirements": "It must end with one fenced `yaml` backlog block that follows the format.",
    "spec": "It must contain a `## Acceptance criteria` section with at least one `- [ ]` item.",
}


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _await_spend(deps: Deps, seconds: float) -> None:
    """Wait for the gateway to flush its spend batch (a fake gateway has none to wait for)."""
    if not getattr(deps.gateway, "instant_spend", False):
        await _sleep(seconds)


@asynccontextmanager
async def _nullcontext(box: Sandbox):
    yield box


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
    """A failure that names the model (quota, not enabled, upstream down), never the gateway."""
    return result.error is not None and model_error_class(result.error) is not None


def context_tokens_for(info: ModelInfo | None) -> int:
    """The prompt budget of a model: its catalog context window, else the default."""
    if info is not None and info.context_window:
        return info.context_window
    return DEFAULT_CONTEXT_TOKENS


async def settle_run_cost(deps: Deps, run_id: UUID) -> Decimal | None:
    """The run's cost: the recorded one, else what the gateway logged for it (stored once).

    None when the gateway cannot be read; the reservation then stays open for M5.9's tick."""
    async with deps.sessions() as s:
        row = await s.get(AgentRun, run_id)
        if row is None:
            return None
        if row.cost_usd is not None:
            return Decimal(row.cost_usd)
    await _await_spend(deps, SPEND_BATCH_S + 1)
    try:
        spend = await deps.gateway.run_spend(run_id)
    except Exception as exc:
        log.warning("run.spend_unreadable", run_id=str(run_id), error=repr(exc))
        return None
    if spend is None:
        return None
    async with deps.sessions() as s, s.begin():
        await record_run_cost(s, run_id, spend, CostSource.GATEWAY)
        stored = await s.scalar(select(AgentRun.cost_usd).where(AgentRun.id == run_id))
        return Decimal(stored) if stored is not None else spend


async def _revoke_stale(deps: Deps, run_id: UUID) -> None:
    """A killed attempt: revoke its key, charge what it spent and release its reservation."""
    try:
        await deps.gateway.revoke_run(run_id)
    except Exception as exc:
        log.warning("run key revoke failed", run_id=str(run_id), error=repr(exc))
    cost = await settle_run_cost(deps, run_id)
    if cost is not None:
        await deps.budgets.release_run(run_id, cost)


def role_engine(deps: Deps, job_id: UUID, role: Role) -> str:
    """The engine the role will run on (M5.8 reads the pinned profile and the selector)."""
    return "fake"


def reviewer_box_name(kind: str, round: int) -> str:
    return f"review-{kind}-r{round}"


def review_attachments(kind: str, screenshots: Sequence[str]) -> tuple[str, ...]:
    """Screenshot paths for the ux and ui reviewers; none for any other kind."""
    if kind not in ("ux", "ui"):
        return ()
    return tuple(screenshot_path(n) for n in screenshots)


_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


async def isolate_instructions(deps: Deps, job_id: UUID, box: Sandbox) -> None:
    """Leave only the default branch's AGENTS.md in the box: engines load these files, so a
    builder's edits to them must not steer a reviewer or approver (the diff still shows them)."""
    found = await box.exec(
        [
            "find",
            REPO,
            "(",
            "-name",
            ".git",
            "-prune",
            ")",
            "-o",
            "(",
            "-not",
            "-type",
            "d",
            "(",
            *[x for n in _INSTRUCTION_FILES for x in ("-name", n, "-o")][:-1],
            ")",
            "-print",
            ")",
        ],
        cwd=REPO,
    )
    for line in found.stdout.splitlines():
        if line.strip():
            await box.remove(line.strip())
    base = await deps.artifacts.get_text(job_id, "base/AGENTS.md")
    if base is not None:
        await box.write_text(f"{REPO}/AGENTS.md", base)


def _parse_contract(contract: str, text: str | None) -> tuple[Verdict | None, Decision | None]:
    """The verdict or decision a finished output carries (None when it has none)."""
    if text is None:
        return None, None
    try:
        if contract == "verdict":
            return parse_verdict(text), None
        if contract == "decision":
            return None, parse_decision(text)
    except ContractError:
        pass
    return None, None


def _runner(defn: Definition, app: AppDef) -> RunnerSpec:
    r = defn.runners.get(app.runner)
    if r is None:
        return RunnerSpec(app.runner, "factory-runner", 2.0, 4096, 512, ())
    return RunnerSpec(
        app.runner, r.image, r.cpus, r.memory_mb, r.pids, r.allowed_hosts, max_work_gb=r.max_work_gb
    )


async def job_sandbox(deps: Deps, job_id: UUID) -> Sandbox:
    """The job's sandbox, with its repository restored when the volume has none (not in prepare)."""
    job, defn, app = await pinned_definition(deps, job_id)
    box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
    if await box.read_bytes(f"{REPO}/.git/HEAD") is None:
        async with running(box):
            await deps.git.prepare(job, app, box)  # restores the kept branch (M4.5)
    return box


async def keep_job_branch(deps: Deps, job_id: UUID) -> None:
    """Push the job's branch to the bare repo; never raises."""
    try:
        job, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        async with running(box):
            await deps.git.keep_branch(job, app, box)
    except Exception as exc:
        log.warning("job.keep_branch_failed", job_id=str(job_id), error=repr(exc))


async def job_head(deps: Deps, job_id: UUID) -> str | None:
    """HEAD of the job's box; None when there is no repository to ask (never raises)."""
    try:
        _, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        async with running(box):
            res = await box.exec(["git", "rev-parse", "HEAD"], cwd=REPO)
        return res.stdout.strip() if res.exit_code == 0 and res.stdout.strip() else None
    except Exception as exc:
        log.warning("job.head_failed", job_id=str(job_id), error=repr(exc))
        return None


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
    deps: Deps,
    box: Sandbox,
    job_id: UUID,
    role: Role,
    round: int,
    tool_policy: ToolPolicy,
    *,
    isolated: bool = False,
) -> None:
    async with deps.sessions() as s, s.begin():
        stale = await fail_stale_runs(s, job_id, role, round)
    for row in stale:
        await _revoke_stale(deps, row.id)
    if stale and tool_policy == ToolPolicy.WRITE and not isolated:
        await box.restart()
        start_sha = next((r.meta["start_sha"] for r in stale if r.meta.get("start_sha")), None)
        if start_sha:
            await box.exec(["git", "reset", "--hard", start_sha], cwd=REPO)
        await box.exec(["git", "clean", "-fdx"], cwd=REPO)


async def _outcome_of(
    deps: Deps, row: AgentRunRow, job_id: UUID, contract: str, *, reused: bool
) -> StepOutcome:
    text = await deps.artifacts.get_text(job_id, row.artifact_path) if row.artifact_path else None
    verdict, decision = _parse_contract(contract, text)
    return StepOutcome(
        row.id,
        RunStatus(row.status),
        text,
        verdict,
        decision,
        reused,
        bool(row.meta.get("diff_cut", False)),
    )


async def build_selection_context(
    deps: Deps, job: Job, role: Role, round: int, box: Sandbox | None
) -> SelectionContext:
    """What the selector sees: sizes of the feature, spec and diff, earlier verdicts, budget."""
    spec_text = await deps.artifacts.get_text(job.id, "spec.md")
    files = lines = 0
    if box is not None and job.base_sha:
        try:
            stat = await deps.git.diff_stat(box, job.base_sha)
            files, lines = stat.files, stat.insertions + stat.deletions
        except Exception as exc:  # no repository to diff yet
            log.info("selection.diff_stat_failed", error=repr(exc))
    async with deps.sessions() as s:
        verdicts = await latest_reviewer_verdicts(s, job.id, before_round=round)
        builder_alias = await last_model_decision(s, job.id, Role.BUILDER.value)
        slug = await s.scalar(select(App.slug).where(App.id == job.app_id))
    family: str | None = None
    if builder_alias is not None:
        info = await deps.catalog.get(builder_alias)
        family = model_family(info.upstream_id) if info is not None else None
    return SelectionContext(
        job_id=job.id,
        app_slug=slug or "",
        role=role,
        round=round,
        feature_chars=len(job.feature),
        spec_chars=len(spec_text) if spec_text is not None else 0,
        diff_files=files,
        diff_lines=lines,
        prior_verdicts=verdicts,
        budget_left_usd=await deps.budgets.remaining_for_job(job.id),
        builder_family=family,
    )


def _daily_held(exc: BudgetExceeded) -> bool:
    """The day is full only because other runs hold reservations that end soon."""
    return exc.scope == "daily" and exc.remaining + exc.reserved >= exc.needed


async def _with_daily_wait[T](fn: Callable[[], Awaitable[T]], deadline: float) -> T:
    """Run a budget check or reservation; wait out a daily stop held only by open reservations."""
    waited = 0
    while True:
        try:
            return await fn()
        except BudgetExceeded as exc:
            if (
                not _daily_held(exc)
                or waited + DAILY_RECHECK_S > DAILY_WAIT_S
                or _monotonic() + DAILY_RECHECK_S > deadline - 60
            ):
                raise
        await _sleep(DAILY_RECHECK_S)
        waited += DAILY_RECHECK_S


@dataclass(frozen=True, slots=True)
class _ModelFailure:
    alias: str
    cls: str


@dataclass(frozen=True, slots=True)
class _Run:
    outcome: StepOutcome
    result: AgentResult | None
    model_failure: _ModelFailure | None = None
    repair: tuple[str, str | None, str] | None = None  # (rejected text, session, problem)


async def _key_spend(deps: Deps, key: str) -> Decimal | None:
    try:
        return await deps.gateway.key_spend(key)
    except Exception as exc:
        log.warning("run.spend_unreadable", error=repr(exc))
        return None


async def _run_cost(
    deps: Deps, key: str, result: AgentResult, info: ModelInfo | None
) -> tuple[Decimal, CostSource]:
    """Step 13: the run's cost from its key's spend, once the gateway has flushed its batch."""
    await _await_spend(deps, SPEND_BATCH_S + 1)
    spend = await _key_spend(deps, key)
    u = result.usage
    tokens = u.input_tokens + u.output_tokens + u.cache_read_tokens
    if spend == 0 and tokens > 0:
        await _await_spend(deps, SPEND_RETRY_S)
        spend = await _key_spend(deps, key)
    priced = info is not None and (info.price_in_per_mtok > 0 or info.price_out_per_mtok > 0)
    if spend is None:
        if result.reported_cost_usd is not None:
            return result.reported_cost_usd, CostSource.ENGINE
        return (estimate_cost(info, u) if info is not None else Decimal(0)), CostSource.ESTIMATE
    if spend == 0 and priced and tokens > 0 and info is not None:
        return estimate_cost(info, u), CostSource.ESTIMATE
    return spend, CostSource.GATEWAY


async def _close_run(
    deps: Deps,
    run_id: UUID,
    key: str | None,
    cost: Decimal | None,
) -> None:
    """Step 14: revoke the key, settle the cost and release the reservation.

    A row left ``running`` (cancel, lease loss, crash) stays so: the next call's step 2 marks
    it failed and restores the checkout. Each part runs even when the one before it failed."""
    if key is not None:
        try:
            await deps.gateway.revoke(key)
        except Exception as exc:  # the key expires by its TTL; finalize revokes the job's keys
            log.warning("run key revoke failed", run_id=str(run_id), error=repr(exc))
    if cost is None:
        if key is None:  # nothing was minted, so nothing was spent
            cost = Decimal(0)
            try:
                async with deps.sessions() as s, s.begin():
                    await record_run_cost(s, run_id, cost, CostSource.GATEWAY)
            except Exception as exc:
                log.warning("run.cost_failed", run_id=str(run_id), error=repr(exc))
        else:
            try:
                cost = await settle_run_cost(deps, run_id)
            except Exception as exc:
                log.warning("run.settle_failed", run_id=str(run_id), error=repr(exc))
    if cost is not None:
        try:
            await deps.budgets.release_run(run_id, cost)
        except Exception as exc:
            log.warning("run.release_failed", run_id=str(run_id), error=repr(exc))


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
    engine: str | None = None,
    choice: ModelChoice | None = None,
    sandbox: Sandbox | None = None,
    fan_out: int = 1,
) -> StepOutcome:
    if tool_policy == ToolPolicy.READ_RUN and sandbox is None:
        raise ValueError("read_run requires an isolated sandbox")
    with log_context(role=role.value):
        job, defn, app = await pinned_definition(deps, job_id)
        slug = next(k for k, v in defn.factory.apps.items() if v is app)
        spec = deps.roles.get(role, definition_sha=job.definition_sha)
        tool_policy = effective_tool_policy(spec.tool_policy, tool_policy)
        isolated = sandbox is not None
        # an isolated box is already running and owned by the caller
        box = sandbox if sandbox is not None else await job_sandbox(deps, job_id)
        async with running(box) if not isolated else _nullcontext(box):
            return await _run_in_box(
                deps,
                box,
                engine=engine,
                choice=choice,
                fan_out=fan_out,
                isolated=isolated,
                job=job,
                app=app,
                role=role,
                round=round,
                inputs=inputs,
                output_name=output_name,
                workdir=workdir,
                tool_policy=tool_policy,
                attachments=attachments,
                occurrence=occurrence,
                spec=spec,
                defn=defn,
                slug=slug,
            )


async def _run_in_box(
    deps: Deps,
    box: Sandbox,
    *,
    job: Job,
    app: AppDef,
    role: Role,
    round: int,
    inputs: Sequence[str],
    output_name: str,
    workdir: str | None,
    tool_policy: ToolPolicy,
    attachments: Sequence[str],
    occurrence: int,
    spec: RoleSpec,
    defn: Definition,
    slug: str,
    engine: str | None = None,
    choice: ModelChoice | None = None,
    fan_out: int = 1,
    isolated: bool = False,
) -> StepOutcome:
    job_id = job.id
    defaults = defn.factory.defaults
    key = TIMEOUT_KEY[role]
    head: str | None = await _head(box) if role in _HEAD_ROLES else None
    found = await _find_finished(deps, job_id, role, round, occurrence, head)
    if found is not None:
        return await _outcome_of(deps, found, job_id, spec.contract, reused=True)
    await _cleanup_stale(deps, box, job_id, role, round, tool_policy, isolated=isolated)

    deadline = _monotonic() + step_budget_s(defaults, key)
    outcome: StepOutcome | None = None
    failed: set[str] = set()  # models that failed in this step (L15)
    fell_back = False
    n = 0  # transient retries used
    while True:
        left = int(deadline - _monotonic())
        if n > 0 and left < MIN_ATTEMPT_S:
            break
        run = await _attempt(
            deps,
            box,
            job=job,
            app=app,
            defn=defn,
            role=role,
            round=round,
            inputs=inputs,
            output_name=output_name,
            workdir=workdir,
            tool_policy=tool_policy,
            attachments=tuple(attachments),
            timeout_s=min(step_timeout_s(defn, key), left),
            head=head,
            spec=spec,
            app_slug=slug,
            max_items=defaults.max_backlog_items,
            engine=engine,
            choice=choice,
            exclude=failed,
            fan_out=fan_out,
            deadline=deadline,
        )
        outcome = run.outcome
        if run.model_failure is not None:
            # L15: one retry on another model; a model failure is never retried on the same one
            if choice is not None or fell_back:
                raise NoModelAvailable("no model available")
            failed.add(run.model_failure.alias)
            fell_back = True
            if run.model_failure.cls == "quota":
                await _sleep(min(_backoff(defaults, 0), MAX_QUOTA_BACKOFF_S))
            continue
        result = run.result
        if result is None or not is_transient(
            result, is_model_unavailable=lambda r: _is_model_unavailable(deps, r)
        ):
            break
        n += 1
        if n > defaults.engine_retries:
            break
        await _sleep(_backoff(defaults, n - 1))
    assert outcome is not None
    return outcome


async def _attempt(
    deps: Deps,
    box: Sandbox,
    *,
    job: Job,
    app: AppDef,
    defn: Definition,
    role: Role,
    round: int,
    inputs: Sequence[str],
    output_name: str,
    workdir: str | None,
    tool_policy: ToolPolicy,
    attachments: tuple[str, ...],
    timeout_s: int,
    head: str | None,
    spec: RoleSpec,
    app_slug: str,
    max_items: int,
    engine: str | None = None,
    choice: ModelChoice | None = None,
    exclude: Collection[str] = (),
    fan_out: int = 1,
    deadline: float = float("inf"),
) -> _Run:
    job_id = job.id
    # step 3: the budget gate, before any model is chosen or key minted
    await _with_daily_wait(lambda: deps.budgets.check_before_step(job_id, role), deadline)
    # step 4: selection (a preselected choice skips it)
    picked = choice
    if picked is None:
        profile, _ = resolve_profile(
            defn,
            app,
            override=job.profile_name,
            triage_profile=(job.triage or {}).get("profile_name"),
        )
        sel_ctx = await build_selection_context(deps, job, role, round, box)
        picked = await deps.selector.choose(profile, sel_ctx, exclude=set(exclude))
    engine_name = engine or picked.engine
    info = await deps.catalog.get(picked.model.alias)
    models = [picked.model.alias]
    if engine_name == "claude_code":
        for alias in defn.factory.defaults.claude_classifier_fallbacks:
            extra = await deps.catalog.get(alias)
            if extra is not None and extra.available and alias not in models:
                models.append(alias)

    # inputs: the explicit ones plus the artifacts that match the role's patterns
    known = [m.path for m in await deps.artifacts.list(job_id)]
    matched = [p for p in known if any(fnmatch.fnmatchcase(p, g) for g in spec.inputs)]
    paths = list(dict.fromkeys([*inputs, *matched]))
    await deps.artifacts.materialize(job_id, box, paths)
    texts: dict[str, str] = {}
    for p in paths:
        t = await deps.artifacts.get_text(job_id, p)
        if t is not None:
            texts[p] = t

    base_meta: dict[str, Any] = {}
    run_head = head
    if tool_policy == ToolPolicy.WRITE or (role in _RECORD_HEAD_ROLES and run_head is None):
        run_head = await _head(box)
    if tool_policy == ToolPolicy.WRITE:
        base_meta["start_sha"] = run_head
    if role not in _RECORD_HEAD_ROLES:
        run_head = None

    prev: tuple[str, str] | None = None  # (rejected text, problem) for the repair run
    prev_session: str | None = None
    repairs = 0
    while True:
        run = await _run_once(
            deps,
            box,
            job=job,
            defn=defn,
            role=role,
            round=round,
            output_name=output_name,
            workdir=workdir,
            tool_policy=tool_policy,
            attachments=attachments,
            timeout_s=timeout_s,
            spec=spec,
            app_slug=app_slug,
            max_items=max_items,
            engine_name=engine_name,
            choice=picked,
            info=info,
            models=models,
            texts=texts,
            run_head=run_head,
            base_meta=base_meta,
            fan_out=fan_out,
            deadline=deadline,
            prev=prev,
            prev_session=prev_session,
            repairs=repairs,
        )
        if run.repair is None:
            return run
        repairs += 1
        prev, prev_session = (run.repair[0], run.repair[2]), run.repair[1]


async def _run_once(
    deps: Deps,
    box: Sandbox,
    *,
    job: Job,
    defn: Definition,
    role: Role,
    round: int,
    output_name: str,
    workdir: str | None,
    tool_policy: ToolPolicy,
    attachments: tuple[str, ...],
    timeout_s: int,
    spec: RoleSpec,
    app_slug: str,
    max_items: int,
    engine_name: str,
    choice: ModelChoice,
    info: ModelInfo | None,
    models: list[str],
    texts: dict[str, str],
    run_head: str | None,
    base_meta: dict[str, Any],
    fan_out: int,
    deadline: float,
    prev: tuple[str, str] | None,
    prev_session: str | None,
    repairs: int,
) -> _Run:
    """One run: its own id, reservation, key and row; closed in the ``finally``."""
    job_id = job.id
    defaults = defn.factory.defaults

    async def reserve() -> tuple[UUID, int, Decimal]:
        # step 5: id first, then the reservation and the running row in one transaction
        async with deps.sessions() as s, s.begin():
            new_id: UUID = (await s.execute(text("SELECT uuidv7()"))).scalar_one()
            cap = await deps.budgets.reserve_run(
                job_id, run_id=new_id, role=role, round=round, fan_out=fan_out, s=s
            )
            rid, n = await start_agent_run(
                s,
                job_id=job_id,
                role=role.value,
                round=round,
                engine=engine_name,
                model_alias=choice.model.alias,
                head_sha=run_head,
                meta=base_meta,
                runner_digest=box.image_digest,
                run_id=new_id,
                status=RunStatus.RUNNING.value,
                decision_id=choice.decision_id,
            )
        return rid, n, cap

    run_id, attempt, cap = await _with_daily_wait(reserve, deadline)
    key: str | None = None
    cost: Decimal | None = None
    try:
        with log_context(run_id=run_id):
            run_key: str = await deps.gateway.mint_run_key(
                run_id=run_id,
                models=models,
                max_budget_usd=cap,
                tags={
                    "app": app_slug,
                    "job": str(job_id),
                    "role": role.value,
                    "round": str(round),
                    "run": str(run_id),
                },
                ttl_s=timeout_s + 300,
            )
            key = run_key
            out_path = f"{OUT}/{run_id}.md"
            writes = tool_policy == ToolPolicy.WRITE
            ctx = PromptContext(
                job_id=job_id,
                app_slug=app_slug,
                round=round,
                inputs=texts,
                output_path=out_path if writes else None,
                workdir=workdir,
                context_tokens=context_tokens_for(info),
            )
            system, prompt = render_prompt(spec, ctx)
            run_meta: dict[str, Any] = {}
            diff_cut = False
            if "diff.patch" in texts:
                diff_cut = diff_was_cut(prompt)
                run_meta["diff_cut"] = diff_cut
            resume_session: str | None = None
            if prev is not None:
                repair = REPAIR_PROMPT.format(
                    problem=prev[1],
                    target=out_path if writes else "your reply",
                    rule=_RULES.get(spec.contract, "It must not be empty."),
                )
                if prev_session:
                    prompt, resume_session = repair, prev_session
                else:
                    prompt = (
                        f"{prompt}\n\n{fence_untrusted('previous-output', prev[0])}\n\n{repair}"
                    )
            task = AgentTask(
                job_id=job_id,
                run_id=run_id,
                role=role,
                round=round,
                attempt=attempt,
                engine=engine_name,
                model=choice.model,
                system_prompt=system,
                prompt=prompt,
                workdir=workdir,
                output_path=out_path,
                tool_policy=tool_policy,
                attachments=attachments,
                max_turns=max_turns_for(defaults, role.value),
                timeout_s=timeout_s,
                resume_session=resume_session,
                gateway_base_url=deps.settings.gateway_sandbox_url,
                gateway_key=run_key,
                tags={
                    "app": app_slug,
                    "job": str(job_id),
                    "role": role.value,
                    "round": str(round),
                    "run": str(run_id),
                },
                max_budget_usd=cap,
            )
            error: str | None = None
            text_out: str | None = None
            status: RunStatus
            result: AgentResult | None = None
            try:
                result = await get_engine(engine_name).run(task, box)
                status, error = result.status, result.error
            except Exception as exc:  # recorded, never raised
                log.warning("engine.error", error=str(exc))
                status, error = RunStatus.FAILED, str(exc)
            pending: tuple[Decimal, CostSource] | None = None
            if result is not None:
                pending = await _run_cost(deps, run_key, result, info)
            model_failure: _ModelFailure | None = None
            if status == RunStatus.FAILED and error:
                cls = model_error_class(error)
                if cls is not None:  # L15: the model, not the gateway
                    first = error.strip().splitlines()[0][:300] if error.strip() else cls
                    try:
                        await deps.catalog.mark_unavailable(choice.model.alias, cls, first)
                    except Exception as exc:
                        log.warning("catalog.mark_unavailable_failed", error=repr(exc))
                    error = f"model unavailable: {cls}: {first}"
                    model_failure = _ModelFailure(choice.model.alias, cls)
            if writes:
                if result is not None and status == RunStatus.OK:
                    text_out = await box.read_text(out_path)
            else:
                # code a read-only role ran could have planted a file here: never read it
                await box.remove(out_path)
                if result is not None and status == RunStatus.OK:
                    text_out = result.output
            verdict: Verdict | None = None
            decision: Decision | None = None
            artifact_path: str | None = None
            problem: str | None = None
            if status == RunStatus.OK:
                problem = check_output(spec.contract, text_out, max_items=max_items)
                if problem is None:
                    verdict, decision = _parse_contract(spec.contract, text_out)
                    await deps.artifacts.put(job_id, output_name, text_out or "", run_id=run_id)
                    artifact_path = output_name
                else:
                    status, error = RunStatus.FAILED, f"contract: {problem}"
            final = problem is None or repairs >= CONTRACT_REPAIRS
            if problem is not None and final and spec.contract == "verdict":
                verdict = Verdict.ERROR
            usage = result.usage if result is not None else Usage()
            async with deps.sessions() as s, s.begin():
                await lock_job(s, job_id)  # before the run row, as a cancel does
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
                if run_meta:
                    row = await s.get(AgentRun, run_id)
                    if row is not None:
                        row.meta = {**row.meta, **run_meta}
                if pending is not None:
                    await record_run_cost(s, run_id, pending[0], pending[1])
            if pending is not None:
                cost = pending[0]
            if status == RunStatus.BUDGET:
                raise BudgetExceeded("run", Decimal(0), Decimal(0))
            if problem is not None and not final:
                session = result.session_id if result is not None else None
                return _Run(
                    StepOutcome(run_id, status, None, verdict, decision, False, diff_cut),
                    result,
                    model_failure,
                    repair=(text_out or "", session, problem),
                )
            if problem is not None and text_out:
                stem = output_name[:-3] if output_name.endswith(".md") else output_name
                await deps.artifacts.put(job_id, f"{stem}.invalid.md", text_out, run_id=run_id)
            if status != RunStatus.OK:
                text_out = None
            return _Run(
                StepOutcome(run_id, status, text_out, verdict, decision, False, diff_cut),
                result,
                model_failure,
            )
    finally:
        await _close_run(deps, run_id, key, cost)
