import asyncio
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from swfactory.apps import RegisterDeps
from swfactory.db.tables import Job, RunRequest
from swfactory.definition.loader import LoadResult, defaults_dir, load_definition
from swfactory.definition.models import AppDef
from swfactory.definition.store import DefinitionStore, load_active
from swfactory.definition.writer import SaveResult
from swfactory.gitops.folder import FolderRepo
from swfactory.runs import RunDeps, create_run_request, run_request_tick
from swfactory.settings import FactoryMode, Settings

pytestmark = pytest.mark.db

PR_URL = "https://github.com/org/def/pull/3"


class FakeWriter:
    def __init__(self, pr=False):
        self.saved: list[tuple[str, str, str]] = []
        self.pr = pr

    async def save_file(self, path, content, *, message, author):
        self.saved.append((path, content, author))
        if self.pr:
            return SaveResult("pr", None, PR_URL)
        return SaveResult("commit", "a" * 40, None)


class Clock:
    def __init__(self):
        self.t = datetime.now(UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


@dataclass
class Harness:
    deps: RunDeps
    clock: Clock
    pending: list[tuple[str, str]]
    store: DefinitionStore
    n: int = 0

    @property
    def register(self):
        return self.deps.register

    async def apply_saved(self, s=None):
        """Merge every saved app file into a new active definition."""
        found = None
        async with self.store.sessions() as s2:
            found = await load_active(s2)
        assert found is not None
        defn = found[1]
        apps = dict(defn.factory.apps)
        for path, content, _ in self.register.writer.saved:  # type: ignore[attr-defined]
            apps[Path(path).stem] = AppDef.model_validate(yaml.safe_load(content))
        defn = defn.model_copy(update={"factory": defn.factory.model_copy(update={"apps": apps})})
        self.n += 1
        await self.store.save(LoadResult(f"sha-{self.n}", "test", defn, ()))
        return defn

    async def merge_definition_pr(self):
        await self.apply_saved()


async def make_harness(sessions, tmp_path, *, pr):
    store = DefinitionStore(sessions)
    base = load_definition(str(defaults_dir()))
    assert await store.save(base)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    settings = Settings(
        FACTORY_MODE=FactoryMode.PERSONAL,
        FACTORY_HOST_REPOS_ROOT=str(work),
        FACTORY_HOST_HOME=str(tmp_path),
    )
    folders = FolderRepo(str(work), str(work), bare_dir=tmp_path / "repos")
    writer = FakeWriter(pr=pr)
    holder: dict[str, Harness] = {}

    async def sync(s):
        return await holder["h"].apply_saved()

    reg = RegisterDeps(writer, folders, settings, None, sync)  # pyright: ignore[reportArgumentType]
    clock = Clock()
    h = Harness(RunDeps(reg, clock), clock, [], store)
    holder["h"] = h
    return h


@pytest.fixture
async def harness(sessions, tmp_path):
    return await make_harness(sessions, tmp_path, pr=False)


@pytest.fixture
async def harness_pr(sessions, tmp_path):
    return await make_harness(sessions, tmp_path, pr=True)


@pytest.fixture
def run_deps(harness):
    return harness.deps


@pytest.fixture
def repo_dir(tmp_path):
    def make(name):
        d = tmp_path / "work" / name
        d.mkdir(parents=True, exist_ok=True)
        if not (d / ".git").exists():
            ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
            (d / "a.txt").write_text("x")
            subprocess.run(["git", "-C", str(d), "add", "."], check=True)
            subprocess.run(["git", *ident, "-C", str(d), "commit", "-qm", "i"], check=True)
        return d

    return make


async def new_request(sessions, *, repo, requirement):
    async with sessions() as s, s.begin():
        return await create_run_request(
            s, repo=repo, requirement=requirement, profile=None, ci_safe=False,
            budget_usd=None, requested_by="me@x.io",
        )  # fmt: skip


async def get_request(sessions, rid):
    async with sessions() as s:
        return await s.get(RunRequest, rid, populate_existing=True)


async def get_job(sessions, jid):
    async with sessions() as s:
        return await s.get(Job, jid)


async def count_jobs(sessions, *, kind):
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(Job).where(Job.kind == kind))


async def test_tick_registers_then_starts_the_requirements_job(sessions, run_deps, repo_dir):
    rid = await new_request(sessions, repo=str(repo_dir("todo-app")), requirement="A todo list")
    assert await run_request_tick(sessions, run_deps, worker_id="w1") is True
    row = await get_request(sessions, rid)
    assert (row.status, row.app_slug) == ("started", "todo-app")
    assert row.job_id is not None and row.backlog_id is not None
    job = await get_job(sessions, row.job_id)
    assert (job.kind, job.source) == ("requirements", "api") and "A todo list" in job.feature
    assert await run_request_tick(sessions, run_deps, worker_id="w1") is False


async def test_a_second_run_on_the_same_repo_registers_once(sessions, run_deps, repo_dir):
    for _ in range(2):
        await new_request(sessions, repo=str(repo_dir("todo-app")), requirement="x")
        await run_request_tick(sessions, run_deps, worker_id="w1")
    assert len(run_deps.register.writer.saved) == 1  # pyright: ignore[reportAttributeAccessIssue]
    assert await count_jobs(sessions, kind="requirements") == 2


async def test_registration_error_is_recorded(sessions, run_deps, tmp_path):
    rid = await new_request(sessions, repo=str(tmp_path / "elsewhere"), requirement="x")
    await run_request_tick(sessions, run_deps, worker_id="w1")
    row = await get_request(sessions, rid)
    assert row.status == "failed" and "not inside FACTORY_HOST_REPOS_ROOT" in row.detail
    assert row.job_id is None


async def test_waiting_request_starts_when_the_app_appears(sessions, harness_pr, repo_dir):
    rid = await new_request(sessions, repo=str(repo_dir("todo-app")), requirement="x")
    await run_request_tick(sessions, harness_pr.deps, worker_id="w1")
    row = await get_request(sessions, rid)
    assert (row.status, row.detail) == ("waiting", PR_URL)
    # not touched for 10 s yet: nothing to do
    assert await run_request_tick(sessions, harness_pr.deps, worker_id="w1") is False
    harness_pr.clock.advance(11)
    assert await run_request_tick(sessions, harness_pr.deps, worker_id="w1") is False  # PR open
    await harness_pr.merge_definition_pr()
    harness_pr.clock.advance(11)
    assert await run_request_tick(sessions, harness_pr.deps, worker_id="w1") is True
    assert (await get_request(sessions, rid)).status == "started"
    assert len(harness_pr.register.writer.saved) == 1  # type: ignore[attr-defined]


async def test_two_workers_never_start_one_request_twice(sessions, run_deps, repo_dir):
    await new_request(sessions, repo=str(repo_dir("todo-app")), requirement="x")
    results = await asyncio.gather(
        run_request_tick(sessions, run_deps, worker_id="w1"),
        run_request_tick(sessions, run_deps, worker_id="w2"),
    )
    assert sorted(results) == [False, True]
    assert await count_jobs(sessions, kind="requirements") == 1


async def test_create_run_request_validates(sessions):
    async with sessions() as s, s.begin():
        for kw in (
            {"requirement": " "},
            {"requirement": "x" * 200_001},
            {"requirement": "x", "repo": "work/app"},
            {"requirement": "x", "profile": "Bad Name"},
            {"requirement": "x", "budget_usd": __import__("decimal").Decimal("26")},
        ):
            args = {"repo": "acme/web", "profile": None, "ci_safe": False, "budget_usd": None,
                    "requested_by": "me@x.io", **kw}  # fmt: skip
            with pytest.raises(ValueError):
                await create_run_request(s, **args)
