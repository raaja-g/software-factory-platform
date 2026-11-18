from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text
from tests.support.pg import scalar
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.engine import DURABILITY, make_engine, open_checkpointer
from swfactory.settings import get_settings

LG_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")

pytestmark = pytest.mark.db


class State(TypedDict, total=False):
    answer: str


def two_node_graph(cp):
    def ask(state: State) -> State:
        return {"answer": interrupt("q")}

    def done(state: State) -> State:
        return {}

    g = StateGraph(State)
    g.add_node("ask", ask)
    g.add_node("done", done)
    g.add_edge(START, "ask")
    g.add_edge("ask", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=cp)


async def table_schemas(url, names):
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "select table_name, table_schema from information_schema.tables "
                    "where table_name = any(:n)"
                ),
                {"n": list(names)},
            )
            return {r[0]: r[1] for r in rows}
    finally:
        await engine.dispose()


def test_db_upgrade_creates_tables_in_lg(pg_url, monkeypatch):
    import asyncio

    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    codes = [CliRunner().invoke(app, ["db", "upgrade"]).exit_code for _ in range(2)]
    assert codes == [0, 0]
    got = asyncio.run(table_schemas(pg_url, LG_TABLES))
    assert got == {t: "lg" for t in LG_TABLES}


async def test_pg_url_fixture_has_lg_tables(pg_url):
    assert await scalar(pg_url, "select to_regclass('lg.checkpoints')::text") == "lg.checkpoints"


async def test_interrupt_survives_new_pool(pg_url):
    cfg: RunnableConfig = {"configurable": {"thread_id": "t1"}}
    async with open_checkpointer(pg_url) as cp:
        await two_node_graph(cp).ainvoke({}, cfg, durability=DURABILITY)
    async with open_checkpointer(pg_url) as cp:
        g = two_node_graph(cp)
        assert len((await g.aget_state(cfg)).interrupts) == 1
        assert (await g.ainvoke(Command(resume="ok"), cfg, durability=DURABILITY))["answer"] == "ok"
