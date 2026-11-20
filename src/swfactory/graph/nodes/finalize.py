from __future__ import annotations

from datetime import UTC, datetime

import structlog

from swfactory.domain.types import TERMINAL_STAGES, Stage
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, keep_job_branch, pinned_definition

log = structlog.get_logger(__name__)


def make(deps: Deps):
    async def _hidden_checks(job_id) -> None:
        from swfactory.bench.runner import run_hidden_checks, trial_task

        try:
            _, defn, app = await pinned_definition(deps, job_id)
            task = await trial_task(deps.sessions, defn, job_id)
        except Exception as exc:
            log.warning("finalize.hidden_checks_skipped", error=str(exc))
            return
        if task is not None:
            await run_hidden_checks(deps, job_id, task, runner=_runner(defn, app))

    async def _reference_checks(job_id) -> None:
        from swfactory.bench.reference import (
            REFERENCE_LOG,
            CheckVerdict,
            reference_task,
            reference_verdict,
            store_verdict,
        )
        from swfactory.bench.runner import run_hidden_checks

        _, defn, app = await pinned_definition(deps, job_id)
        found = await reference_task(deps.sessions, defn, job_id)
        if found is None:
            raise RuntimeError("reference job without a check row or task")
        key, _, task = found
        res = await run_hidden_checks(
            deps,
            job_id,
            task,
            runner=_runner(defn, app),
            fresh=True,
            also_base=True,
            log_path=REFERENCE_LOG,
        )
        if res.error is not None:
            verdict = CheckVerdict(False, res.error)
        elif not res.files:
            verdict = CheckVerdict(False, "the task has no check files")
        else:
            verdict = reference_verdict(res.files, res.base_files)
        await store_verdict(
            deps.sessions,
            key,
            verdict,
            {"at_merge": dict(res.files), "at_base": dict(res.base_files)},
        )

    async def finalize(state: JobState) -> dict:
        job_id = job_uuid(state)
        stage = Stage(state.get("outcome") or Stage.CLOSED.value)
        if state.get("bench_reference"):
            await _reference_checks(job_id)
            stage = Stage.CLOSED
        elif state.get("kind") == "benchmark":  # before the sandbox goes: the checks need the tree
            await _hidden_checks(job_id)
        fields = {"finished_at": datetime.now(UTC)} if stage in TERMINAL_STAGES else {}
        await enter_stage(deps, job_id, stage, **fields)  # the stage first, then cleanup
        try:
            await keep_job_branch(deps, job_id)  # never raises
        except Exception as exc:
            log.warning("finalize.keep_branch_failed", error=str(exc))
        try:
            await deps.sandboxes.destroy(job_id)
        except Exception as exc:  # the reaper removes leftovers (M4.4)
            log.warning("finalize.cleanup_failed", error=str(exc))
        return {"outcome": stage.value}

    return finalize
