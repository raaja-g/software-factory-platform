"""The worker: claims jobs under leases and drives each job's graph."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.errors import EmptyInputError
from langgraph.types import Command
from sqlalchemy import select, update

from swfactory.artifacts import ArtifactStore
from swfactory.commands import CommandRefused, to_resume
from swfactory.db.engine import DURABILITY, make_engine, make_sessionmaker, open_checkpointer
from swfactory.db.repo import (
    CommandRow,
    LeaseLost,
    claim_commanded_job,
    command_applied,
    consume_command,
    consume_gate_commands,
    finish_agent_run,
    holds_lease,
    latest_waiting,
    next_command,
    pending_cancel,
    pending_gate_command,
    record_crash,
    requeue_failed,
    set_stage,
)
from swfactory.db.tables import AgentRun, Job
from swfactory.definition.models import Defaults
from swfactory.definition.store import DefinitionStore, NoActiveDefinition
from swfactory.domain.types import (
    ACTIVE_STAGES,
    TERMINAL_STAGES,
    HumanAction,
    RunStatus,
    Stage,
    Usage,
)
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import register_engine
from swfactory.events import EventBus, record_event
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.build import (
    GRAPH_COMPAT,
    GRAPH_VERSION,
    build_job_graph,
    initial_state,
    node_timeouts,
)
from swfactory.graph.deps import Deps, unavailable
from swfactory.graph.steps import _runner, pinned_definition
from swfactory.logging import log_context
from swfactory.queue import claim_next_job, release_job, renew_lease
from swfactory.sandbox.fake import FakeSandboxProvider

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.settings import Settings

log = structlog.get_logger(__name__)

POLL_S = 5.0
SHUTDOWN_WAIT_S = 20.0
_ACTIVE = frozenset(st.value for st in ACTIVE_STAGES)


def crash_backoff_s(crash_count: int) -> int:
    """Seconds a job waits before it is claimed again after its ``crash_count``-th crash."""
    return 60 * 2 ** (crash_count - 1)


def build_worker_deps(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    fake_script: Path | None,
    fake_git_state: Path | None = None,
) -> Deps:
    """The worker's dependency bundle. Fakes for now: real engines arrive in M6."""
    if fake_script is None:
        raise ValueError("no engines yet: pass --fake-script (real engines arrive in M6)")
    register_engine(FakeEngine(FakeScript.from_yaml(fake_script)))
    return Deps(
        sessions=sessions,
        events=EventBus(sessions, settings.libpq_dsn()),
        artifacts=ArtifactStore(sessions),
        sandboxes=FakeSandboxProvider(),  # pyright: ignore[reportArgumentType]
        selector=unavailable("selector", "M5"),
        catalog=unavailable("catalog", "M5"),
        gateway=unavailable("gateway", "M5"),
        budgets=unavailable("budgets", "M5"),
        git=FakeGitService(state_path=fake_git_state),
        roles=unavailable("roles", "M3"),
        triage=unavailable("triage", "M3"),
        definitions=DefinitionStore(sessions, mode=settings.mode),
        scoring=unavailable("scoring", "M10"),
        settings=settings,
    )


class _Renewal:
    """Shared between the renewal task and drive_job: why the job task was stopped."""

    def __init__(self) -> None:
        self.stopped = False
        self.cancelled = False  # a cancel command stopped the job task
        self.poke = asyncio.Event()  # a command may have arrived: look for a cancel


_POKES: dict[UUID, asyncio.Event] = {}  # the jobs this process drives, by id


async def _renew_loop(
    deps: Deps,
    job_id: UUID,
    worker_id: str,
    lease_epoch: int,
    lease_s: int,
    renew_s: float,
    run: asyncio.Task[Any],
    flag: _Renewal,
) -> None:
    last_ok = time.monotonic()
    while True:
        await asyncio.sleep(renew_s)
        flag.poke.set()  # in case a command notification was lost
        deadline = last_ok + lease_s - renew_s
        ok: bool | None = None
        try:
            async with deps.sessions() as s:
                ok = await asyncio.wait_for(
                    renew_lease(
                        s,
                        job_id=job_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        lease_s=lease_s,
                    ),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
        except Exception as exc:  # the database may be restarting: try again next tick
            log.warning("worker.renew_failed", error=repr(exc))
        if ok:
            last_ok = time.monotonic()
        elif ok is False:
            log.warning("worker.lease_lost", job_id=str(job_id))
        elif time.monotonic() < deadline:
            continue
        else:
            log.warning("worker.lease_unrenewable", job_id=str(job_id))
        flag.stopped = True
        run.cancel()
        return


async def _guard(deps: Deps, s: AsyncSession, job_id: UUID, worker_id: str, epoch: int) -> None:
    if not await holds_lease(s, job_id=job_id, worker_id=worker_id, lease_epoch=epoch):
        raise LeaseLost(str(job_id))


async def _fail_job(
    deps: Deps, job_id: UUID, worker_id: str, epoch: int, error: str, **fields: Any
) -> None:
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job_id, worker_id, epoch)
        await set_stage(s, job_id, Stage.FAILED, error=error, **fields)


async def _graph_run(
    deps: Deps,
    checkpointer: BaseCheckpointSaver,
    job: Job,
    *,
    worker_id: str,
    lease_epoch: int,
    command: CommandRow | None,
) -> None:
    if command is None:  # a cancel may have been written between the claim and now
        async with deps.sessions() as s:
            command = await pending_cancel(s, job.id)
    if command is not None and command.action == HumanAction.CANCEL.value:
        await _cancel_job(deps, checkpointer, job, worker_id, lease_epoch, command)
        return
    if command is not None and command.action == HumanAction.RETRY.value and job.stage == "failed":
        await _retry_failed(deps, job, worker_id, lease_epoch, command)
        return
    row, defn, _ = await pinned_definition(deps, job.id)
    if job.crash_count >= defn.factory.defaults.max_job_crashes:
        await _fail_job(
            deps,
            job.id,
            worker_id,
            lease_epoch,
            f"worker stopped {job.crash_count} times while running this job",
        )
        return
    graph = build_job_graph(deps, checkpointer, timeouts=node_timeouts(defn.factory.defaults))
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": str(job.id),
            "worker_id": worker_id,
            "lease_epoch": lease_epoch,
        }
    }
    snap = await graph.aget_state(cfg)
    has_checkpoint = bool(snap.values) or bool(snap.next)
    saved = row.graph_version
    if has_checkpoint and saved is not None and not _compatible(saved):
        await _fail_job(
            deps,
            job.id,
            worker_id,
            lease_epoch,
            f"graph version {saved} cannot be resumed by graph version {GRAPH_VERSION}",
        )
        return

    async def fresh() -> dict[str, Any]:
        async with deps.sessions.begin() as s:
            await _guard(deps, s, job.id, worker_id, lease_epoch)
            await s.execute(
                update(Job).where(Job.id == job.id).values(graph_version=str(GRAPH_VERSION))
            )
        return await graph.ainvoke(initial_state(row, defn), cfg, durability=DURABILITY)

    consume: tuple[UUID, int] | None = None
    if has_checkpoint:
        await _consume_applied(deps, job.id, worker_id, lease_epoch)
    if command is not None:  # a gate command: answer the wait it is bound to
        answer = await _gate_answer(deps, snap, job.id, worker_id, lease_epoch)
        if answer is None:
            return
        resume, consume = answer
        result = await graph.ainvoke(resume, cfg, durability=DURABILITY)
    elif not has_checkpoint:
        result = await fresh()
    else:
        try:
            result = await graph.ainvoke(None, cfg, durability=DURABILITY)
        except EmptyInputError:  # R01: no usable checkpoint after all
            result = await fresh()
    await _settle(deps, job, result, worker_id, lease_epoch, consume=consume)


async def _retry_failed(
    deps: Deps, job: Job, worker_id: str, lease_epoch: int, command: CommandRow
) -> None:
    """A ``retry`` on a failed job: requeue it so the next claim resumes from its checkpoint."""
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job.id, worker_id, lease_epoch)
        current = await s.get(Job, job.id, populate_existing=True)
        assert current is not None
        saved = current.graph_version
        if saved is not None and saved != str(GRAPH_VERSION):
            await consume_command(s, command.id, "refused: graph version no longer supported")
            return
        await requeue_failed(s, job_id=job.id, command_id=command.id, by=command.requested_by)


async def _consume_applied(deps: Deps, job_id: UUID, worker_id: str, lease_epoch: int) -> None:
    """Consume a gate command the checkpoint already shows as answered (a crash before consume)."""
    async with deps.sessions.begin() as s:
        cmd = await next_command(s, job_id)
        if cmd is None or cmd.action == HumanAction.CANCEL.value:
            return
        if not await command_applied(s, job_id, cmd.id):
            return
        await _guard(deps, s, job_id, worker_id, lease_epoch)
        await consume_command(s, cmd.id, "applied")


async def _gate_answer(
    deps: Deps, snap: Any, job_id: UUID, worker_id: str, lease_epoch: int
) -> tuple[Command, tuple[UUID, int]] | None:
    """The graph resume for the oldest gate command bound to the current wait, or None.

    Commands bound to an earlier wait are consumed as ``stale``; a refused command is consumed
    as ``refused: <reason>`` and leaves the job waiting.
    """
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job_id, worker_id, lease_epoch)
        waiting = await latest_waiting(s, job_id)
        if not snap.interrupts or waiting is None:
            cmd = await next_command(s, job_id)
            if cmd is not None and cmd.action != HumanAction.CANCEL.value:
                if await command_applied(s, job_id, cmd.id):
                    await consume_command(s, cmd.id, "applied")
                else:
                    await consume_command(s, cmd.id, "refused: job is not waiting for a person")
            return None
        wait_id = waiting[0]
        await consume_gate_commands(s, job_id, wait_id=wait_id, answers=False, result="stale")
        cmd = await pending_gate_command(s, job_id, wait_id)
        if cmd is None:
            return None
        try:
            resume = to_resume(
                HumanAction(cmd.action),
                note=cmd.note,
                by=cmd.requested_by,
                retry_offered=dict(snap.interrupts[0].value).get("retry_node") is not None,
                command_id=cmd.id,
            )
        except CommandRefused as exc:
            await consume_command(s, cmd.id, f"refused: {exc}")
            return None
        assert resume is not None
        return resume, (cmd.id, wait_id)


async def _cancel_job(
    deps: Deps,
    checkpointer: BaseCheckpointSaver,
    job: Job,
    worker_id: str,
    lease_epoch: int,
    command: CommandRow | None,
) -> None:
    """Stop a job: mark its runs cancelled, keep the branch, destroy the sandbox (L10)."""
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job.id, worker_id, lease_epoch)
        running = (
            await s.scalars(
                select(AgentRun.id).where(
                    AgentRun.job_id == job.id, AgentRun.status == RunStatus.RUNNING.value
                )
            )
        ).all()
        for run_id in running:
            await finish_agent_run(
                s,
                run_id,
                status=RunStatus.CANCELLED,
                verdict=None,
                artifact_path=None,
                usage=Usage(),
                error="cancelled",
                engine_version=None,
                used_models=(),
            )
    # a sandbox exists once the graph has started (its first node creates it)
    started = await checkpointer.aget_tuple({"configurable": {"thread_id": str(job.id)}})
    if started is not None:
        try:
            row, defn, app = await pinned_definition(deps, job.id)
            box = await deps.sandboxes.ensure(job.id, _runner(defn, app))
            try:
                await deps.git.keep_branch(row, app, box)
            except Exception as exc:  # the sandbox is destroyed all the same
                log.warning("worker.keep_branch_failed", error=repr(exc))
        except Exception as exc:
            log.warning("worker.cancel_cleanup_failed", error=repr(exc))
        try:
            await deps.sandboxes.destroy(job.id)
        except Exception as exc:
            log.warning("worker.destroy_failed", error=repr(exc))
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job.id, worker_id, lease_epoch)
        await set_stage(s, job.id, Stage.CANCELLED, finished_at=datetime.now(UTC))
        cancel = command or await pending_cancel(s, job.id)
        if cancel is not None:
            await consume_command(s, cancel.id, "cancelled")


def _compatible(saved: str) -> bool:
    try:
        return int(saved) in GRAPH_COMPAT.get(GRAPH_VERSION, frozenset())
    except ValueError:
        return False


async def _settle(
    deps: Deps,
    job: Job,
    result: dict[str, Any],
    worker_id: str,
    lease_epoch: int,
    *,
    consume: tuple[UUID, int] | None = None,
) -> None:
    interrupts = result.get("__interrupt__") or []
    async with deps.sessions.begin() as s:
        await _guard(deps, s, job.id, worker_id, lease_epoch)
        if consume is not None:  # the checkpoint shows the interrupt answered
            command_id, wait_id = consume
            await consume_command(s, command_id, "resumed")
            await consume_gate_commands(
                s, job.id, wait_id=wait_id, answers=True, result="superseded"
            )
        if interrupts:
            value = dict(interrupts[0].value)
            await set_stage(s, job.id, Stage.NEEDS_HUMAN, gate_reason=value.get("reason"))
            await record_event(
                s,
                job.id,
                "human.waiting",
                {
                    "reason": value.get("reason"),
                    "retry_node": value.get("retry_node"),
                    "artifacts": value.get("artifacts") or [],
                },
            )
            return
        current = await s.get(Job, job.id, populate_existing=True)
        if current is None or not (current.stage == "queued" or current.stage in _ACTIVE):
            return
        outcome = result.get("outcome")
        if outcome is None:
            await set_stage(s, job.id, Stage.FAILED, error="graph ended without an outcome")
            return
        stage = Stage(outcome)
        fields: dict[str, Any] = {}
        if stage in TERMINAL_STAGES:
            fields["finished_at"] = datetime.now(UTC)
        await set_stage(s, job.id, stage, **fields)


async def drive_job(
    deps: Deps,
    checkpointer: BaseCheckpointSaver,
    job: Job,
    *,
    worker_id: str,
    lease_epoch: int,
    lease_s: int,
    renew_s: float,
    command: CommandRow | None,
) -> None:
    """Run (or resume) one claimed job's graph, renewing its lease until it ends."""
    with log_context(job_id=job.id):
        flag = _Renewal()
        run = asyncio.create_task(
            _graph_run(
                deps,
                checkpointer,
                job,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                command=command,
            )
        )
        renewer = asyncio.create_task(
            _renew_loop(deps, job.id, worker_id, lease_epoch, lease_s, renew_s, run, flag)
        )
        watcher = asyncio.create_task(_watch_cancel(deps, job.id, run, flag))
        _POKES[job.id] = flag.poke
        me = asyncio.current_task()
        try:
            await run
        except LeaseLost:
            log.warning("worker.lease_lost", job_id=str(job.id))
            return  # no release and no stage write: the job is not ours any more
        except asyncio.CancelledError:
            if flag.cancelled and not (me is not None and me.cancelling()):
                try:
                    await _cancel_job(deps, checkpointer, job, worker_id, lease_epoch, None)
                except LeaseLost:
                    log.warning("worker.lease_lost", job_id=str(job.id))
                    return
                except Exception:
                    log.exception("worker.cancel_failed")
                    return  # the lease runs out and the cancel is read again
            elif flag.stopped and not (me is not None and me.cancelling()):
                return  # the renewal task stopped the job
            else:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.shield(_release(deps, job.id, worker_id, lease_epoch)), 5
                    )
                raise
        except Exception as exc:
            log.exception("worker.job_failed")
            try:
                await _record_crash(deps, job.id, worker_id, lease_epoch, exc)
            except LeaseLost:
                log.warning("worker.lease_lost", job_id=str(job.id))
                return
            except Exception:
                log.exception("worker.crash_not_recorded")
                return  # the lease runs out and the claim counts the crash
        finally:
            _POKES.pop(job.id, None)
            renewer.cancel()
            watcher.cancel()
            await asyncio.gather(renewer, watcher, return_exceptions=True)
        await _release(deps, job.id, worker_id, lease_epoch)


async def _record_crash(
    deps: Deps, job_id: UUID, worker_id: str, lease_epoch: int, exc: BaseException
) -> None:
    max_crashes = Defaults().max_job_crashes
    try:
        _, defn, _ = await pinned_definition(deps, job_id)
        max_crashes = defn.factory.defaults.max_job_crashes
    except Exception:
        log.warning("worker.crash_definition_unreadable", job_id=str(job_id))
    async with deps.sessions.begin() as s:
        failed = await record_crash(
            s,
            job_id=job_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            error=str(exc) or type(exc).__name__,
            max_crashes=max_crashes,
        )
    if failed:
        log.error("worker.job_poisoned", job_id=str(job_id))


async def _watch_cancel(deps: Deps, job_id: UUID, run: asyncio.Task[Any], flag: _Renewal) -> None:
    """Stop the job task as soon as a ``cancel`` command exists for the job."""
    while True:
        await flag.poke.wait()
        flag.poke.clear()
        try:
            async with deps.sessions() as s:
                found = await pending_cancel(s, job_id)
        except Exception as exc:
            log.warning("worker.cancel_check_failed", error=repr(exc))
            continue
        if found is not None:
            flag.cancelled = True
            run.cancel()
            return


async def _release(deps: Deps, job_id: UUID, worker_id: str, lease_epoch: int) -> None:
    async with deps.sessions() as s:
        await release_job(s, job_id=job_id, worker_id=worker_id, lease_epoch=lease_epoch)


def _poke_job(payload: str) -> None:
    """A command notification: have the job this process drives (if any) look for a cancel."""
    try:
        poke = _POKES.get(UUID(str(json.loads(payload)["job_id"])))
    except (ValueError, KeyError, TypeError):
        return
    if poke is not None:
        poke.set()


async def _listen(dsn: str, wake: asyncio.Event) -> None:
    """Set ``wake`` on every NOTIFY of the ``commands`` and ``job_events`` channels."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                await conn.execute("LISTEN commands")
                await conn.execute("LISTEN job_events")
                async for note in conn.notifies():
                    if note.channel == "commands":
                        _poke_job(note.payload)
                    wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("worker.listen_unavailable", error=repr(exc))
            await asyncio.sleep(POLL_S)


async def _limit(deps: Deps, max_jobs: int | None) -> int:
    if max_jobs is not None:
        return max_jobs
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        return 0
    return defn.factory.concurrency.global_


async def run_worker(
    settings: Settings,
    *,
    worker_id: str,
    max_jobs: int | None = None,
    lease_s: int = 90,
    renew_s: float = 30,
    fake_script: Path | None = None,
    fake_git_state: Path | None = None,
    exit_when_idle: bool = False,
    stop: asyncio.Event | None = None,
) -> None:
    if lease_s < 3 * renew_s:
        raise ValueError("lease_s must be at least 3 x renew_s")
    url = settings.sqlalchemy_url()
    engine = make_engine(url)
    sessions = make_sessionmaker(engine)
    try:
        deps = build_worker_deps(
            settings, sessions, fake_script=fake_script, fake_git_state=fake_git_state
        )
        async with open_checkpointer(url) as cp:
            await _loop(
                deps,
                cp,
                settings,
                worker_id=worker_id,
                max_jobs=max_jobs,
                lease_s=lease_s,
                renew_s=renew_s,
                exit_when_idle=exit_when_idle,
                stop=stop or asyncio.Event(),
            )
    finally:
        await engine.dispose()


async def _shutdown(
    deps: Deps, driving: dict[UUID, asyncio.Task[None]], epochs: dict[UUID, int], worker_id: str
) -> None:
    """Stop gracefully: restart each job's sandbox, cancel its task, release its lease."""
    log.info("worker.stopping", jobs=len(driving))
    for jid in driving:
        try:
            _, defn, app = await pinned_definition(deps, jid)
            box = await asyncio.wait_for(deps.sandboxes.ensure(jid, _runner(defn, app)), 10)
            await asyncio.wait_for(box.restart(), 10)  # kills the engine processes
        except Exception as exc:
            log.warning("worker.sandbox_restart_failed", job_id=str(jid), error=repr(exc))
    tasks = list(driving.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT_S)
    for jid in driving:
        try:
            await asyncio.wait_for(_release(deps, jid, worker_id, epochs[jid]), 5)
        except Exception as exc:
            log.warning("worker.release_failed", job_id=str(jid), error=repr(exc))


async def _loop(
    deps: Deps,
    cp: BaseCheckpointSaver,
    settings: Settings,
    *,
    worker_id: str,
    max_jobs: int | None,
    lease_s: int,
    renew_s: float,
    exit_when_idle: bool,
    stop: asyncio.Event,
) -> None:
    wake = asyncio.Event()
    driving: dict[UUID, asyncio.Task[None]] = {}
    epochs: dict[UUID, int] = {}
    listener = asyncio.create_task(_listen(settings.libpq_dsn(), wake))
    try:
        while True:
            wake.clear()
            if stop.is_set():
                await _shutdown(deps, driving, epochs, worker_id)
                return
            for jid, task in list(driving.items()):
                if task.done():
                    del driving[jid]
                    if not task.cancelled() and task.exception() is not None:
                        log.error("worker.drive_crashed", job_id=str(jid))
            limit = await _limit(deps, max_jobs)
            claimed = False
            while len(driving) < limit and not stop.is_set():
                command: CommandRow | None = None
                async with deps.sessions() as s:
                    found = await claim_commanded_job(
                        s, worker_id=worker_id, lease_s=lease_s, exclude=set(driving)
                    )
                if found is not None:
                    job, command = found
                else:
                    async with deps.sessions() as s:
                        job = await claim_next_job(
                            s, worker_id=worker_id, lease_s=lease_s, exclude=set(driving)
                        )
                if job is None:
                    break
                claimed = True
                epochs[job.id] = job.lease_epoch
                driving[job.id] = asyncio.create_task(
                    drive_job(
                        deps,
                        cp,
                        job,
                        worker_id=worker_id,
                        lease_epoch=job.lease_epoch,
                        lease_s=lease_s,
                        renew_s=renew_s,
                        command=command,
                    )
                )
            if exit_when_idle and not driving and not claimed:
                return
            waiters = {
                asyncio.ensure_future(wake.wait()),
                asyncio.ensure_future(stop.wait()),
                *driving.values(),
            }
            try:
                await asyncio.wait(waiters, timeout=POLL_S, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    if w not in driving.values():
                        w.cancel()
    finally:
        listener.cancel()
        for task in driving.values():
            task.cancel()
        await asyncio.gather(listener, *driving.values(), return_exceptions=True)
