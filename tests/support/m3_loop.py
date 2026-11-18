"""Scripts and the run result for the M3 loop tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from swfactory.db.tables import AgentRun, Job
from swfactory.domain.types import Role, RunStatus
from swfactory.engines.fake import FakeScript
from swfactory.engines.registry import get_engine

V1 = Path(__file__).parents[1] / "fixtures" / "v1"
KINDS = ("security", "ux", "ui", "code")


def happy(overrides: Mapping[tuple[str, int], list[str]] | None = None) -> FakeScript:
    """All-PASS script; overrides are {(role, round): [output per attempt]}."""
    turns: list[dict[str, Any]] = []
    for (role, rnd), outputs in (overrides or {}).items():
        for n, out in enumerate(outputs, 1):
            turns.append({"role": role, "round": rnd, "attempt": n, "output": out})
    return FakeScript.from_dict({"turns": turns})


def v1_feature(name: str) -> str:
    return (V1 / name / "feature.md").read_text()


def v1_replay(name: str) -> FakeScript:
    d = V1 / name
    rounds = sorted(int(p.name.split("-")[1]) for p in d.glob("round-*"))
    turns: list[dict[str, Any]] = [
        {"role": "spec-writer", "output": (d / "spec.md").read_text()},
        {"role": "builder", "output": (d / "build.md").read_text()},
        {"role": "approver", "output": (d / "decision.md").read_text()},
    ]
    for r in rounds:
        for kind in KINDS:
            f = d / f"round-{r}" / f"review-{kind}.md"
            if f.exists():
                turns.append({"role": f"reviewer-{kind}", "round": r, "output": f.read_text()})
    return FakeScript.from_dict({"turns": turns})


async def collect(sessions, deps, job_id, state, box) -> SimpleNamespace:
    async with sessions() as s:
        job = await s.get(Job, job_id, populate_existing=True)
        runs = list((await s.scalars(select(AgentRun).where(AgentRun.job_id == job_id))).all())
    artifacts = {}
    for m in await deps.artifacts.list(job_id):
        artifacts[m.path] = await deps.artifacts.get_text(job_id, m.path)

    def runs_for(role: Role) -> list[AgentRun]:
        return [r for r in runs if r.role == role.value]

    def ok_runs() -> dict[str, int]:
        out: dict[str, int] = {}
        for r in runs:
            if r.status == RunStatus.OK.value:
                out[r.role] = out.get(r.role, 0) + 1
        return out

    def tasks(role: Role):
        return [t for t in get_engine("fake").tasks if t.role == role]  # type: ignore[attr-defined]

    return SimpleNamespace(
        job=job,
        state=state,
        artifacts=artifacts,
        box=box,
        runs_for=runs_for,
        ok_runs=ok_runs,
        tasks=tasks,
    )
