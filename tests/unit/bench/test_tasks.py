from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import update
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq
from tests.support.pg import truncate_app_tables

from swfactory.bench.tasks import (
    BenchTaskError,
    acceptance_items,
    app_for_repo,
    is_test_path,
    task_from_job,
)
from swfactory.db.tables import Job, JobEvent
from swfactory.definition.models import AppDef

BASE, DEFN_SHA, MERGE_SHA = "a" * 40, "d" * 40, "b" * 40
MERGED_TEST = "def test_web():\n    assert True\n"
SPEC = "## Acceptance criteria\n- [ ] lists todos\n- [x] adds\n## Notes\n- [ ] no"
SPEC_ITEMS = ["lists todos", "adds"]
KEY = "benchmarks/v1/tasks/003-todo-web.yaml"


def test_is_test_path():
    paths = ["tests/test_web.py", "src/app/web.py", "web/app.test.ts", "pkg/x_test.go", "README.md"]
    assert [p for p in paths if is_test_path(p)] == [
        "tests/test_web.py",
        "web/app.test.ts",
        "pkg/x_test.go",
    ]


def test_acceptance_items():
    assert acceptance_items(SPEC) == SPEC_ITEMS


def test_app_for_repo():
    local = AppDef(repo_kind="local", repo_url="/x/todo")
    gh = AppDef(repo_kind="github", repo_url="https://github.com/o/n.git")
    defn = SimpleNamespace(factory=SimpleNamespace(apps={"todo": local, "g": gh}))
    assert app_for_repo(defn, "local:todo") is local  # pyright: ignore[reportArgumentType]
    assert app_for_repo(defn, "github:o/n") is gh  # pyright: ignore[reportArgumentType]


class Changes:
    def __init__(self, rows: dict[str, tuple[str, str]]):
        self.rows = rows

    async def changed(self, app, merge_sha):
        assert merge_sha == MERGE_SHA
        return [(st, p) for p, (st, _) in self.rows.items()]

    async def read(self, app, sha, path):
        return self.rows[path][1]


class Artifacts:
    async def get_text(self, job_id, path):
        return SPEC if path == "spec.md" else None


GOOD = {"src/app/web.py": ("M", "x = 1\n"), "tests/test_web.py": ("A", MERGED_TEST)}


async def seed(db, *, stage="merged", merge_event=True, sha=True):
    await seed_demo(db)
    job = await enq(db, app="demo", feature="Add a web page\nwith todos")
    async with db.begin() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(
                stage=stage, base_sha=BASE, definition_sha=DEFN_SHA, slug="todo-web", seq=3,
                profile_name="standard",
            )
        )  # fmt: skip
        if merge_event:
            s.add(
                JobEvent(
                    job_id=job.id,
                    kind="delivery.merged",
                    data={"merge_sha": MERGE_SHA} if sha else {},
                )
            )
    return job


def defn():
    app = AppDef(repo_kind="local", repo_url="/x/demo")
    return SimpleNamespace(
        factory=SimpleNamespace(apps={"demo": app}),
        scorers={"merge-readiness": object()},
        benchmarks={},
    )


async def add(db, rows=GOOD, **kw):
    async with db() as s:
        return await task_from_job(
            s,
            "demo:003",
            suite="v1",
            name=kw.pop("name", None),
            scorers=["merge-readiness"],
            changes=Changes(rows),
            artifacts=Artifacts(),  # pyright: ignore[reportArgumentType]
            defn=defn(),  # pyright: ignore[reportArgumentType]
            **kw,
        )


pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def db(sessions):
    yield sessions
    async with sessions.kw["bind"].connect() as conn:
        await truncate_app_tables(conn)


async def test_task_from_merged_job(db):
    await seed(db)
    d = await add(db)
    t = yaml.safe_load(d.files[KEY])
    assert (t["base_sha"], t["definition_sha"], t["hidden_checks"], t["source_job"]) == (
        BASE,
        DEFN_SHA,
        ["tests/test_web.py"],
        "demo:003",
    )
    assert t["success_criteria"] == SPEC_ITEMS
    assert d.files["benchmarks/v1/checks/003-todo-web/tests/test_web.py"] == MERGED_TEST
    assert (t["merge_sha"], t["check_command"], t["repo"]) == (
        MERGE_SHA,
        "python3 -m pytest -q {file}",
        "local:demo",
    )
    suite = yaml.safe_load(d.files["benchmarks/v1/suite.yaml"])
    assert suite == {
        "configurations": [{"name": "current", "profile": "standard"}],
        "budget_usd": 40,
    }
    assert (d.suite, d.name) == ("v1", "003-todo-web")


async def test_unmerged_job_refused(db):
    await seed(db, stage="delivered")
    with pytest.raises(BenchTaskError, match="only merged jobs"):
        await add(db)


async def test_merge_sha_not_recorded_refused(db):
    await seed(db, sha=False)
    with pytest.raises(BenchTaskError, match="merge sha not recorded"):
        await add(db)


async def test_no_test_files_refused(db):
    await seed(db)
    with pytest.raises(BenchTaskError, match="added or changed no test files"):
        await add(db, {"src/app/web.py": ("M", "x\n")})


async def test_non_python_checks_need_a_command(db):
    await seed(db)
    rows = {"web/app.test.ts": ("A", "test()\n")}
    with pytest.raises(BenchTaskError, match=r"set check_command: no default for \.ts checks"):
        await add(db, rows)
    d = await add(db, rows, check_command="npx vitest run {file}")
    assert yaml.safe_load(d.files[KEY])["check_command"] == "npx vitest run {file}"


async def test_deleted_test_files_are_not_checks(db):
    await seed(db)
    with pytest.raises(BenchTaskError, match="no test files"):
        await add(db, {"tests/test_old.py": ("D", "x")})
