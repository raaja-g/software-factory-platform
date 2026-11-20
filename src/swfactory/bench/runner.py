"""Benchmark runs: the estimate shown before a run and the estimate-gated start (section 12)."""

from __future__ import annotations

import json
import shlex
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.bench.reference import (
    reference_branch,
    reference_source_ref,
    task_check_key,
)
from swfactory.bench.sanitize import sanitize_tree
from swfactory.bench.tasks import app_for_repo
from swfactory.costs.pricing import STEP_MIN_USD
from swfactory.db.repo import JobRefError, calibration_table, resolve_job_ref
from swfactory.db.tables import (
    AgentRun,
    BenchResult,
    BenchRun,
    BenchTaskCheck,
    Job,
    JobEvent,
    Score,
)
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef, Definition
from swfactory.domain.types import TERMINAL_STAGES, JobKind, Role, RunStatus, Stage
from swfactory.gitops.service import SAFE_GIT_ENV, safe_git
from swfactory.queue import enqueue_job
from swfactory.sandbox.base import RunnerSpec, running
from swfactory.sandbox.paths import REPO
from swfactory.scoring.calibration import counted_judge_rows
from swfactory.scoring.objective import ScoreRow
from swfactory.verify.gate import TEST_TIMEOUT_S

log = structlog.get_logger(__name__)

HISTORY_DAYS = 90
MIN_HISTORY = 3
_FINISHED = (Stage.DELIVERED.value, Stage.MERGED.value, Stage.CLOSED.value)


def bench_branch(run_id: UUID, task: str, config: str, repetition: int = 1) -> str:
    run8 = run_id.hex[-8:]
    suffix = config if repetition == 1 else f"{config}-r{repetition}"
    return f"bench/{run8}/{task}/{suffix}"


@dataclass(frozen=True, slots=True)
class Estimate:
    trials: int
    total_usd: Decimal
    max_usd: Decimal
    per_config: Mapping[str, Decimal]
    basis: Mapping[str, str]


def estimate_run(
    suite: BenchSuiteDef,
    configs: Sequence[BenchConfigDef],
    *,
    repetitions: int,
    history: Mapping[str, Sequence[Decimal]],
    source_costs: Mapping[str, Decimal],
    job_cap: Decimal,
    judge_step_usd: Decimal,
    default_scorers: int,
) -> Estimate:
    # A source job's cost describes the unmodified profile; a configuration that overrides roles
    # or triage modes would run differently, so for it the estimate falls back to the job cap.
    judge = {
        name: (len(t.scorers) or default_scorers) * judge_step_usd
        for name, t in suite.tasks.items()
    }
    per_config: dict[str, Decimal] = {}
    basis: dict[str, str] = {}
    total = Decimal(0)
    max_usd = Decimal(0)
    trials = 0
    for cfg in configs:
        hist = list(history.get(cfg.profile, ()))
        median = Decimal(str(statistics.median(hist))) if len(hist) >= MIN_HISTORY else None
        used: set[str] = set()
        cost = Decimal(0)
        for name in suite.tasks:
            if median is not None:
                base = median
                used.add("history")
            elif name in source_costs and not (cfg.roles or cfg.triage_modes):
                base = source_costs[name]
                used.add("source job cost")
            else:
                base = job_cap
                used.add("job budget cap")
            cost += (base + judge[name]) * repetitions
            max_usd += (job_cap + judge[name]) * repetitions
            trials += repetitions
        per_config[cfg.name] = cost
        total += cost
        basis[cfg.name] = ", ".join(sorted(used)) if used else "job budget cap"
    return Estimate(trials, total, max_usd, per_config, basis)


class EstimateChanged(Exception):
    """The estimate differs from the one the person accepted."""

    def __init__(self, current: Decimal, accepted: Decimal):
        super().__init__(f"estimate is now {current} USD, not the accepted {accepted} USD")
        self.current = current
        self.accepted = accepted


async def _history(s: AsyncSession, profiles: set[str]) -> dict[str, list[Decimal]]:
    since = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)
    out: dict[str, list[Decimal]] = {p: [] for p in profiles}
    if not profiles:
        return out
    rows = await s.execute(
        select(Job.profile_name, Job.cost_usd).where(
            Job.profile_name.in_(profiles),
            Job.kind == JobKind.NORMAL.value,
            Job.stage.in_(_FINISHED),
            Job.finished_at >= since,
        )
    )
    for name, cost in rows:
        if name is not None:
            out[name].append(Decimal(cost))
    return out


async def _source_costs(s: AsyncSession, suite: BenchSuiteDef) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for name, task in suite.tasks.items():
        if not task.source_job:
            continue
        try:
            job = await resolve_job_ref(s, task.source_job)
        except JobRefError:
            continue
        out[name] = Decimal(job.cost_usd)
    return out


def _chosen_configs(
    sdef: BenchSuiteDef,
    suite: str,
    configs: Sequence[str | BenchConfigDef] | None,
    *,
    defn: Definition | None = None,
) -> list[BenchConfigDef]:
    """Suite configurations by name, plus inline ``BenchConfigDef`` values revalidated (M10.7)."""
    by_name = {c.name: c for c in sdef.configurations}
    if configs is None:
        return list(sdef.configurations)
    out: dict[str, BenchConfigDef] = {}
    unknown: list[str] = []
    for c in configs:
        if isinstance(c, BenchConfigDef):
            c = BenchConfigDef.model_validate(c.model_dump(mode="json"))
            if c.name in out:
                raise ValueError(f"duplicate configuration name '{c.name}'")
            if defn is not None and c.profile not in defn.profiles:
                raise ValueError(f"configuration '{c.name}': unknown profile '{c.profile}'")
            out[c.name] = c
        elif c in by_name:
            out.setdefault(c, by_name[c])
        else:
            unknown.append(c)
    if unknown:
        raise ValueError(f"unknown configuration(s) {', '.join(unknown)} in suite '{suite}'")
    return list(out.values())


async def compute_estimate(
    s: AsyncSession,
    defn: Definition,
    *,
    suite: str,
    configs: Sequence[str | BenchConfigDef] | None,
    repetitions: int,
) -> tuple[BenchSuiteDef, list[BenchConfigDef], Estimate]:
    """The estimate for a run as it would start now (the same numbers ``start`` checks)."""
    sdef = defn.benchmarks.get(suite)
    if sdef is None:
        raise ValueError(f"no such benchmark suite '{suite}'")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    chosen = _chosen_configs(sdef, suite, configs, defn=defn)
    est = estimate_run(
        sdef,
        chosen,
        repetitions=repetitions,
        history=await _history(s, {c.profile for c in chosen}),
        source_costs=await _source_costs(s, sdef),
        job_cap=defn.factory.budgets.job_usd,
        judge_step_usd=STEP_MIN_USD[Role.JUDGE],
        default_scorers=len(defn.scorers),
    )
    return sdef, chosen, est


async def start_bench_run(
    s: AsyncSession,
    defn: Definition,
    *,
    suite: str,
    configs: Sequence[str | BenchConfigDef] | None,
    repetitions: int,
    budget_usd: Decimal | None,
    definition_sha: str,
    accept_estimate_usd: Decimal,
    requested_by: str,
) -> UUID:
    sdef, chosen, est = await compute_estimate(
        s, defn, suite=suite, configs=configs, repetitions=repetitions
    )
    if est.total_usd != accept_estimate_usd:
        raise EstimateChanged(est.total_usd, accept_estimate_usd)
    run = BenchRun(
        suite=suite,
        configurations=[c.model_dump(mode="json") for c in chosen],
        definition_sha=definition_sha,
        status="running",
        budget_usd=budget_usd if budget_usd is not None else sdef.budget_usd,
        estimate_usd=est.total_usd,
        repetitions=repetitions,
        max_parallel=sdef.max_parallel,
        requested_by=requested_by,
    )
    s.add(run)
    await s.flush()
    for task in sdef.tasks:
        for cfg in chosen:
            overrides = {
                "roles": {r.value: o.model_dump(mode="json") for r, o in cfg.roles.items()},
                "triage_modes": {q: m.value for q, m in cfg.triage_modes.items()},
                "laya_heads": dict(cfg.laya_heads),
            }
            for rep in range(1, repetitions + 1):
                s.add(
                    BenchResult(
                        run_id=run.id,
                        task=task,
                        configuration=cfg.name,
                        repetition=rep,
                        status="pending",
                        overrides=overrides,
                    )
                )
    await s.commit()
    return run.id


async def _label_bench(s: AsyncSession, run_id: UUID) -> None:
    """Label the run's tier decisions; a failure never blocks the run (L13)."""
    try:
        from swfactory.laya.export import label_bench_outcomes

        async with s.begin_nested():
            await label_bench_outcomes(s, run_id)
    except Exception as exc:
        log.warning("laya.bench_label_failed", run_id=str(run_id), error=repr(exc))


TICK_LOCK = "select pg_try_advisory_xact_lock(hashtext('bench_tick'))"


def _source_ref(run: UUID, task: str, config: str, rep: int) -> str:
    return f"bench:{run}:{task}:{config}:{rep}"


class BenchRunner:
    """Starts benchmark trials one tick at a time and keeps the run under its hard cap."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], definitions: Any) -> None:
        self.sessions = sessions
        self.definitions = definitions

    async def _definition(self, sha: str | None) -> Definition:
        if sha:
            found = await self.definitions.get(sha)
            if found is not None:
                return found
        return (await self.definitions.active())[1]

    async def tick(self, now: datetime) -> list[UUID]:
        """One pass over all running runs; returns the ids of the jobs it enqueued."""
        started: list[UUID] = []
        async with self.sessions() as lock, lock.begin():
            if not await lock.scalar(text(TICK_LOCK)):
                return started
            runs = (
                await lock.scalars(
                    select(BenchRun)
                    .where(BenchRun.status.in_(("running", "cancelled")))
                    .order_by(BenchRun.created_at, BenchRun.id)
                )
            ).all()
            for run in runs:
                started.extend(await self._tick_run(lock, run, now))
        return started

    async def _tick_run(self, s: AsyncSession, run: BenchRun, now: datetime) -> list[UUID]:
        results = (await s.scalars(select(BenchResult).where(BenchResult.run_id == run.id))).all()
        by_job = {
            j.id: j
            for j in (
                await s.scalars(
                    select(Job).where(Job.id.in_([r.job_id for r in results if r.job_id]))
                )
            ).all()
        }
        for r in results:
            job = by_job.get(r.job_id) if r.job_id else None
            if r.status == "running" and job is not None and _job_over(job):
                r.status = "done"
        if run.status == "cancelled":
            for r in results:
                if r.status == "pending":
                    r.status = "skipped_budget"
            await s.flush()
            return []
        defn = await self._definition(run.definition_sha)
        job_cap = defn.factory.budgets.job_usd
        order = _trial_order(run, defn)
        pending = sorted(
            (r for r in results if r.status == "pending"),
            key=lambda r: order(r),
        )
        running = sum(1 for r in results if r.status == "running")
        checks = await self._check_states(s, run, defn, {r.task for r in pending})
        for r in [r for r in pending if checks.get(r.task) == "invalid"]:
            r.status = "skipped_invalid"
            pending.remove(r)
        running += sum(1 for c in checks.values() if c == "active")
        spent = await _run_spend(s, run.id)
        started: list[UUID] = []
        while pending:
            if run.budget_usd is not None and (
                run.budget_usd - spent - job_cap * running < job_cap
            ):
                for r in pending:
                    r.status = "skipped_budget"
                pending = []
                if running == 0:
                    run.status = "stopped_budget"
                    run.finished_at = now
                break
            if running >= run.max_parallel:
                break
            ready = next((r for r in pending if checks.get(r.task) == "valid"), None)
            if ready is not None:
                pending.remove(ready)
                job = await self._start_trial(s, run, ready, defn, job_cap)
            else:
                unchecked = next((r for r in pending if checks.get(r.task) == "none"), None)
                if unchecked is None:
                    break  # every pending task waits for its reference job
                job = await self._start_check(s, run, unchecked.task, defn, job_cap)
                checks[unchecked.task] = "active"
            running += 1
            started.append(job.id)
        run.cost_usd = spent
        if run.status == "running" and not pending and running == 0:
            if all(r.status not in ("pending", "running") for r in results):
                capped = any(r.status == "skipped_budget" for r in results)
                run.status = "stopped_budget" if capped else "done"
                run.finished_at = now
                if run.status == "done":
                    await _label_bench(s, run.id)
        await s.flush()
        return started

    async def _check_states(
        self, s: AsyncSession, run: BenchRun, defn: Definition, tasks: set[str]
    ) -> dict[str, str]:
        """Per task: valid, invalid, active (a reference job is running), waiting, or none."""
        suite = defn.benchmarks.get(run.suite)
        out: dict[str, str] = {}
        if suite is None:
            return out
        keys = {t: task_check_key(run.suite, suite.tasks[t]) for t in tasks if t in suite.tasks}
        rows = {
            row.check_key: row
            for row in (
                await s.scalars(
                    select(BenchTaskCheck).where(BenchTaskCheck.check_key.in_(list(keys.values())))
                )
            ).all()
        }
        for t, key in keys.items():
            row = rows.get(key)
            if row is None:
                out[t] = "none"
            elif row.status in ("valid", "invalid"):
                out[t] = row.status
            else:
                job = await s.scalar(select(Job).where(Job.source_ref == reference_source_ref(key)))
                if job is not None and _job_over(job):
                    row.status = "invalid"
                    row.detail = {"reason": "the reference job ended without a verdict"}
                    row.finished_at = datetime.now(UTC)
                    out[t] = "invalid"
                else:
                    out[t] = "active"
        return out

    async def _start_check(
        self, s: AsyncSession, run: BenchRun, task_name: str, defn: Definition, job_cap: Decimal
    ) -> Job:
        """Enqueue the reference job of a task (or adopt the one a crashed tick left)."""
        task = defn.benchmarks[run.suite].tasks[task_name]
        key = task_check_key(run.suite, task)
        app = app_for_repo(defn, task.repo)
        slug = next(k for k, v in defn.factory.apps.items() if v is app or v == app)
        ref = reference_source_ref(key)
        async with self.sessions() as work, work.begin():
            job = await work.scalar(select(Job).where(Job.source_ref == ref))
            if job is None:
                job = await enqueue_job(
                    work,
                    app_slug=slug,
                    feature=task.feature,
                    title=f"check {task_name}",
                    source="api",
                    source_ref=ref,
                    requested_by=run.requested_by or "bench",
                    kind=JobKind.BENCHMARK,
                    profile_name=run.configurations[0]["profile"],
                    budget_usd=job_cap,
                    branch=reference_branch(key, task_name),
                    base_sha=task.base_sha,
                    definition_sha=run.definition_sha,
                )
            await work.execute(
                pg_insert(BenchTaskCheck)
                .values(
                    check_key=key, suite=run.suite, task=task_name, job_id=job.id, status="pending"
                )
                .on_conflict_do_update(
                    index_elements=["check_key"], set_={"job_id": job.id, "status": "pending"}
                )
            )
        return job

    async def _start_trial(
        self, s: AsyncSession, run: BenchRun, r: BenchResult, defn: Definition, job_cap: Decimal
    ) -> Job:
        suite = defn.benchmarks[run.suite]
        task = suite.tasks[r.task]
        cfg = next(c for c in run.configurations if c["name"] == r.configuration)
        app = app_for_repo(defn, task.repo)
        slug = next(k for k, v in defn.factory.apps.items() if v is app or v == app)
        ref = _source_ref(run.id, r.task, r.configuration, r.repetition)
        # The enqueue and the result update share one transaction on their own session.
        async with self.sessions() as work, work.begin():
            job = await work.scalar(select(Job).where(Job.source_ref == ref))
            if job is None:
                job = await enqueue_job(
                    work,
                    app_slug=slug,
                    feature=task.feature,
                    title=f"bench {r.task} [{r.configuration}]",
                    source="api",
                    source_ref=ref,
                    requested_by=run.requested_by or "bench",
                    kind=JobKind.BENCHMARK,
                    profile_name=cfg["profile"],
                    budget_usd=job_cap,
                    branch=bench_branch(run.id, r.task, r.configuration, r.repetition),
                    base_sha=task.base_sha,
                    definition_sha=run.definition_sha,
                )
            await work.execute(
                update(BenchResult)
                .where(BenchResult.id == r.id)
                .values(status="running", job_id=job.id)
            )
        r.status = "running"
        r.job_id = job.id
        return job


def _job_over(job: Job) -> bool:
    return job.stage in {st.value for st in TERMINAL_STAGES} or job.stage == Stage.FAILED.value


def _trial_order(run: BenchRun, defn: Definition) -> Callable[[BenchResult], tuple[int, int, int]]:
    suite = defn.benchmarks.get(run.suite)
    tasks = list(suite.tasks) if suite is not None else []
    cfgs = [c["name"] for c in run.configurations]

    def key(r: BenchResult) -> tuple[int, int, int]:
        t = tasks.index(r.task) if r.task in tasks else len(tasks)
        c = cfgs.index(r.configuration) if r.configuration in cfgs else len(cfgs)
        return (t, c, r.repetition)

    return key


async def _run_spend(s: AsyncSession, run_id: UUID) -> Decimal:
    """Agent-run cost (judges included) of every benchmark job of the run, adopted or not."""
    total = await s.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0))
        .join(Job, Job.id == AgentRun.job_id)
        .where(Job.kind == JobKind.BENCHMARK.value, Job.source_ref.like(f"bench:{run_id}:%"))
    )
    return Decimal(total or 0)


# --- benchmark trials: hidden checks and the recorded result (M10.11) ------------------------

HIDDEN_CHECKS_BOX = "hidden-checks"
HIDDEN_LOG = "bench/hidden-checks.log"
NO_BUILD = "no verified build to check"
LOG_TAIL_CHARS = 4000


@dataclass(frozen=True, slots=True)
class HiddenResult:
    passed: bool | None
    files: Mapping[str, int | None]
    base_files: Mapping[str, int | None]
    error: str | None
    sanitized: tuple[str, ...] = ()  # paths the sanitizer took out of the agent's tree


def _detail(res: HiddenResult) -> str:
    out: dict[str, Any] = {"files": dict(res.files)}
    if res.sanitized:
        out["sanitized"] = list(res.sanitized)
    if res.base_files:
        out["base_files"] = dict(res.base_files)
    if res.error is not None:
        out["error"] = res.error
    return json.dumps(out, sort_keys=True)


async def _has_verified_build(deps: Any, job_id: UUID) -> bool:
    """A builder run finished OK and the gate ran on it (it writes verify.md)."""
    async with deps.sessions() as s:
        built = await s.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.job_id == job_id,
                AgentRun.role == Role.BUILDER.value,
                AgentRun.status == RunStatus.OK.value,
            )
        )
    if not built:
        return False
    return await deps.artifacts.get_text(job_id, "verify.md") is not None


async def _run_files(box: Any, task: BenchTaskDef, paths: Sequence[str], out: list[str]) -> dict:
    codes: dict[str, int | None] = {}
    for path in paths:
        cmd = task.check_command.replace("{file}", shlex.quote(path))
        res = await box.exec(["bash", "-c", cmd], cwd=REPO, timeout_s=TEST_TIMEOUT_S)
        codes[path] = res.exit_code
        out.append(f"$ {cmd}\n# exit {res.exit_code}\n{res.stdout}{res.stderr}")
    return codes


async def run_hidden_checks(
    deps: Any,
    job_id: UUID,
    task: BenchTaskDef,
    *,
    runner: RunnerSpec,
    fresh: bool = False,
    also_base: bool = False,
    log_path: str = HIDDEN_LOG,
) -> HiddenResult:
    """Run the task's check files, one by one, in a sanitized copy of the verified tree.

    Never raises. The builder's checkout is never written; the command is the task's own
    ``check_command``. The copy is what the agent committed, so before the check files are
    written ``sanitize_tree`` restores or removes the files that could take over the test runner
    (see there, including what it leaves alone); what it changed is recorded in the result, the
    ``hidden_detail`` JSON (``"sanitized"``) and the log, and does not by itself fail the trial.
    The command runs under a plain ``bash -c``: no login profile of the box is read.
    """
    try:
        if not fresh and not await _has_verified_build(deps, job_id):
            return await _store(deps, job_id, HiddenResult(False, {}, {}, NO_BUILD), [], log_path)
    except Exception as exc:
        return await _store(
            deps,
            job_id,
            HiddenResult(None, {}, {}, f"hidden checks could not run: {exc}"),
            [],
            log_path,
        )
    logs: list[str] = []
    files: dict[str, int | None] = {}
    base_files: dict[str, int | None] = {}
    sanitized: list[str] = []
    try:
        paths = sorted(task.check_files)
        async with await _checks_box(deps, job_id, runner, fresh) as iso:
            sanitized = await sanitize_tree(iso, task.base_sha, set(task.check_files))
            logs.extend(f"sanitized: {path}" for path in sanitized)
            for path in paths:
                await iso.write_text(f"{REPO}/{path}", task.check_files[path])
            files = await _run_files(iso, task, paths, logs)
            if also_base:
                res = await iso.exec(
                    safe_git("checkout", "--detach", "--force", task.base_sha),
                    cwd=REPO,
                    env=SAFE_GIT_ENV,
                    timeout_s=300,
                )
                if res.exit_code != 0:
                    raise RuntimeError(f"base checkout failed: {res.stderr.strip()}")
                for path in paths:  # the base tree lacks the check files: put them back
                    await iso.write_text(f"{REPO}/{path}", task.check_files[path])
                base_files = await _run_files(iso, task, paths, logs)
        passed = bool(files) and all(code == 0 for code in files.values())
        result = HiddenResult(passed, files, base_files, None, tuple(sanitized))
    except Exception as exc:
        log.warning("bench.hidden_checks_failed", job_id=str(job_id), error=str(exc))
        result = HiddenResult(
            None, files, base_files, f"hidden checks could not run: {exc}", tuple(sanitized)
        )
    return await _store(deps, job_id, result, logs, log_path)


async def _checks_box(deps: Any, job_id: UUID, runner: RunnerSpec, fresh: bool) -> Any:
    """The checks box: a copy of the verified tree, or (no gate ran) of the branch head."""
    if not fresh:
        return deps.sandboxes.isolated(job_id, runner, name=HIDDEN_CHECKS_BOX)
    job_box = await deps.sandboxes.ensure(job_id, runner)
    async with running(job_box):
        res = await job_box.exec(
            safe_git("rev-parse", "HEAD"), cwd=REPO, env=SAFE_GIT_ENV, timeout_s=60
        )
    head = res.stdout.strip()
    if res.exit_code != 0 or not head:
        raise RuntimeError(f"cannot read the branch head: {res.stderr.strip()}")
    return deps.sandboxes.verify_box(job_id, runner, name=HIDDEN_CHECKS_BOX, head=head)


async def _store(
    deps: Any, job_id: UUID, res: HiddenResult, logs: list[str], log_path: str = HIDDEN_LOG
) -> HiddenResult:
    """Write hidden_passed, hidden_detail and the log; a failure to store never raises."""
    try:
        text_log = "\n".join(logs) if logs else (res.error or "")
        await deps.artifacts.put(job_id, log_path, text_log + "\n", kind="log")
        async with deps.sessions.begin() as s:
            await s.execute(
                update(BenchResult)
                .where(BenchResult.job_id == job_id)
                .values(hidden_passed=res.passed, hidden_detail=_detail(res))
            )
    except Exception as exc:
        log.warning("bench.hidden_store_failed", job_id=str(job_id), error=str(exc))
    return res


def trial_judges(
    rows: Sequence[ScoreRow], table: Mapping[tuple[str, str, str, str], Any]
) -> tuple[bool | None, bool | None]:
    """(judge_pass, judge_pass_uncalibrated): calibrated judges decide, the rest only display."""
    judged = [r for r in rows if r.judge_model is not None and r.passed is not None]
    counted = counted_judge_rows(judged, table)
    ids = {id(r) for r in counted}
    judge_pass = all(r.passed for r in counted) if counted else None
    rest = [r for r in judged if id(r) not in ids]
    return judge_pass, (all(r.passed for r in rest) if rest else None)


def trial_status(gate_reason: str | None, gate_scope: str | None) -> str:
    """A trial the run's own budget stopped was not run; a job or run cap stop is a result."""
    return "budget_stopped" if gate_reason == "budget" and gate_scope == "bench_run" else "done"


async def record_trial(s: AsyncSession, job_id: UUID) -> None:
    """Fill the trial's result row from the finished job, its scores and its last bench.gate."""
    result = await s.scalar(select(BenchResult).where(BenchResult.job_id == job_id))
    job = await s.get(Job, job_id, populate_existing=True)
    if result is None or job is None:
        return
    scores = list(await s.scalars(select(Score).where(Score.job_id == job_id).order_by(Score.id)))
    latest: dict[str, Score] = {}
    for sc in scores:
        if sc.judge_model is not None:
            latest[sc.scorer] = sc
    rows = [
        ScoreRow(
            sc.scorer,
            sc.label,
            sc.score,
            sc.passed,
            sc.reason or "",
            sc.run_id,
            sc.judge_model,
            sc.cost_usd,
            sc.rubric_sha,
            sc.judge_sha,
        )
        for sc in latest.values()
        if sc.label != "skipped"
    ]
    table = await calibration_table(s)
    result.judge_pass, result.judge_pass_uncalibrated = trial_judges(rows, table)
    gate = await s.scalar(
        select(JobEvent.data)
        .where(JobEvent.job_id == job_id, JobEvent.kind == "bench.gate")
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    gate = gate or {}
    result.rounds = job.round
    result.cost_usd = job.cost_usd
    start = job.started_at or job.created_at
    end = job.finished_at or datetime.now(UTC)
    result.duration_s = Decimal(str(round((end - start).total_seconds(), 3)))
    result.status = trial_status(gate.get("reason"), gate.get("scope"))
    await s.flush()


async def trial_task(
    sessions: async_sessionmaker[AsyncSession], defn: Definition, job_id: UUID
) -> BenchTaskDef | None:
    """The task definition a benchmark job is a trial of (None when it has no result row)."""
    async with sessions() as s:
        row = (
            await s.execute(
                select(BenchResult.task, BenchRun.suite)
                .join(BenchRun, BenchRun.id == BenchResult.run_id)
                .where(BenchResult.job_id == job_id)
            )
        ).first()
    if row is None:
        return None
    suite = defn.benchmarks.get(row.suite)
    return None if suite is None else suite.tasks.get(row.task)
