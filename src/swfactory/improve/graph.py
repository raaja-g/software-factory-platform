"""The improver graph: analyze, propose, validate, benchmark, wait, open a PR, record.

The proposal is untrusted input (an agent edits the definition that configures every other agent).
Every node is check-before-act so a replay does nothing twice. The graph can push a branch and open
a pull request and nothing more: it holds no merge, approve or force-push code, and the GitHub
client it uses is wrapped so that only ``find_pr`` and ``create_pr`` are reachable."""

# pyright: reportTypedDictNotRequiredAccess=false
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict
from uuid import UUID, uuid4

import structlog
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import EmptyInputError, GraphBubbleUp
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.bench.report import ConfigSummary, build_report
from swfactory.bench.runner import compute_estimate, start_bench_run
from swfactory.db.engine import DURABILITY
from swfactory.db.repo import holds_lease, reset_crashes, set_stage
from swfactory.db.tables import IMPROVEMENT_DONE, BenchRun, Improvement, Job
from swfactory.definition.models import Defaults, Definition, ImproverConfig
from swfactory.definition.store import DefinitionStore, NoActiveDefinition, load_active
from swfactory.domain.types import JobKind, Role, RunStatus, Stage, ToolPolicy
from swfactory.events import record_event
from swfactory.github.auth import PERMS_DEFINITION_PR
from swfactory.github.client import GitHubClient, PullRef
from swfactory.graph.deps import Deps
from swfactory.graph.steps import job_sandbox, run_agent_step
from swfactory.improve.proposal import (
    REGRESSIONS_HEADING,
    FileChange,
    PrContext,
    Violation,
    check_paths,
    check_redraft,
    check_scope,
    compare,
    describe_model_changes,
    exercised_by_benchmark,
    render_improvement_pr,
    validate_proposal,
)
from swfactory.improve.signals import (
    FailureSignal,
    collect_signals,
    failures_since,
    group_by_role,
    last_improvement_at,
    trigger_due,
)
from swfactory.improve.workspace import (
    DefinitionWorkspace,
    improve_branch,
    read_changes,
    rebuild_changes,
    seed_sandbox,
)
from swfactory.logging import safe_error
from swfactory.queue import release_job, renew_lease
from swfactory.sandbox.base import running
from swfactory.sandbox.paths import REPO
from swfactory.settings import Settings

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

log = structlog.get_logger(__name__)

__all__ = [
    "IMPROVER_BENCH_DEADLINE_H",
    "IMPROVER_QUEUE_DEADLINE_H",
    "IMPROVER_TICK_S",
    "IMPROVE_NODES",
    "BenchPort",
    "CycleCapReached",
    "ImproveDeps",
    "ImproveState",
    "ImproverGitHub",
    "ImproverTicker",
    "RunnerBenchPort",
    "build_improve_graph",
    "expire_queued",
    "improve_initial_state",
    "improver_client_factory",
    "improver_status",
    "start_cycle",
]

IMPROVER_TICK_S = 3600
IMPROVER_BENCH_DEADLINE_H = 48
IMPROVER_QUEUE_DEADLINE_H = 72
IMPROVER_REQUESTER = "improver"

IMPROVE_NODES = (
    "analyze",
    "propose",
    "validate",
    "benchmark",
    "await_bench",
    "open_pr",
    "record",
)
MAX_VIOLATIONS = 50
MAX_ERROR = 400
_FINAL = ("pr_open", "branch_ready", "no_change", "rejected", "failed")


class CycleCapReached(Exception):
    def __init__(self, committed: Decimal, cap: Decimal, max_cycle_usd: Decimal) -> None:
        super().__init__(
            f"cycle benchmark cap reached: ${_usd(committed)} committed, "
            f"${_usd(cap)} more would pass ${_usd(max_cycle_usd)}"
        )
        self.committed = committed
        self.cap = cap
        self.max_cycle_usd = max_cycle_usd


def _usd(v: Decimal) -> str:
    return format(Decimal(v).normalize(), "f")


# --------------------------------------------------------------------------
# the benchmark port
# --------------------------------------------------------------------------


class BenchPort(Protocol):
    async def find_run(self, suite: str, definition_sha: str) -> UUID | None: ...

    async def start(
        self, suite: str, *, definition_sha: str, cycle_id: UUID, max_cycle_usd: Decimal
    ) -> UUID: ...

    async def status(self, run_id: UUID) -> Literal["running", "done", "failed"]: ...

    async def summary(self, run_id: UUID) -> tuple[ConfigSummary, ...]: ...

    async def cost(self, run_id: UUID) -> Decimal: ...


_COMMITTED = text(
    """
    SELECT coalesce(sum(b.budget_usd), 0) FROM app.bench_runs b
     WHERE b.id::text IN (
        SELECT DISTINCT jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(i.evidence->'started_bench_runs') = 'array'
                      THEN i.evidence->'started_bench_runs' ELSE '[]'::jsonb END)
          FROM app.improvements i WHERE i.cycle_id = :cycle)
    """
)


class RunnerBenchPort:
    """M10's benchmark runner as the improver's port: first configuration, once, under the cap."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], definitions: DefinitionStore
    ) -> None:
        self.sessions = sessions
        self.definitions = definitions

    async def find_run(self, suite: str, definition_sha: str) -> UUID | None:
        """A run to reuse: started by the improver for this sha, with the configuration and
        repetitions the improver would start now. Anyone else's run is never reused: it may
        cover other configurations or more repetitions, or have been requested to fit a result."""
        defn = await self.definitions.get(definition_sha)
        sdef = defn.benchmarks.get(suite) if defn is not None else None
        if sdef is None:
            return None
        want = [sdef.configurations[0].model_dump(mode="json")]
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(BenchRun.id, BenchRun.configurations)
                    .where(
                        BenchRun.suite == suite,
                        BenchRun.definition_sha == definition_sha,
                        BenchRun.status.in_(("running", "done")),
                        BenchRun.requested_by == IMPROVER_REQUESTER,
                        BenchRun.repetitions == 1,
                    )
                    .order_by(BenchRun.created_at.desc(), BenchRun.id.desc())
                    .limit(20)
                )
            ).all()
        for run_id, configs in rows:
            if configs == want:
                return run_id
        return None

    async def start(
        self, suite: str, *, definition_sha: str, cycle_id: UUID, max_cycle_usd: Decimal
    ) -> UUID:
        defn = await self.definitions.get(definition_sha)
        if defn is None:
            raise ValueError(f"definition {definition_sha} is not stored")
        sdef = defn.benchmarks.get(suite)
        if sdef is None:
            raise ValueError(f"no such benchmark suite '{suite}'")
        first = sdef.configurations[0]
        async with self.sessions() as s, s.begin():
            committed = Decimal(str(await s.scalar(_COMMITTED, {"cycle": cycle_id}) or 0))
            if committed + sdef.budget_usd > max_cycle_usd:
                raise CycleCapReached(committed, sdef.budget_usd, max_cycle_usd)
            _, _, est = await compute_estimate(
                s, defn, suite=suite, configs=[first.name], repetitions=1
            )
            return await start_bench_run(
                s,
                defn,
                suite=suite,
                configs=[first.name],
                repetitions=1,
                budget_usd=sdef.budget_usd,
                definition_sha=definition_sha,
                accept_estimate_usd=est.total_usd,
                requested_by=IMPROVER_REQUESTER,
            )

    async def status(self, run_id: UUID) -> Literal["running", "done", "failed"]:
        async with self.sessions() as s:
            st = await s.scalar(select(BenchRun.status).where(BenchRun.id == run_id))
        if st == "done":
            return "done"
        if st == "running":
            return "running"
        return "failed"  # stopped_budget, cancelled, or a run that vanished

    async def _report(self, run_id: UUID) -> Any:
        from swfactory.api.routes.bench import load_run  # lazy: the routes import much more

        async with self.sessions() as s:
            found = await load_run(s, run_id)
        if found is None:
            raise ValueError(f"bench run {run_id} not found")
        return build_report(*found)

    async def summary(self, run_id: UUID) -> tuple[ConfigSummary, ...]:
        return (await self._report(run_id)).configs

    async def cost(self, run_id: UUID) -> Decimal:
        return Decimal((await self._report(run_id)).cost_usd)


# --------------------------------------------------------------------------
# the narrow GitHub client
# --------------------------------------------------------------------------


class ImproverGitHub:
    """What the improver may do on GitHub: find a PR and open one. Nothing else is reachable."""

    __slots__ = ("_client",)

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def find_pr(self, branch: str) -> PullRef | None:
        """The pull request for the branch in any state (for following it to merged or closed)."""
        return await self._client.find_pr(branch)

    async def find_open_pr(self, branch: str) -> PullRef | None:
        """The OPEN pull request for exactly this head branch; a closed or merged one is not
        reused (opening the improvement's PR must never end by recording somebody else's)."""
        if not branch.startswith("improve/"):
            raise ValueError("the improver only looks at improve/ branches")
        pr = await self._client.find_pr(branch)
        return pr if pr is not None and pr.state == "open" and not pr.merged else None

    async def create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRef:
        if not head.startswith("improve/"):
            raise ValueError("the improver only opens pull requests from improve/ branches")
        return await self._client.create_pr(
            head=head, base=base, title=title, body=body, draft=False
        )


def improver_client_factory(settings: Settings) -> Callable[[str], GitHubClient]:
    """GitHub clients for the improver: installation tokens for exactly PERMS_DEFINITION_PR
    (contents and pull requests write: push a branch, open a PR), never the delivery set."""
    from swfactory.github.intake import make_client_factory

    return make_client_factory(settings, permissions=PERMS_DEFINITION_PR)


# --------------------------------------------------------------------------
# state and deps
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImproveDeps:
    core: Deps
    bench: BenchPort
    workspace: Callable[[], DefinitionWorkspace]
    github: Callable[[str], GitHubClient] | None
    clock: Callable[[], datetime]
    checkpointer: BaseCheckpointSaver | None = None  # the ticker drives graphs with this


class ImproveState(TypedDict, total=False):
    improvement_id: str
    job_id: str
    role: str
    base_sha: str
    proposed_sha: str | None
    branch: str
    status: str
    violations: list[dict]
    bench_before: str | None
    bench_after: str | None
    pr_url: str | None


def improve_initial_state(row: Improvement) -> ImproveState:
    if row.job_id is None:
        raise ValueError("an improvement needs its improver job")
    return {
        "improvement_id": str(row.id),
        "job_id": str(row.job_id),
        "role": row.role_group,
        "base_sha": row.base_sha,
        "proposed_sha": row.proposed_sha,
        "branch": improve_branch(
            row.created_at.astimezone(UTC).date(), Role(row.role_group), row.id
        ),
        "status": row.status,
        "violations": [],
        "bench_before": str(row.bench_run_before) if row.bench_run_before else None,
        "bench_after": str(row.bench_run_after) if row.bench_run_after else None,
        "pr_url": row.pr_url,
    }


class _Stop(Exception):
    """A node ends the improvement with this status (and, for failed, this error)."""

    def __init__(self, status: str, error: str | None = None, **extra: Any) -> None:
        super().__init__(error or status)
        self.status = status
        self.error = error
        self.extra = extra


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _changes_json(changes: Sequence[FileChange]) -> str:
    return json.dumps([asdict(c) for c in changes], sort_keys=True)


def _load_changes(raw: str) -> list[FileChange]:
    return [FileChange(**d) for d in json.loads(raw)]


def _signals(evidence: Any) -> list[FailureSignal]:
    out: list[FailureSignal] = []
    items = evidence.get("signals") if isinstance(evidence, dict) else None
    for d in items if isinstance(items, list) else []:
        try:
            out.append(
                FailureSignal(
                    job_id=UUID(str(d["job_id"])),
                    job_ref=str(d.get("job_ref", "")),
                    role=Role(d["role"]),
                    kind=d["kind"],
                    detail=str(d.get("detail", "")),
                    at=datetime.fromisoformat(str(d["at"])),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _evidence_md(role: str, signals: Sequence[FailureSignal]) -> str:
    lines = [f"# Evidence for the {role} role group", ""]
    if not signals:
        lines.append("No signals were recorded.")
    for s in signals:
        lines.append(f"- {s.job_ref} ({s.role.value}, {s.kind}): {s.detail}")
    return "\n".join(lines) + "\n"


def _violation(v: Violation) -> dict[str, str]:
    return {"path": v.path[:200], "rule": v.rule, "message": v.message[:300]}


def _message(row: Improvement) -> str:
    return (
        f"improve({row.role_group}): redraft {row.role_group} guidance\n\nImprovement: {row.id}\n"
    )


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------


def build_improve_graph(deps: ImproveDeps, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    core = deps.core
    sessions = core.sessions

    async def row_of(state: ImproveState) -> Improvement:
        async with sessions() as s:
            row = await s.get(Improvement, UUID(state["improvement_id"]))
        if row is None:
            raise RuntimeError("improvement not found")
        return row

    def job_of(state: ImproveState) -> UUID:
        return UUID(state["job_id"])

    async def update(
        state: ImproveState,
        *,
        status: str | None = None,
        evidence: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
        **more: Any,
    ) -> None:
        fields = {**(fields or {}), **more}
        iid = UUID(state["improvement_id"])
        async with sessions() as s, s.begin():
            row = await s.get(Improvement, iid, with_for_update=True, populate_existing=True)
            if row is None:
                raise RuntimeError("improvement not found")
            if status is not None:
                row.status = status
            for k, v in fields.items():
                setattr(row, k, v)
            if evidence:
                row.evidence = {**(row.evidence or {}), **evidence}
            row.updated_at = deps.clock()
            await record_event(
                s,
                row.job_id,
                "improvement.updated",
                {"id": str(row.id), "status": row.status, "role": row.role_group},
            )

    def guarded(name: str, fn: Callable[[ImproveState], Any]) -> Any:
        async def node(state: ImproveState) -> Command:
            try:
                result = await fn(state)
                try:
                    async with sessions.begin() as s:  # a completed node ends any run of crashes
                        await reset_crashes(s, job_of(state))
                except Exception as exc:
                    log.warning("improve.reset_crashes_failed", error=safe_error(exc))
                return result
            except GraphBubbleUp:
                raise  # an interrupt is the graph pausing, not a failure
            except _Stop as stop:
                status, error, extra = stop.status, stop.error, stop.extra
            except Exception as exc:
                detail = safe_error(exc)
                if not hasattr(exc, "response"):
                    detail = f"{detail}: {str(exc)[:200]}"
                log.warning("improve.node_failed", node=name, error=detail)
                status, error, extra = "failed", f"{name} failed: {detail}", {}
            fields: dict[str, Any] = {}
            if error is not None:
                fields["error"] = error[:MAX_ERROR]
            evidence = extra.pop("evidence", None)
            await update(state, status=status, evidence=evidence, **fields)
            return Command(goto="record", update={"status": status, **extra})

        return node

    # -- analyze ----------------------------------------------------------------
    async def analyze(state: ImproveState) -> Command:
        row = await row_of(state)
        await update(state, status="analyzing")
        signals = _signals(row.evidence)
        await core.artifacts.put(job_of(state), "evidence.md", _evidence_md(state["role"], signals))
        outcome = await run_agent_step(
            core,
            job_id=job_of(state),
            role=Role.IMPROVER,
            round=1,
            inputs=["evidence.md"],
            output_name="analysis.md",
            workdir=None,
            tool_policy=ToolPolicy.NONE,
            engine="direct",
        )
        if outcome.status != RunStatus.OK:
            raise _Stop("failed", f"analysis failed: {outcome.status}")
        return Command(goto="propose", update={"status": "analyzing"})

    # -- propose ----------------------------------------------------------------
    async def propose(state: ImproveState) -> Command:
        job_id = job_of(state)
        await update(state, status="proposing")
        stored = await core.artifacts.get_text(job_id, "changes.json")
        if stored is None:
            ws = deps.workspace()
            files = await ws.checkout(state["base_sha"])
            box = await job_sandbox(core, job_id)
            async with running(box):
                if (await box.exec(["test", "-d", REPO], cwd="/")).exit_code == 0:
                    wipe = await box.exec(["find", REPO, "-mindepth", "1", "-delete"], cwd="/")
                    if wipe.exit_code != 0:  # a leftover edit would be committed as the base
                        raise _Stop("failed", "could not clear the sandbox repository")
                await seed_sandbox(box, files)
            outcome = await run_agent_step(
                core,
                job_id=job_id,
                role=Role.IMPROVER,
                round=2,
                inputs=["evidence.md", "analysis.md"],
                output_name="proposal.md",
                workdir=REPO,
                tool_policy=ToolPolicy.WRITE,
            )
            if outcome.status != RunStatus.OK:
                raise _Stop("failed", f"proposal failed: {outcome.status}")
            try:
                async with running(box):
                    changes = await read_changes(box, files)
            except Exception as exc:  # a diff git cannot describe is refused, never guessed at
                v = {"path": "", "rule": "invalid", "message": str(exc)[:300]}
                raise _Stop("rejected", violations=[v], evidence={"violations": [v]}) from exc
            stored = _changes_json(changes)
            await core.artifacts.put(job_id, "changes.json", stored, kind="json")
        changes = _load_changes(stored)
        if not changes:
            raise _Stop("no_change")
        proposal = await core.artifacts.get_text(job_id, "proposal.md") or ""
        if REGRESSIONS_HEADING not in proposal:
            v = {
                "path": "proposal.md",
                "rule": "invalid",
                "message": f"the proposal has no '{REGRESSIONS_HEADING}' section",
            }
            raise _Stop("rejected", violations=[v], evidence={"violations": [v]})
        return Command(goto="validate", update={"status": "proposing"})

    # -- validate ---------------------------------------------------------------
    async def validate(state: ImproveState) -> Command:
        row = await row_of(state)
        await update(state, status="validating")
        raw = await core.artifacts.get_text(job_of(state), "changes.json")
        if raw is None:
            raise _Stop("failed", "the proposal's changes are missing")
        role = Role(state["role"])
        base = await core.definitions.get(state["base_sha"])
        if base is None:
            raise _Stop("failed", "the base definition is not stored")
        ws = deps.workspace()
        # every check runs on host-derived old text and status, whatever changes.json says
        changes = rebuild_changes(_load_changes(raw), await ws.checkout(state["base_sha"]))
        early = [*check_paths(changes), *check_redraft(changes), *check_scope(changes, role)]
        if early:
            vs = [_violation(v) for v in early[:MAX_VIOLATIONS]]
            raise _Stop("rejected", violations=vs, evidence={"violations": vs})
        sha = await ws.commit(state["branch"], changes, message=_message(row), when=row.created_at)
        check = validate_proposal(ws.path, base, changes, mode=core.definitions.mode, role=role)
        if not check.ok:
            vs = [_violation(v) for v in check.violations[:MAX_VIOLATIONS]]
            raise _Stop("rejected", violations=vs, evidence={"violations": vs})
        if check.load.sha != sha:
            raise _Stop("failed", "the validated definition is not the committed one")
        await core.definitions.save(check.load, activate=False)
        await update(state, proposed_sha=sha, branch=state["branch"])
        return Command(goto="benchmark", update={"status": "validating", "proposed_sha": sha})

    # -- benchmark --------------------------------------------------------------
    async def append_started(iid: UUID, run_id: UUID) -> None:
        async with sessions() as s, s.begin():
            row = await s.get(Improvement, iid, with_for_update=True, populate_existing=True)
            if row is None:
                raise RuntimeError("improvement not found")
            ev = dict(row.evidence or {})
            started = [str(x) for x in ev.get("started_bench_runs", [])]
            if str(run_id) not in started:
                started.append(str(run_id))
            row.evidence = {**ev, "started_bench_runs": started}
            row.updated_at = deps.clock()

    async def benchmark(state: ImproveState) -> Command:
        row = await row_of(state)
        await update(state, status="benchmarking")
        proposed = state.get("proposed_sha") or row.proposed_sha
        if proposed is None:
            raise _Stop("failed", "there is no proposed definition to benchmark")
        _, active = await core.definitions.active()
        cap = active.factory.improver.max_cycle_usd
        cycle = row.cycle_id or row.id
        got: dict[str, UUID] = {}
        for key, column, sha in (
            ("before", "bench_run_before", state["base_sha"]),
            ("after", "bench_run_after", proposed),
        ):
            run_id: UUID | None = getattr(row, column)
            if run_id is None:
                run_id = await deps.bench.find_run(row.suite, sha)
                if run_id is None:
                    try:
                        run_id = await deps.bench.start(
                            row.suite, definition_sha=sha, cycle_id=cycle, max_cycle_usd=cap
                        )
                    except CycleCapReached as exc:
                        raise _Stop("failed", str(exc)) from exc
                    await append_started(row.id, run_id)
                await update(state, fields={column: run_id})
            got[key] = run_id
        return Command(
            goto="await_bench",
            update={
                "status": "benchmarking",
                "bench_before": str(got["before"]),
                "bench_after": str(got["after"]),
            },
        )

    # -- await_bench ------------------------------------------------------------
    async def await_bench(state: ImproveState) -> Command:
        ids = [UUID(str(state["bench_before"])), UUID(str(state["bench_after"]))]
        while True:
            statuses = [await deps.bench.status(i) for i in ids]
            if "failed" in statuses:
                raise _Stop("failed", "a benchmark run did not finish")
            if all(st == "done" for st in statuses):
                break
            interrupt({"bench_runs": [str(i) for i in ids]})
        return Command(goto="open_pr", update={"status": "benchmarking"})

    # -- open_pr ----------------------------------------------------------------
    async def open_pr(state: ImproveState) -> Command:
        row = await row_of(state)
        job_id = job_of(state)
        raw = await core.artifacts.get_text(job_id, "changes.json")
        proposed = state.get("proposed_sha")
        if raw is None or proposed is None:
            raise _Stop("failed", "nothing to push")
        role = Role(state["role"])
        branch = state["branch"]
        ws = deps.workspace()
        changes = rebuild_changes(_load_changes(raw), await ws.checkout(state["base_sha"]))
        sha = await ws.commit(branch, changes, message=_message(row), when=row.created_at)
        if sha != proposed:
            raise _Stop("failed", "the rebuilt commit differs from the validated one")
        await ws.push(branch, sha)
        if ws.slug is None:
            await update(state, status="branch_ready", branch=branch)
            return Command(goto="record", update={"status": "branch_ready", "pr_url": None})
        if deps.github is None:
            raise _Stop("failed", "no GitHub client is configured for the definition repo")
        gh = ImproverGitHub(deps.github(ws.slug))
        pr = await gh.find_open_pr(branch)
        if pr is None:
            before_id, after_id = UUID(str(state["bench_before"])), UUID(str(state["bench_after"]))
            before = await deps.bench.summary(before_id)
            after = await deps.bench.summary(after_id)
            base_defn = await core.definitions.get(state["base_sha"])
            new_defn = await core.definitions.get(proposed)
            if base_defn is None or new_defn is None:  # never open a PR that hides model changes
                raise _Stop("failed", "a stored definition needed for the pull request is missing")
            models = describe_model_changes(base_defn, new_defn)
            async with sessions() as s:
                job_cost = await s.scalar(select(Job.cost_usd).where(Job.id == job_id))
            ctx = PrContext(
                role=role,
                suite=row.suite,
                summary=await core.artifacts.get_text(job_id, "proposal.md") or "",
                signals=tuple(_signals(row.evidence)),
                changes=tuple(changes),
                comparison=compare(
                    before,
                    after,
                    role=role,
                    exercised=exercised_by_benchmark(new_defn, row.suite, role, changes),
                ),
                improver_cost_usd=Decimal(job_cost or 0),
                bench_cost_usd=(await deps.bench.cost(before_id), await deps.bench.cost(after_id)),
                dashboard_url=core.settings.public_url or "",
                model_changes=tuple(models),
            )
            title, body = render_improvement_pr(ctx)
            pr = await gh.create_pr(head=branch, base=ws.branch, title=title, body=body)
        await update(state, status="pr_open", branch=branch, pr_url=pr.url)
        return Command(goto="record", update={"status": "pr_open", "pr_url": pr.url})

    # -- record -----------------------------------------------------------------
    async def record(state: ImproveState) -> dict[str, Any]:
        job_id = job_of(state)
        status = state.get("status") or "failed"
        if status not in _FINAL:
            status = "failed"
        costs = Decimal(0)
        unknown: list[str] = []
        for key in ("bench_before", "bench_after"):
            if state.get(key):
                try:
                    costs += await deps.bench.cost(UUID(str(state[key])))
                except Exception as exc:  # a missing report must not lose the record
                    log.warning("improve.cost_failed", error=safe_error(exc))
                    unknown.append(key)
        async with sessions() as s:
            job_cost = Decimal(await s.scalar(select(Job.cost_usd).where(Job.id == job_id)) or 0)
        fields: dict[str, Any] = {"cost_usd": job_cost + costs}
        evidence: dict[str, Any] = {}
        if unknown:  # cost_usd is then a lower bound, and says so
            evidence["cost_unknown"] = unknown
        if state.get("pr_url"):
            fields["pr_url"] = state["pr_url"]
        if state.get("violations"):
            evidence["violations"] = state["violations"]
        await update(state, status=status, evidence=evidence or None, **fields)
        stage = Stage.DELIVERED if status in ("pr_open", "branch_ready") else Stage.CLOSED
        async with sessions() as s, s.begin():
            await set_stage(s, job_id, stage, finished_at=deps.clock())
        return {"status": status}

    g = StateGraph(ImproveState)
    nodes: dict[str, Any] = {
        "analyze": analyze,
        "propose": propose,
        "validate": validate,
        "benchmark": benchmark,
        "await_bench": await_bench,
        "open_pr": open_pr,
    }
    for name, fn in nodes.items():
        g.add_node(name, guarded(name, fn))
    g.add_node("record", record)
    g.add_edge(START, "analyze")
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------
# cycles
# --------------------------------------------------------------------------

_LOCK = text("SELECT pg_advisory_xact_lock(hashtext('factory.improver'))")
_OPEN_FOR_REVIEW = ("pr_open", "branch_ready")
_FINISHED_JOB = ("failed", "closed", "merged", "cancelled", "delivered")
_IN_FLIGHT = ("analyzing", "proposing", "validating")


def _signal_json(sig: FailureSignal) -> dict[str, str]:
    return {
        "job_id": str(sig.job_id),
        "job_ref": sig.job_ref,
        "role": sig.role.value,
        "kind": sig.kind,
        "detail": sig.detail,
        "at": sig.at.isoformat(),
    }


async def expire_queued(s: AsyncSession, now: datetime) -> list[UUID]:
    """Fail improvements that stayed `queued` for IMPROVER_QUEUE_DEADLINE_H hours.

    Nobody is driving them (no worker runs an improver ticker, or none for a long time), and a
    queued row blocks its role group for as long as it exists. Runs in the caller's transaction."""
    cutoff = now - timedelta(hours=IMPROVER_QUEUE_DEADLINE_H)
    rows = list(
        await s.scalars(
            select(Improvement)
            .where(Improvement.status == "queued", Improvement.created_at < cutoff)
            .with_for_update()
        )
    )
    error = f"no improver worker picked this up in {IMPROVER_QUEUE_DEADLINE_H} h"
    for row in rows:
        row.status = "failed"
        row.error = error
        row.updated_at = now
        await record_event(
            s,
            row.job_id,
            "improvement.updated",
            {"id": str(row.id), "status": "failed", "role": row.role_group},
        )
        if row.job_id is not None:
            await set_stage(s, row.job_id, Stage.FAILED, error=error, finished_at=now)
    return [r.id for r in rows]


async def start_cycle(
    s: AsyncSession,
    defn: Definition,
    *,
    trigger: Literal["threshold", "age", "manual"],
    role: Role | None = None,
    now: datetime | None = None,
) -> list[UUID]:
    """One improvement and one improver job per role group with failure signals since the last
    improvement. A group that still has an improvement in progress or open for review is skipped.
    Runs in the caller's transaction and never commits."""
    suite = defn.factory.improver.bench_suite
    if not suite:
        raise ValueError("set improver.bench_suite first")
    await s.execute(_LOCK)
    found = await load_active(s)
    if found is None:
        raise NoActiveDefinition("no active definition")
    sha = found[0]
    when = now or datetime.now(UTC)
    await expire_queued(s, when)
    signals = await collect_signals(s, since=await last_improvement_at(s))
    groups = group_by_role(signals)
    if role is not None:
        groups = [g for g in groups if g[0] == role]
    busy = set(
        await s.scalars(
            select(Improvement.role_group).where(
                or_(
                    Improvement.status.not_in(IMPROVEMENT_DONE),
                    Improvement.status.in_(_OPEN_FOR_REVIEW),
                )
            )
        )
    )
    groups = [g for g in groups if g[0].value not in busy]
    if not groups:
        return []
    cycle = uuid4()
    seq = int(
        await s.scalar(
            select(func.coalesce(func.max(Job.seq), 0)).where(Job.kind == JobKind.IMPROVER.value)
        )
        or 0
    )
    ids: list[UUID] = []
    for i, (grp, sigs) in enumerate(groups):
        seq += 1
        job = Job(
            app_id=None,
            seq=seq,
            slug=f"improve-{grp.value}"[:40],
            title=f"improve {grp.value}",
            feature=f"Improve the {grp.value} role group",
            kind=JobKind.IMPROVER.value,
            requested_by="improver",
            definition_sha=sha,
            stage=Stage.QUEUED.value,
            budget_usd=defn.factory.budgets.job_usd,
        )
        s.add(job)
        await s.flush()
        row = Improvement(
            trigger=trigger,
            role_group=grp.value,
            status="queued",
            job_id=job.id,
            cycle_id=cycle,
            base_sha=sha,
            suite=suite,
            evidence={"signals": [_signal_json(x) for x in sigs]},
            created_at=when + timedelta(microseconds=i),  # one order for the whole cycle
            updated_at=when,
        )
        s.add(row)
        await s.flush()
        await record_event(
            s,
            job.id,
            "improvement.updated",
            {"id": str(row.id), "status": "queued", "role": grp.value},
        )
        ids.append(row.id)
    return ids


async def improver_status(s: AsyncSession, defn: Definition, *, now: datetime) -> dict[str, Any]:
    """What the trigger sees: failures since the last improvement against the threshold."""
    cfg: ImproverConfig = defn.factory.improver
    since = await last_improvement_at(s)
    failed, oldest = await failures_since(s, since)
    decision = trigger_due(cfg, failed_jobs=failed, oldest_at=oldest, now=now)
    open_rows = (
        await s.execute(
            select(Improvement.role_group, Improvement.status)
            .where(Improvement.status.not_in(IMPROVEMENT_DONE))
            .order_by(Improvement.created_at)
        )
    ).all()
    return {
        "enabled": cfg.enabled,
        "suite": cfg.bench_suite,
        "threshold": cfg.failed_run_threshold,
        "max_failure_age_days": cfg.max_failure_age_days,
        "failures": failed,
        "oldest_failure_at": oldest.isoformat() if oldest else None,
        "last_improvement_at": since.isoformat() if since else None,
        "due": decision.reason,
        "in_progress": [{"role_group": r, "status": st} for r, st in open_rows],
    }


# --------------------------------------------------------------------------
# the ticker
# --------------------------------------------------------------------------


class ImproverTicker:
    """Starts cycles, drives one improvement at a time under a lease, resumes waiting ones."""

    def __init__(
        self,
        deps: ImproveDeps,
        *,
        worker_id: str,
        checkpointer: BaseCheckpointSaver | None = None,
        lease_s: int = 90,
        renew_s: float = 30,
    ) -> None:
        self.deps = deps
        self.worker_id = worker_id
        self.sessions = deps.core.sessions
        self.checkpointer = checkpointer or deps.checkpointer or InMemorySaver()
        self.lease_s = lease_s
        self.renew_s = renew_s

    async def tick(self, now: datetime) -> list[UUID]:
        ids: list[UUID] = []
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("expire_queued", lambda: self._expire_queued(now)),
            ("reclaim", lambda: self._reclaim(now)),
            ("claim", lambda: self._claim_queued(now)),
            ("resume", lambda: self._resume_waiting(now)),
            ("deadline", lambda: self._deadline(now)),
            ("pull_requests", lambda: self._refresh_prs()),
        ]
        try:
            ids = await self._trigger(now)
        except NoActiveDefinition:
            pass
        except Exception as exc:
            log.warning("improve.trigger_failed", error=safe_error(exc))
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                log.warning("improve.tick_step_failed", step=name, error=safe_error(exc))
        return ids

    async def _expire_queued(self, now: datetime) -> None:
        async with self.sessions() as s, s.begin():
            expired = await expire_queued(s, now)
        if expired:
            log.warning("improve.queued_expired", improvements=[str(i) for i in expired])

    # -- trigger ----------------------------------------------------------------
    async def _trigger(self, now: datetime) -> list[UUID]:
        async with self.sessions() as s, s.begin():
            await s.execute(_LOCK)
            found = await load_active(s)
            if found is None:
                return []
            defn = found[1]
            cfg = defn.factory.improver
            if not cfg.bench_suite:
                return []
            failed, oldest = await failures_since(s, await last_improvement_at(s))
            decision = trigger_due(cfg, failed_jobs=failed, oldest_at=oldest, now=now)
            if decision.reason is None:
                return []
            return await start_cycle(s, defn, trigger=decision.reason, now=now)

    # -- claims -----------------------------------------------------------------
    async def _max_crashes(self) -> int:
        try:
            _, defn = await self.deps.core.definitions.active()
            return defn.factory.defaults.max_job_crashes
        except Exception:
            return Defaults().max_job_crashes

    async def _claim_job(
        self, s: AsyncSession, job_id: UUID, now: datetime
    ) -> tuple[int, int] | None:
        """Take the job's lease (epoch + 1); an expired lease of a dead worker counts as a crash."""
        row = (
            await s.execute(
                text(
                    "UPDATE app.jobs SET lease_owner = :w, "
                    "lease_until = CAST(:now AS timestamptz) + make_interval(secs => :ls), "
                    "lease_epoch = lease_epoch + 1, "
                    "crash_count = crash_count "
                    "+ CASE WHEN lease_owner IS NOT NULL THEN 1 ELSE 0 END, "
                    "retry_after = CASE WHEN lease_owner IS NOT NULL THEN "
                    "CAST(:now AS timestamptz) + make_interval(secs => 60 * power(2, crash_count)) "
                    "ELSE retry_after END, "
                    "started_at = coalesce(started_at, CAST(:now AS timestamptz)), "
                    "updated_at = now() "
                    "WHERE id = (SELECT id FROM app.jobs WHERE id = :id "
                    "AND (lease_until IS NULL OR lease_until < CAST(:now AS timestamptz)) "
                    "AND (retry_after IS NULL OR retry_after <= CAST(:now AS timestamptz)) "
                    "AND stage <> ALL(CAST(:done AS text[])) FOR UPDATE SKIP LOCKED) "
                    "RETURNING lease_epoch, crash_count"
                ),
                {
                    "w": self.worker_id,
                    "now": now,
                    "ls": self.lease_s,
                    "id": job_id,
                    "done": list(_FINISHED_JOB),
                },
            )
        ).first()
        return None if row is None else (int(row[0]), int(row[1]))

    async def _claim_one(self, imp_id: UUID, now: datetime) -> tuple[Improvement, int, int] | None:
        async with self.sessions() as s, s.begin():
            row = await s.get(Improvement, imp_id, with_for_update=True, populate_existing=True)
            if row is None or row.job_id is None or row.status in IMPROVEMENT_DONE:
                return None
            got = await self._claim_job(s, row.job_id, now)
            if got is None:
                return None
            s.expunge(row)
            return row, got[0], got[1]

    async def _reclaim(self, now: datetime) -> None:
        async with self.sessions() as s:
            ids = list(
                await s.scalars(
                    text(
                        "SELECT i.id FROM app.improvements i JOIN app.jobs j ON j.id = i.job_id "
                        "WHERE i.status IN ('queued', 'analyzing', 'proposing', 'validating', "
                        "'benchmarking') AND j.stage <> ALL(CAST(:done AS text[])) AND "
                        "((j.lease_until IS NOT NULL "
                        "AND j.lease_until < CAST(:now AS timestamptz)) "
                        "OR (i.status IN ('analyzing', 'proposing', 'validating') "
                        "AND j.lease_until IS NULL)) ORDER BY i.created_at, i.id"
                    ).bindparams(now=now, done=list(_FINISHED_JOB))
                )
            )
        for imp_id in ids:
            claimed = await self._claim_one(imp_id, now)
            if claimed is not None:
                await self._drive(*claimed, now=now, resume=False)

    async def _claim_queued(self, now: datetime) -> None:
        async with self.sessions() as s, s.begin():
            await s.execute(_LOCK)
            busy = await s.scalar(
                text(
                    "SELECT count(*) FROM app.improvements i LEFT JOIN app.jobs j "
                    "ON j.id = i.job_id WHERE i.status = 'benchmarking' OR "
                    "(i.status <> ALL(CAST(:done AS text[])) AND j.lease_until IS NOT NULL "
                    "AND j.lease_until >= CAST(:now AS timestamptz))"
                ),
                {"done": sorted(IMPROVEMENT_DONE), "now": now},
            )
            if busy:
                return
            ids = list(
                await s.scalars(
                    text(
                        "SELECT i.id FROM app.improvements i JOIN app.jobs j ON j.id = i.job_id "
                        "WHERE i.status = 'queued' AND j.lease_until IS NULL "
                        "ORDER BY i.created_at, i.id FOR UPDATE OF i SKIP LOCKED"
                    )
                )
            )
            picked: tuple[Improvement, int, int] | None = None
            for imp_id in ids:
                row = await s.get(Improvement, imp_id, populate_existing=True)
                if row is None or row.job_id is None:
                    continue
                got = await self._claim_job(s, row.job_id, now)
                if got is not None:
                    s.expunge(row)
                    picked = (row, got[0], got[1])
                    break
        if picked is not None:
            await self._drive(*picked, now=now, resume=False)

    # -- driving ----------------------------------------------------------------
    async def _drive(
        self, row: Improvement, epoch: int, crashes: int, *, now: datetime, resume: bool
    ) -> None:
        assert row.job_id is not None
        job_id = row.job_id
        if crashes >= await self._max_crashes():
            await self._fail(
                row.id,
                job_id,
                f"worker stopped {crashes} times while running this improvement",
                now,
            )
            await self._release(job_id, epoch)
            return
        graph = build_improve_graph(self.deps, self.checkpointer)
        cfg: Any = {
            "configurable": {
                "thread_id": str(row.id),
                "worker_id": self.worker_id,
                "lease_epoch": epoch,
            }
        }
        lost: list[bool] = []
        run = asyncio.create_task(self._run_graph(graph, cfg, row, resume))
        renewer = asyncio.create_task(self._renew(job_id, epoch, run, lost))
        try:
            await run
        except asyncio.CancelledError:
            if lost:
                log.warning("improve.lease_lost", job_id=str(job_id))
                return  # the job is not ours any more: no release, no write
            raise
        except Exception as exc:
            log.exception("improve.graph_crashed", improvement=str(row.id))
            try:
                await self._record_crash(row.id, job_id, epoch, exc, now)
            except Exception:
                log.exception("improve.crash_not_recorded")
                return  # the lease runs out and the next claim counts the crash
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        await self._release(job_id, epoch)

    async def _run_graph(self, graph: Any, cfg: Any, row: Improvement, resume: bool) -> None:
        snap = await graph.aget_state(cfg)
        has_checkpoint = bool(snap.values) or bool(snap.next)
        if resume:
            if snap.next:
                await graph.ainvoke(
                    Command(resume={"bench_done": True}), cfg, durability=DURABILITY
                )
            return
        if has_checkpoint:
            try:
                await graph.ainvoke(None, cfg, durability=DURABILITY)
                return
            except EmptyInputError:
                pass  # no usable checkpoint after all: start over
        await graph.ainvoke(improve_initial_state(row), cfg, durability=DURABILITY)

    async def _renew(
        self, job_id: UUID, epoch: int, run: asyncio.Task[Any], lost: list[bool]
    ) -> None:
        while True:
            await asyncio.sleep(self.renew_s)
            try:
                async with self.sessions() as s:
                    ok = await renew_lease(
                        s,
                        job_id=job_id,
                        worker_id=self.worker_id,
                        lease_epoch=epoch,
                        lease_s=self.lease_s,
                    )
            except Exception as exc:  # the database may be restarting: try again next time
                log.warning("improve.renew_failed", error=safe_error(exc))
                continue
            if not ok:
                lost.append(True)
                run.cancel()
                return

    async def _release(self, job_id: UUID, epoch: int) -> None:
        async with self.sessions() as s:
            await release_job(s, job_id=job_id, worker_id=self.worker_id, lease_epoch=epoch)

    async def _record_crash(
        self, imp_id: UUID, job_id: UUID, epoch: int, exc: BaseException, now: datetime
    ) -> None:
        error = (str(exc) or type(exc).__name__)[:2000]
        max_crashes = await self._max_crashes()
        async with self.sessions.begin() as s:
            if not await holds_lease(s, job_id=job_id, worker_id=self.worker_id, lease_epoch=epoch):
                return
            count = int(
                await s.scalar(
                    text(
                        "UPDATE app.jobs SET crash_count = crash_count + 1, error = :e, "
                        "retry_after = CAST(:now AS timestamptz) "
                        "+ make_interval(secs => 60 * power(2, crash_count)), updated_at = now() "
                        "WHERE id = :id RETURNING crash_count"
                    ),
                    {"e": error, "now": now, "id": job_id},
                )
                or 0
            )
        if count >= max_crashes:
            await self._fail(imp_id, job_id, error, now)

    async def _set_status(
        self,
        s: AsyncSession,
        row: Improvement,
        status: str,
        *,
        error: str | None = None,
        now: datetime,
    ) -> None:
        row.status = status
        if error is not None:
            row.error = error[:MAX_ERROR]
        row.updated_at = now
        await record_event(
            s,
            row.job_id,
            "improvement.updated",
            {"id": str(row.id), "status": status, "role": row.role_group},
        )

    async def _fail(self, imp_id: UUID, job_id: UUID, error: str, now: datetime) -> None:
        async with self.sessions.begin() as s:
            row = await s.get(Improvement, imp_id, with_for_update=True, populate_existing=True)
            if row is not None and row.status not in IMPROVEMENT_DONE:
                await self._set_status(s, row, "failed", error=error, now=now)
            await set_stage(s, job_id, Stage.FAILED, error=error[:2000], finished_at=now)

    # -- waiting graphs ---------------------------------------------------------
    async def _resume_waiting(self, now: datetime) -> None:
        async with self.sessions() as s:
            rows = list(
                await s.scalars(
                    select(Improvement)
                    .where(
                        Improvement.status == "benchmarking",
                        Improvement.bench_run_before.is_not(None),
                        Improvement.bench_run_after.is_not(None),
                    )
                    .order_by(Improvement.created_at, Improvement.id)
                )
            )
        for row in rows:
            try:
                ids = [row.bench_run_before, row.bench_run_after]
                statuses = [await self.deps.bench.status(i) for i in ids if i is not None]
            except Exception as exc:
                log.warning("improve.bench_status_failed", error=safe_error(exc))
                continue
            if "running" in statuses:
                continue
            claimed = await self._claim_one(row.id, now)
            if claimed is not None:
                await self._drive(*claimed, now=now, resume=True)

    async def _deadline(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=IMPROVER_BENCH_DEADLINE_H)
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(Improvement.id, Improvement.job_id).where(
                        Improvement.status == "benchmarking", Improvement.updated_at < cutoff
                    )
                )
            ).all()
        for imp_id, job_id in rows:
            async with self.sessions.begin() as s:
                row = await s.get(Improvement, imp_id, with_for_update=True, populate_existing=True)
                if row is None or row.status != "benchmarking":
                    continue
                await self._set_status(
                    s,
                    row,
                    "failed",
                    error=f"benchmark did not finish in {IMPROVER_BENCH_DEADLINE_H} h",
                    now=now,
                )
                if job_id is not None:  # the improver job is closed so later improvements can run
                    await set_stage(s, job_id, Stage.CLOSED, finished_at=now)

    # -- pull requests ----------------------------------------------------------
    async def _refresh_prs(self) -> None:
        if self.deps.github is None:
            return
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    select(Improvement.id, Improvement.branch).where(
                        Improvement.status == "pr_open", Improvement.branch.is_not(None)
                    )
                )
            ).all()
        if not rows:
            return
        slug = self.deps.workspace().slug
        if slug is None:
            return
        gh = ImproverGitHub(self.deps.github(slug))
        for imp_id, branch in rows:
            try:
                pr = await gh.find_pr(str(branch))
            except Exception as exc:
                log.warning("improve.pr_lookup_failed", error=safe_error(exc))
                continue
            if pr is None:
                continue
            new = "merged" if pr.merged else ("closed" if pr.state == "closed" else None)
            if new is None:
                continue
            async with self.sessions.begin() as s:
                row = await s.get(Improvement, imp_id, with_for_update=True, populate_existing=True)
                if row is not None and row.status == "pr_open":
                    await self._set_status(s, row, new, now=self.deps.clock())
