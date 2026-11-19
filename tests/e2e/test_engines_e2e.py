"""One small feature per engine on cheap models (opt-in, real gateway and credentials)."""

import asyncio
import os
import subprocess
import time
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from swfactory.db.tables import AgentRun
from swfactory.db.tables import Job as JobRow
from swfactory.domain.types import Stage
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.e2e

ENGINE = {
    "e2e-opencode": "opencode",
    "e2e-claude-code": "claude_code",
    "e2e-gemini-cli": "gemini_cli",
}
ENABLED = {e.strip() for e in os.environ.get("FACTORY_E2E_ENGINES", "").split(",") if e.strip()}
TERMINAL = {Stage.MERGED, Stage.NEEDS_HUMAN, Stage.FAILED}

_stack = None


@pytest.fixture(autouse=True)
def _bind_stack(request):
    global _stack  # noqa: PLW0603
    _stack = None
    yield
    _stack = None


async def wait_for_stage(job_id: UUID, stages: set[Stage], timeout_s: float):
    assert _stack is not None
    deadline = time.monotonic() + timeout_s
    while True:
        async with _stack.sessions() as s:
            job = await s.get(JobRow, job_id)
        assert job is not None
        if Stage(job.stage) in stages or time.monotonic() > deadline:
            return job
        await asyncio.sleep(5)


async def runs_for(job_id: UUID) -> list[AgentRun]:
    assert _stack is not None
    async with _stack.sessions() as s:
        rows = await s.scalars(select(AgentRun).where(AgentRun.job_id == job_id))
        return list(rows)


def _git(*args: str) -> str:
    assert _stack is not None
    bare = _stack.repos_dir / "calc-app.git"
    out = subprocess.run(
        ["git", "-C", str(bare), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return out.stdout


def bare_repo_file(app: str, branch: str, path: str) -> str:
    assert app == "calc-app"
    return _git("show", f"refs/heads/{branch}:{path}")


def builder_commit_trailers(job) -> list[list[str]]:
    """Trailer keys of every non-merge commit the factory made for the job."""
    out = _git(
        "log",
        "refs/heads/main",
        "--no-merges",
        "--fixed-strings",
        f"--grep=Factory-Job: {job.id}",
        "--format=%x00%(trailers:only,unfold)",
    )
    commits = []
    for block in out.split("\x00")[1:]:
        keys = [ln.split(":", 1)[0] for ln in block.splitlines() if ":" in ln]
        commits.append(keys)
    return commits


@pytest.mark.parametrize("profile", ["e2e-opencode", "e2e-claude-code", "e2e-gemini-cli"])
async def test_small_feature(profile, stack, catalog):  # noqa: ANN001
    global _stack  # noqa: PLW0603
    _stack = stack
    if ENABLED and ENGINE[profile] not in ENABLED:
        pytest.skip(f"opt-in: {ENGINE[profile]} not in FACTORY_E2E_ENGINES")
    store_defn = await catalog.definitions.active()
    roles = store_defn[1].profiles[profile].roles
    aliases = {spec.model for spec in roles.values()}
    for alias in aliases:
        info = await catalog.get(alias)
        if info is None or not info.available:
            pytest.skip(f"opt-in: alias {alias} is unavailable in the catalog")

    async with stack.sessions() as s, s.begin():
        job = await enqueue_job(
            s,
            app_slug="calc-app",
            feature="Add subtract(a, b) to calc.py with a test.",
            title=None,
            source="cli",
            source_ref=None,
            requested_by="e2e",
            profile_name=profile,
            budget_usd=Decimal("1"),
        )
    final = await wait_for_stage(job.id, TERMINAL, timeout_s=45 * 60)
    assert Stage(final.stage) == Stage.MERGED, final.error
    runs = await runs_for(job.id)
    builders = {r.engine for r in runs if r.role == "builder"}
    assert builders == {ENGINE[profile]}
    assert all(r.engine_version not in (None, "unknown") for r in runs if r.engine != "direct")
    assert not any(r.fallback_detected for r in runs)
    assert sum((r.cost_usd or Decimal(0)) for r in runs) < Decimal("1")
    assert "def subtract" in bare_repo_file("calc-app", "main", "calc.py")
    commits = builder_commit_trailers(job)
    assert commits
    assert all(set(keys) == {"Factory-Job", "Factory-Role", "Factory-Model"} for keys in commits)
