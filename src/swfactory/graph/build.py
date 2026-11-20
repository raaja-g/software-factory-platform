"""build_job_graph: the spine's job graph."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, TimeoutPolicy

from swfactory.db.repo import LeaseLost, holds_lease, reset_crashes
from swfactory.db.tables import Job
from swfactory.definition.models import Defaults, Definition
from swfactory.domain.types import DEFAULT_REVIEWERS
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import (
    LEASE,
    after_reviews,
    approve,
    build,
    deliver,
    finalize,
    human_gate,
    prepare,
    review,
    score,
    spec,
    triage,
    verify,
)
from swfactory.graph.nodes import requirements as req_nodes
from swfactory.graph.routing import failure_update
from swfactory.graph.state import JobState, ReviewTask
from swfactory.graph.steps import step_budget_s

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

GRAPH_VERSION = 1
SCORE_RUN_TIMEOUT_S = 1800
GRAPH_COMPAT: Mapping[int, frozenset[int]] = {1: frozenset({1})}
NODE_NAMES = (
    "prepare",
    "triage",
    "spec",
    "build",
    "verify",
    "review",
    "after_reviews",
    "approve",
    "human_gate",
    "deliver",
    "finalize",
    "score",
)
REQUIREMENTS_NODE_NAMES = ("prepare", "requirements", "human_gate", "finalize")
AGENT_NODES = ("triage", "spec", "build", "review", "approve")
_NO_HANDLER_GATE = ("finalize", "score")
PAUSE_ENV = (
    "FACTORY_TEST_PAUSE"  # "<node>:<seconds>:<marker path>", read only with FACTORY_TEST_HOOKS=1
)


def node_timeouts(defaults: Defaults) -> dict[str, int]:
    """The whole-step budget (M2.4) for each agent node."""
    return {n: step_budget_s(defaults, n) for n in AGENT_NODES}


def _app_for(job: Job, defn: Definition) -> Any:
    apps = defn.factory.apps
    slug = getattr(job, "app_slug", None)
    if slug in apps:
        return apps[slug]
    return next(iter(apps.values())) if len(apps) == 1 else None


def initial_state(job: Job, defn: Definition) -> JobState:
    defaults = defn.factory.defaults
    app = _app_for(job, defn)
    max_rounds = getattr(app, "max_rounds", None)
    max_fix = getattr(app, "max_fix_attempts", None)
    return JobState(
        job_id=str(job.id),
        kind=str(job.kind),
        round=1,
        max_rounds=max_rounds if max_rounds is not None else defaults.max_rounds,
        fix_attempts=0,
        max_fix_attempts=max_fix if max_fix is not None else defaults.max_fix_attempts,
        reviewers=list(DEFAULT_REVIEWERS),
        reviews={},
        decision=None,
        gate_reason=None,
        retry_node=None,
        human=None,
        outcome=None,
    )


def make_error_handler(
    node: str, *, gate_all: bool = False
) -> Callable[[JobState, NodeError], Command]:
    """A node's error handler: failures go to the human gate, except score and finalize."""

    def handler(state: JobState, error: NodeError) -> Command:
        # a NodeError when langgraph calls it; the bare exception is accepted too
        cause = error.error if isinstance(error, NodeError) else error
        if isinstance(cause, LeaseLost):  # never write a gate into a thread another worker drives
            raise cause
        if (
            node in _NO_HANDLER_GATE and not gate_all
        ):  # L13, L14: scoring never reaches a person; finalize runs after the outcome
            return Command(goto=END)
        update = failure_update(node)
        reason = getattr(cause, "gate_reason", None)  # BudgetExceeded, NoModelAvailable
        if isinstance(reason, str) and reason:
            update["gate_reason"] = reason
        return Command(goto="human_gate", update=update)

    handler.__name__ = f"on_error_{node}"
    return handler


def _leased(deps: Deps, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a node: under a worker (``worker_id`` in the config), check the lease on entry."""

    async def node(state: Any, config: RunnableConfig) -> Any:
        conf = config.get("configurable") or {}
        worker_id, epoch = conf.get("worker_id"), conf.get("lease_epoch")
        if worker_id is None or epoch is None:
            return await fn(state)
        job_id = UUID(str(state["job_id"]))
        async with deps.sessions.begin() as s:
            if not await holds_lease(s, job_id=job_id, worker_id=worker_id, lease_epoch=epoch):
                raise LeaseLost(str(job_id))
        token = LEASE.set((worker_id, int(epoch)))
        try:
            result = await fn(state)
        finally:
            LEASE.reset(token)
        async with deps.sessions.begin() as s:  # a completed node ends any run of crashes
            await reset_crashes(s, job_id)
        return result

    node.__name__ = getattr(fn, "__name__", "node")
    return node


def _paused(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Test hook: first entry creates the marker file, then sleeps so a test can kill the worker."""
    spec = os.environ.get(PAUSE_ENV, "")
    node, _, rest = spec.partition(":")
    if os.environ.get("FACTORY_TEST_HOOKS") != "1" or node != name:
        return fn
    seconds, _, marker = rest.partition(":")

    async def paused(state: Any) -> Any:
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return await fn(state)
        os.close(fd)
        await asyncio.sleep(float(seconds))
        return await fn(state)

    paused.__name__ = getattr(fn, "__name__", "node")
    return paused


def build_job_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver, *, timeouts: Mapping[str, int] | None = None
) -> CompiledStateGraph:
    limits = dict(timeouts) if timeouts is not None else node_timeouts(Defaults())
    makers = {
        "prepare": prepare,
        "triage": triage,
        "spec": spec,
        "build": build,
        "verify": verify,
        "review": review,
        "after_reviews": after_reviews,
        "approve": approve,
        "human_gate": human_gate,
        "deliver": deliver,
        "finalize": finalize,
        "score": score,
    }
    g = StateGraph(JobState)
    for name in NODE_NAMES:
        kwargs: dict[str, Any] = {}
        if name in AGENT_NODES:
            kwargs["timeout"] = TimeoutPolicy(run_timeout=limits[name] + 60)
        if name == "after_reviews":
            kwargs["defer"] = True
        if name == "review":
            kwargs["input_schema"] = ReviewTask
        if name == "score":
            kwargs["timeout"] = TimeoutPolicy(run_timeout=SCORE_RUN_TIMEOUT_S)
        kwargs["error_handler"] = make_error_handler(name)
        g.add_node(name, _leased(deps, _paused(name, makers[name].make(deps))), **kwargs)
    g.add_edge(START, "prepare")
    g.add_edge("triage", "spec")
    g.add_edge("review", "after_reviews")
    g.add_edge("finalize", "score")
    g.add_edge("score", END)
    return g.compile(checkpointer=checkpointer)


def build_requirements_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver, *, timeouts: Mapping[str, int] | None = None
) -> CompiledStateGraph:
    limits = (
        dict(timeouts)
        if timeouts is not None
        else {"requirements": step_budget_s(Defaults(), "requirements")}
    )
    makers = {
        "prepare": req_nodes.make_prepare(deps),
        "requirements": req_nodes.make_requirements(deps),
        "human_gate": human_gate.make(deps),
        "finalize": req_nodes.make_finalize(deps),
    }
    g = StateGraph(JobState)
    for name in REQUIREMENTS_NODE_NAMES:
        kwargs: dict[str, Any] = {"error_handler": make_error_handler(name, gate_all=True)}
        if name == "requirements":
            kwargs["timeout"] = TimeoutPolicy(run_timeout=limits["requirements"] + 60)
        g.add_node(name, _leased(deps, _paused(name, makers[name])), **kwargs)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "requirements")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def graph_for(kind: str) -> Callable[..., CompiledStateGraph]:
    return build_requirements_graph if kind == "requirements" else build_job_graph
