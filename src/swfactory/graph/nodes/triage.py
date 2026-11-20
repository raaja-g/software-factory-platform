from __future__ import annotations

from swfactory.domain.types import Stage, TriageMode
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState
from swfactory.graph.steps import benchmark_laya_pins, benchmark_overrides, pinned_definition
from swfactory.models.triage import triage_to_json


def make(deps: Deps):
    async def triage(state: JobState) -> dict:
        job_id = job_uuid(state)
        job, defn, app = await pinned_definition(deps, job_id)
        ov = await benchmark_overrides(deps, job)
        modes = {q: TriageMode(m) for q, m in (ov or {}).get("triage_modes", {}).items()} or None
        result = await deps.triage.triage(
            job, app, modes=modes, pins=await benchmark_laya_pins(deps, job) or None
        )
        fields: dict = {
            "triage": {
                **(job.triage if isinstance(job.triage, dict) else {}),
                **triage_to_json(result),
            }
        }
        if not job.profile_name and result.profile_name:
            fields["profile_name"] = result.profile_name
        # intake always fills budget_usd (request, else app, else factory default): a value equal to
        # that fallback means nobody asked for a specific budget
        fallback = app.budget_usd or defn.factory.budgets.job_usd
        if result.budget_usd is not None and job.budget_usd == fallback:
            fields["budget_usd"] = result.budget_usd
        await enter_stage(deps, job_id, Stage.TRIAGE, **fields)
        from_job = bool(job.require_human_approval)
        return {
            "reviewers": list(result.reviewers),
            "require_human_approval": result.require_human_approval or from_job,
            "approval_from_job": from_job and not result.require_human_approval,
        }

    return triage
