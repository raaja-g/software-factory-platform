"""Nodes of the requirements graph: prepare, requirements and finalize."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from langgraph.types import Command

from swfactory.backlogs import approve_backlog
from swfactory.db.repo import LeaseLost, holds_lease, set_stage
from swfactory.domain.types import Role, RunStatus, Stage, ToolPolicy
from swfactory.graph.app_context import render_app_md, render_feature_md, stack_from_agents_md
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import LEASE, enter_stage, job_uuid
from swfactory.graph.routing import REQUIREMENTS_GATE_REASON, budget_update, failure_update
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, pinned_definition, run_agent_step
from swfactory.roles.contracts import parse_requirements
from swfactory.sandbox.paths import REPO

log = structlog.get_logger(__name__)

TREE_MAX_PATHS = 400
README_MAX_CHARS = 40_000


def make_prepare(deps: Deps):
    async def prepare(state: JobState) -> dict:
        job_id = job_uuid(state)
        job, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        await deps.git.prepare(job, app, box)  # a read-only checkout: nothing is committed
        slug = next(k for k, v in defn.factory.apps.items() if v is app)
        stack = stack_from_agents_md(await box.read_text(f"{REPO}/AGENTS.md"))
        await deps.artifacts.put(job_id, "feature.md", render_feature_md(job.title, job.feature))
        await deps.artifacts.put(job_id, "app.md", render_app_md(slug, app, stack=stack))
        res = await box.exec(["git", "ls-files"], cwd=REPO)
        paths = [p for p in res.stdout.splitlines() if p.strip()]
        tree = "\n".join(paths[:TREE_MAX_PATHS])
        if len(paths) > TREE_MAX_PATHS:
            tree += f"\nomitted: {len(paths) - TREE_MAX_PATHS} more paths"
        await deps.artifacts.put(job_id, "tree.md", tree + "\n")
        readme = next((p for p in paths if "/" not in p and p.upper().startswith("README")), None)
        if readme is not None:
            text = await box.read_text(f"{REPO}/{readme}")
            if text is not None:
                await deps.artifacts.put(job_id, "readme.md", text[:README_MAX_CHARS])
        await enter_stage(deps, job_id, Stage.SPEC)
        return {}

    return prepare


def make_requirements(deps: Deps):
    async def requirements(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.SPEC)
        out = await run_agent_step(
            deps,
            job_id=job_id,
            role=Role.REQUIREMENTS,
            round=state.get("round", 1),
            inputs=["feature.md", "app.md", "tree.md", "readme.md", "rework-*.md"],
            output_name="requirements.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_ONLY,
        )
        if out.status == RunStatus.BUDGET:
            return Command(goto="human_gate", update=budget_update("requirements"))
        if out.status != RunStatus.OK:
            return Command(goto="human_gate", update=failure_update("requirements"))
        return Command(
            goto="human_gate",
            update={"gate_reason": REQUIREMENTS_GATE_REASON, "retry_node": "requirements"},
        )

    return requirements


def make_finalize(deps: Deps):
    async def finalize(state: JobState) -> dict:
        job_id = job_uuid(state)
        job, defn, _ = await pinned_definition(deps, job_id)
        stage = Stage.MERGED if state.get("outcome") == Stage.MERGED.value else Stage.CLOSED
        items = None
        if stage == Stage.MERGED:
            text = await deps.artifacts.get_text(job_id, "requirements.md")
            items = parse_requirements(
                text or "", max_items=defn.factory.defaults.max_backlog_items
            )
        async with deps.sessions.begin() as s:
            lease = LEASE.get()
            if lease is not None and not await holds_lease(
                s, job_id=job_id, worker_id=lease[0], lease_epoch=lease[1]
            ):
                raise LeaseLost(str(job_id))
            if items is not None and job.backlog_id is not None:
                await approve_backlog(
                    s,
                    backlog_id=job.backlog_id,
                    items=items,
                    approved_by=state.get("approved_by") or "",
                )
            await set_stage(s, job_id, stage, finished_at=datetime.now(UTC))
        try:
            await deps.sandboxes.destroy(job_id)
        except Exception as exc:  # the reaper removes leftovers (M4.4)
            log.warning("finalize.cleanup_failed", error=str(exc))
        return {"outcome": stage.value}

    return finalize
