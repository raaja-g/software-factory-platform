"""The worker: claims jobs under leases and drives each job's graph."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
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
    apply_budget_top_up,
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
from swfactory.expiry import EXPIRY_TICK_S, expire_waiting_jobs
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.build import (
    GRAPH_COMPAT,
    GRAPH_VERSION,
    build_job_graph,
    graph_for,
    initial_state,
    node_timeouts,
)
from swfactory.graph.deps import Deps
from swfactory.graph.steps import (
    _runner,
    job_head,
    keep_job_branch,
    pinned_definition,
    step_budget_s,
)
from swfactory.logging import log_context
from swfactory.models.triage import TriageService
from swfactory.notify import Notifier
from swfactory.queue import claim_next_job, release_job, renew_lease
from swfactory.roles.library import RoleLibrary
from swfactory.sandbox.disk import (
    WORKSPACE_CHECK_S,
    claim_new_allowed,
    disk_paths,
    disk_usage_pct,
)
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.sandbox.reaper import REAP_INTERVAL_S, reconcile

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.settings import Settings

log = structlog.get_logger(__name__)

POLL_S = 5.0
CATALOG_TICK_S = 300.0
CATALOG_POLL_S = 15.0
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
    """The worker's dependency bundle: fakes with a fake script, real engines without."""
    definitions = DefinitionStore(sessions, mode=settings.mode)
    from swfactory.costs.budgets import BudgetService
    from swfactory.gateway.client import GatewayClient
    from swfactory.models.catalog import Catalog
    from swfactory.scoring.service import ScoringService

    artifacts = ArtifactStore(sessions, settings.artifacts_dir)
    events = EventBus(sessions, settings.libpq_dsn())
    if fake_script is None:
        import docker

        from swfactory.engines.registry import register_engines
        from swfactory.gitops.folder import FolderRepo
        from swfactory.gitops.local import LocalGit
        from swfactory.gitops.service import GitDispatcher, GitService
        from swfactory.models.selection import ModelSelector
        from swfactory.sandbox.docker import DockerSandboxProvider

        register_engines(settings)
        git_services: dict[str, GitService] = {
            "local": LocalGit(
                Path(settings.repos_dir),
                max_bundle_mb=settings.max_bundle_mb,
                folders=FolderRepo.from_settings(settings),
            )
        }
        if settings.github_app_client_id or settings.github_token:
            from swfactory.gitops.github import build_github_git

            git_services["github"] = build_github_git(
                settings,
                sessions,
                artifacts,
                lambda: docker.DockerClient(base_url=settings.docker_host),
            )
        real_gateway = GatewayClient(
            settings.gateway_url, settings.gateway_master_key.get_secret_value()
        )
        real_catalog = Catalog(sessions, real_gateway, definitions)
        return Deps(
            sessions=sessions,
            events=events,
            artifacts=artifacts,
            sandboxes=DockerSandboxProvider(  # pyright: ignore[reportArgumentType]
                lambda: docker.DockerClient(base_url=settings.docker_host),
                attach=settings.sandbox_attach,
                output_max_bytes=settings.exec_output_max_bytes,
            ),
            selector=ModelSelector(sessions, real_catalog, events),
            catalog=real_catalog,
            gateway=real_gateway,
            budgets=BudgetService(sessions, definitions),
            git=GitDispatcher(git_services),
            roles=RoleLibrary(),
            triage=TriageService(definitions, artifacts),
            definitions=definitions,
            scoring=ScoringService(sessions, events, definitions, real_catalog),
            settings=settings,
            docker_client=lambda: docker.DockerClient(base_url=settings.docker_host),
        )
    register_engine(FakeEngine(FakeScript.from_yaml(fake_script)))
    from swfactory.engines.fake import FakeRunGateway, FakeSelector

    catalog: Any = Catalog(sessions, None, definitions)
    # the fake engine mints and spends nothing: run keys stay in-process
    gateway: Any = FakeRunGateway()
    gateway_url = getattr(settings, "gateway_url", None)
    master_key = getattr(settings, "gateway_master_key", None)
    if gateway_url and master_key is not None:
        catalog = Catalog(
            sessions, GatewayClient(gateway_url, master_key.get_secret_value()), definitions
        )
    return Deps(
        sessions=sessions,
        events=events,
        artifacts=artifacts,
        sandboxes=FakeSandboxProvider(),  # pyright: ignore[reportArgumentType]
        selector=FakeSelector(sessions),
        catalog=catalog,
        gateway=gateway,
        budgets=BudgetService(sessions, definitions),
        git=FakeGitService(state_path=fake_git_state),
        roles=RoleLibrary(),
        triage=TriageService(definitions, artifacts),
        definitions=definitions,
        scoring=ScoringService(sessions, events, definitions, catalog),
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
            continue
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
    defaults = defn.factory.defaults
    timeouts = (
        {"requirements": step_budget_s(defaults, "requirements")}
        if job.kind == "requirements"
        else node_timeouts(defaults)
    )
    builder = graph_for(job.kind)
    if builder.__name__ == "build_job_graph":  # looked up here so a test can wrap it
        builder = build_job_graph
    graph = builder(deps, checkpointer, timeouts=timeouts)
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
                head_sha=cmd.head_sha,
            )
        except CommandRefused as exc:
            await consume_command(s, cmd.id, f"refused: {exc}")
            return None
        assert resume is not None
        if cmd.action == HumanAction.ADD_BUDGET.value:
            # under the lease, once: a crash before the resume never adds the amount twice
            _, defn, app_def = await pinned_definition(deps, job_id)
            await apply_budget_top_up(
                s,
                command_id=cmd.id,
                job_id=job_id,
                default_usd=app_def.budget_usd or defn.factory.budgets.job_usd,
                job_max_usd=defn.factory.budgets.job_max_usd,
            )
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
        await keep_job_branch(deps, job.id)  # starts the sandbox if it is stopped; never raises
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
    if job.stage == Stage.DELIVERED.value:  # its PR is still open: say why it is closing
        found_cmd = command or cancel
        by = found_cmd.requested_by if found_cmd is not None else "a person"
        try:
            _, _, app_def = await pinned_definition(deps, job.id)
            await deps.git.withdraw(job, app_def, note=f"Cancelled in the factory by {by}.")
        except Exception as exc:
            log.warning("worker.withdraw_failed", job_id=str(job.id), error=repr(exc))


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
    head_sha: str | None = None
    if interrupts and job.kind != "requirements":
        # a waiting job's commits must reach the bare repo before anyone is told to look
        await keep_job_branch(deps, job.id)
        head_sha = await job_head(deps, job.id)
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
                    "head_sha": head_sha,
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
    engine = make_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        statement_timeout_s=settings.db_statement_timeout_s,
    )
    sessions = make_sessionmaker(engine)
    try:
        deps = build_worker_deps(
            settings, sessions, fake_script=fake_script, fake_git_state=fake_git_state
        )
        async with open_checkpointer(url, max_size=settings.checkpointer_pool_max) as cp:
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


async def sync_gateway_config(deps: Deps) -> bool:
    """Render the active definition into the gateway's config volume and restart the gateway
    when it changed. Never raises; a failure is retried on the next tick."""
    path = deps.settings.gateway_config_path
    if not path or deps.docker_client is None:
        return False
    from swfactory.gateway.render import sync_gateway

    try:
        sha, defn = await deps.definitions.active()
        return await sync_gateway(
            defn,
            sha,
            deps.docker_client(),
            conf_path=Path(path),
            ready_url=f"{deps.settings.gateway_url}/health/readiness",
        )
    except NoActiveDefinition:
        return False
    except Exception as exc:
        log.warning("gateway.sync_failed", error=repr(exc))
        return False


async def catalog_tick(deps: Deps) -> None:
    """One catalog tick (on-demand requests first, then the due refresh): never raises."""
    from swfactory.models.catalog import catalog_tick as serve

    try:
        await serve(
            deps.sessions,
            deps.catalog,
            now=datetime.now(UTC),
            before=(lambda: sync_gateway_config(deps))
            if deps.settings.gateway_config_path
            else None,
        )
    except NoActiveDefinition:
        pass
    except Exception as exc:
        if isinstance(exc, httpx.TransportError):
            log.warning("catalog.gateway_unreachable", error=repr(exc))
        else:
            log.warning("catalog.refresh_failed", error=repr(exc))


async def _catalog_loop(deps: Deps, interval_s: float = CATALOG_POLL_S) -> None:
    while True:
        await catalog_tick(deps)
        await asyncio.sleep(interval_s)


async def cost_tick(deps: Deps) -> None:
    """One spend reconcile (M5.9): never raises (the next tick tries again)."""
    from swfactory.costs.reconcile import reconcile_tick

    try:
        res = await reconcile_tick(deps, now=datetime.now(UTC))
        if res is not None and (res.costs_filled or res.reservations_released):
            log.info(
                "costs.reconciled",
                costs_filled=res.costs_filled,
                reservations_released=res.reservations_released,
            )
    except Exception as exc:
        log.warning("costs.reconcile_failed", error=repr(exc))


async def _cost_loop(deps: Deps, interval_s: float = CATALOG_TICK_S) -> None:
    while True:
        await cost_tick(deps)
        await asyncio.sleep(interval_s)


async def reap_tick(deps: Deps) -> None:
    """One sandbox reconcile: never raises (the next tick tries again)."""
    try:
        removed = await reconcile(deps.sandboxes, deps.sessions)
        if removed:
            log.info("sandbox.reaped", count=removed)
    except Exception as exc:
        log.warning("sandbox.reap_failed", error=repr(exc))


async def workspace_tick(deps: Deps) -> None:
    """One workspace size check: never raises (the next tick tries again)."""
    try:
        over = await deps.sandboxes.check_workspaces()
        if over:
            log.warning("sandbox.workspaces_over_limit", count=len(over))
    except Exception as exc:
        log.warning("sandbox.workspace_check_failed", error=repr(exc))


async def _workspace_loop(deps: Deps, interval_s: float = WORKSPACE_CHECK_S) -> None:
    while True:
        await workspace_tick(deps)
        await asyncio.sleep(interval_s)


def _disk_allows_claim(settings: Settings, state: dict[str, bool]) -> bool:
    """False while the fullest watched disk is over the high-water mark; logs each crossing."""
    try:
        pct, path = disk_usage_pct(disk_paths(settings))
    except OSError as exc:
        log.warning("worker.disk_check_failed", error=repr(exc))
        return True
    ok = claim_new_allowed(pct, settings.disk_high_water_pct)
    if not ok and not state["high"]:
        log.warning("worker.disk_high", pct=pct, path=path)
    elif ok and state["high"]:
        log.info("worker.disk_ok", pct=pct)
    state["high"] = not ok
    return ok


def make_notifier(settings: Settings) -> Notifier:
    """The worker-side Notifier, for checks that call ``notify_once``."""
    return Notifier.from_settings(settings)


def _worker_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("swfactory")
    except PackageNotFoundError:
        return "dev"


async def _heartbeat_loop(
    deps: Deps, worker_id: str, driving: dict[UUID, asyncio.Task[None]], renew_s: float
) -> None:
    """Beat on every lease-renew tick (``renew_s``), also when no job is being driven."""
    from swfactory.health import beat

    while True:
        try:
            async with deps.sessions() as s:
                await beat(s, worker_id=worker_id, jobs=len(driving), version=_worker_version())
        except Exception as exc:  # the database may be restarting: beat again next tick
            log.warning("worker.heartbeat_failed", error=repr(exc))
        await asyncio.sleep(renew_s)


async def _reap_loop(deps: Deps, interval_s: float = REAP_INTERVAL_S) -> None:
    while True:
        await reap_tick(deps)
        await asyncio.sleep(interval_s)


async def expiry_tick(deps: Deps) -> None:
    """One expiry pass over waiting jobs: never raises (the next tick tries again)."""
    try:
        _, defn = await deps.definitions.active()
        days = defn.factory.defaults.waiting_expiry_days
        closed = await expire_waiting_jobs(deps, now=datetime.now(UTC), days=days)
        if closed:
            log.info("job.expired", count=len(closed))
    except NoActiveDefinition:
        pass
    except Exception as exc:
        log.warning("job.expiry_failed", error=repr(exc))


async def _expiry_loop(deps: Deps, interval_s: float = EXPIRY_TICK_S) -> None:
    while True:
        await expiry_tick(deps)
        await asyncio.sleep(interval_s)


NIGHTLY_TICK_S = 300


def make_commit_logs(settings: Settings) -> dict[str, Any]:
    """Commit logs by repo kind: the local bare repos, and GitHub when credentials exist."""
    from swfactory.scoring.objective import GitHubCommitLog, LocalCommitLog

    logs: dict[str, Any] = {"local": LocalCommitLog(Path(settings.repos_dir))}
    if settings.github_app_client_id or settings.github_token:
        from swfactory.github.intake import make_client_factory

        logs["github"] = GitHubCommitLog(make_client_factory(settings))
    return logs


async def nightly_scoring_tick(deps: Deps, logs: Mapping[str, Any]) -> None:
    """One nightly-scoring pass (it runs once a day): never raises."""
    from swfactory.scoring.objective import nightly_scoring

    try:
        changed = await nightly_scoring(deps.sessions, logs, now=datetime.now(UTC))
        if changed:
            log.info("scoring.nightly", jobs=changed)
    except Exception as exc:
        log.warning("scoring.nightly_failed", error=repr(exc))


async def _nightly_loop(deps: Deps, settings: Settings, interval_s: float = NIGHTLY_TICK_S) -> None:
    logs = make_commit_logs(settings)
    while True:
        await nightly_scoring_tick(deps, logs)
        await asyncio.sleep(interval_s)


async def _scheduler_loop(deps: Deps, interval_s: float | None = None) -> None:
    from swfactory.automations.scheduler import SCHEDULER_TICK_S, Scheduler

    scheduler = Scheduler(deps.sessions, deps.definitions)
    while True:
        try:
            await scheduler.tick(datetime.now(UTC))
        except Exception as exc:
            log.warning("automation.tick_failed", error=repr(exc))
        await asyncio.sleep(interval_s or SCHEDULER_TICK_S)


BENCH_TICK_S = 30.0


async def _bench_loop(deps: Deps, interval_s: float = BENCH_TICK_S) -> None:
    from swfactory.bench.runner import BenchRunner

    bench = BenchRunner(deps.sessions, deps.definitions)
    while True:
        try:
            started = await bench.tick(datetime.now(UTC))
            if started:
                log.info("bench.trials_started", jobs=[str(j) for j in started])
        except Exception as exc:
            log.warning("bench.tick_failed", error=repr(exc))
        await asyncio.sleep(interval_s)


RUN_REQUEST_TICK_S = 2.0


async def run_request_loop(
    deps: Deps, worker_id: str, interval_s: float = RUN_REQUEST_TICK_S
) -> None:
    """Poll for point-and-run requests (no NOTIFY): never raises."""
    from swfactory.runs import build_run_deps, run_request_tick

    run_deps = build_run_deps(deps.settings, deps.definitions)
    if run_deps is None:
        log.info("run_request.disabled", reason="no local definition checkout")
        return
    while True:
        try:
            while await run_request_tick(deps.sessions, run_deps, worker_id=worker_id):
                pass
        except Exception as exc:
            log.warning("run_request.failed", error=repr(exc))
        await asyncio.sleep(interval_s)


SCORE_REQUEST_TICK_S = 2.0


async def score_request_loop(
    deps: Deps, worker_id: str, interval_s: float = SCORE_REQUEST_TICK_S
) -> None:
    """Run the judges the API asked for (`factory score`): polls, never raises."""
    from swfactory.scoring.requests import score_request_tick

    while True:
        try:
            while await score_request_tick(deps, worker_id):
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("score_request.failed", error=repr(exc))
        await asyncio.sleep(interval_s)


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


BROKER_PORT = 8091
_GH_REPO = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


async def _github_app_repos(deps: Deps) -> set[str]:
    try:
        _, defn = await deps.definitions.active()
    except Exception:
        return set()
    out: set[str] = set()
    for app in defn.factory.apps.values():
        if app.repo_kind == "github":
            m = _GH_REPO.match(app.repo_url)
            if m:
                out.add(f"{m.group(1)}/{m.group(2)}")
    return out


def make_definition_sync(deps: Deps, settings: Settings) -> Any | None:
    """The definition sync for this worker, or None without FACTORY_DEFINITION."""
    if not settings.definition_source:
        return None
    from swfactory.definition.sync import DefinitionSync, definition_repo_slug
    from swfactory.github.auth import make_auth

    auth = None
    if definition_repo_slug(settings.definition_source):
        try:
            auth = make_auth(settings)
        except ValueError as exc:  # no credentials: a public repo still fetches without a token
            log.warning("definition_sync.no_github_auth", error=str(exc))
    return DefinitionSync(settings, deps.definitions, auth=auth)  # pyright: ignore[reportArgumentType]


async def _serve_broker(deps: Deps, settings: Settings) -> None:
    """Serve the token broker on core-net (never published); needs a GitHub App and a token."""
    if not (settings.github_app_client_id and settings.broker_token_file):
        return
    import uvicorn

    from swfactory.definition.sync import definition_repo_slug
    from swfactory.github.auth import AppAuth, make_auth
    from swfactory.github.broker import TokenBroker, broker_app

    auth = make_auth(settings)
    if not isinstance(auth, AppAuth):
        return
    shared = Path(settings.broker_token_file).read_text().strip()
    broker = TokenBroker(
        auth,
        definition_repo=definition_repo_slug(settings.definition_source),
        app_repos=lambda: _github_app_repos(deps),
    )
    config = uvicorn.Config(
        broker_app(broker, shared_token=shared),
        host="0.0.0.0",  # noqa: S104 - core-net only, never published
        port=BROKER_PORT,
        log_level="warning",
    )
    await uvicorn.Server(config).serve()


async def _github_poll_loop(deps: Deps, settings: Settings) -> None:
    """Orchestrator: poll labelled issues and delivered PRs; it holds the App key."""
    if not (settings.github_app_client_id or settings.github_token):
        return
    from swfactory.github.intake import GitHubPoller, make_client_factory

    poller = GitHubPoller(deps.sessions, deps.definitions, make_client_factory(settings), settings)
    while True:
        try:
            await poller.tick()
        except Exception as exc:
            log.warning("github.poll_tick_failed", error=repr(exc))
        await asyncio.sleep(settings.github_poll_s)


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
    from swfactory.models.catalog import Catalog

    await sync_gateway_config(
        deps
    )  # startup; the catalog tick repeats it on every definition change

    real_catalog = isinstance(deps.catalog, Catalog)
    ticker = asyncio.create_task(_cost_loop(deps)) if real_catalog else None
    catalog_ticker = asyncio.create_task(_catalog_loop(deps)) if real_catalog else None
    reaper = asyncio.create_task(_reap_loop(deps))
    workspaces = asyncio.create_task(_workspace_loop(deps))
    expirer = asyncio.create_task(_expiry_loop(deps))
    heart = asyncio.create_task(_heartbeat_loop(deps, worker_id, driving, renew_s))
    runner = asyncio.create_task(run_request_loop(deps, worker_id))
    scorer_loop = asyncio.create_task(score_request_loop(deps, worker_id))
    scheduler = asyncio.create_task(_scheduler_loop(deps))
    bencher = asyncio.create_task(_bench_loop(deps))
    nightly = asyncio.create_task(_nightly_loop(deps, settings))
    github_poller = asyncio.create_task(_github_poll_loop(deps, settings))
    definition_sync = make_definition_sync(deps, settings)
    sync_tasks = (
        [
            asyncio.create_task(definition_sync.run_poll_loop()),
            asyncio.create_task(definition_sync.listen()),
        ]
        if definition_sync is not None
        else []
    )
    disk_state = {"high": False}
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
                elif _disk_allows_claim(settings, disk_state):
                    async with deps.sessions() as s:
                        job = await claim_next_job(
                            s, worker_id=worker_id, lease_s=lease_s, exclude=set(driving)
                        )
                else:
                    job = None
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
        reaper.cancel()
        workspaces.cancel()
        expirer.cancel()
        heart.cancel()
        runner.cancel()
        scorer_loop.cancel()
        scheduler.cancel()
        bencher.cancel()
        nightly.cancel()
        github_poller.cancel()
        for t in sync_tasks:
            t.cancel()
        if ticker is not None:
            ticker.cancel()
        if catalog_ticker is not None:
            catalog_ticker.cancel()
        for task in driving.values():
            task.cancel()
        await asyncio.gather(
            listener,
            reaper,
            workspaces,
            expirer,
            runner,
            scorer_loop,
            scheduler,
            bencher,
            nightly,
            github_poller,
            *sync_tasks,
            *([ticker] if ticker else []),
            *([catalog_ticker] if catalog_ticker else []),
            *driving.values(),
            return_exceptions=True,
        )
