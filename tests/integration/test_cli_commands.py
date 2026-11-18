import pytest
from sqlalchemy import select, update
from tests.support.db_helpers import activate, enq
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.tables import Command, Job
from swfactory.settings import get_settings

pytestmark = pytest.mark.db


@pytest.fixture
async def cli(sessions, pg_url, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    await activate(sessions, apps={"demo": {}})
    for stage in ("build", "merged", "needs_human"):
        j = await enq(sessions, app="demo")
        async with sessions.begin() as s:
            await s.execute(update(Job).where(Job.id == j.id).values(stage=stage))
    import asyncio

    loop = asyncio.get_running_loop()

    async def call(*args):
        return await loop.run_in_executor(None, lambda: CliRunner().invoke(app, list(args)))

    call.sessions = sessions  # type: ignore[attr-defined]
    return call


async def commands_rows(sessions):
    async with sessions() as s:
        return list((await s.scalars(select(Command).order_by(Command.created_at))).all())


def outcome(r):
    return r.exit_code, r.output.strip().splitlines()[-1]


async def test_cli_writes_command(cli):
    # demo:003 waits in needs_human
    r = await cli("jobs", "rework", "demo:003", "--note", "Add a test", "--by", "ann")
    assert r.exit_code == 0, r.output
    rows = await commands_rows(cli.sessions)
    assert [(c.action, c.note, c.requested_by, c.consumed_at) for c in rows] == [
        ("rework", "Add a test", "ann", None)
    ]


async def test_cli_refuses_when_not_waiting(cli):
    assert outcome(await cli("jobs", "approve", "demo:001")) == (
        1,
        "job is not waiting for a person",
    )
    assert outcome(await cli("jobs", "cancel", "demo:002")) == (1, "job has already finished")
    assert outcome(await cli("jobs", "rework", "demo:003")) == (1, "rework needs a note")


async def test_cli_retry_is_written_even_if_not_offered(cli):
    assert (await cli("jobs", "retry", "demo:3")).exit_code == 0
    assert [c.action for c in await commands_rows(cli.sessions)] == ["retry"]
