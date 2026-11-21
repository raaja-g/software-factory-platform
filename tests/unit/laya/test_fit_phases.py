"""A fit must not hold a database transaction open while the router trains, and must not try to
train on every imported row on a CPU.

Found with the first real data: `factory laya fit --question kind` died with
IdleInTransactionSessionTimeout because the transaction that read the rows sat idle through the
router call, and the compose Postgres ends idle transactions after 300 s. Training on the 9,700
imported size rows kept the router at 700% CPU for many minutes, and the write at the end
would have failed anyway."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, text
from tests.unit.laya.test_export import db  # noqa: F401  (fixture)
from tests.unit.laya.test_fit import FAKE_FIT, SENT, URL, seed_touches_ui  # noqa: F401

from swfactory.db.tables import LayaHead
from swfactory.laya.export import ExportRow
from swfactory.laya.fit import MAX_FIT_ROWS, cap_rows, fit_with_sessions


def rows(own: int, ext: dict[str, int]) -> list[ExportRow]:
    out = [ExportRow(f'{{"i": {i}}}', "AB"[i % 2], f"job:demo:{i}") for i in range(own)]
    for label, n in ext.items():
        out += [ExportRow(f'{{"x": "{label}{i}"}}', label, "external:pub") for i in range(n)]
    return out


def test_cap_keeps_every_row_when_under_the_limit():
    r = rows(5, {"A": 10, "B": 10})
    assert cap_rows(r, 100) == r


def test_cap_keeps_all_of_your_own_rows_and_samples_the_imported_ones():
    r = rows(40, {"A": 500, "B": 500})
    capped = cap_rows(r, 200)
    own = [x for x in capped if not x.source.startswith("external:")]
    assert len(capped) == 200 and len(own) == 40  # all 40 own rows survive
    ext = [x for x in capped if x.source.startswith("external:")]
    assert {x.label for x in ext} == {"A", "B"} and abs(
        sum(x.label == "A" for x in ext) - sum(x.label == "B" for x in ext)
    ) <= 1  # imported rows are balanced across the labels


def test_cap_is_deterministic():
    r = rows(3, {"A": 400, "B": 400, "C": 400})
    assert cap_rows(r, 90) == cap_rows(r, 90)


def test_cap_never_drops_a_small_class_that_has_few_rows():
    r = rows(0, {"A": 1000, "B": 12})
    capped = cap_rows(r, 100)
    assert sum(x.label == "B" for x in capped) == 12 and len(capped) == 100


def test_cap_with_more_own_rows_than_the_limit_keeps_only_own_rows():
    r = rows(50, {"A": 100})
    capped = cap_rows(r, 30)
    assert len(capped) == 50 and all(not x.source.startswith("external:") for x in capped)


def test_default_cap_is_a_cpu_sized_number():
    assert 500 <= MAX_FIT_ROWS <= 5000


@pytest.mark.db
async def test_no_transaction_is_open_during_the_router_call(db):  # noqa: F811
    async with db() as s, s.begin():
        await seed_touches_ui(s, 60)
    seen: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        async with db.kw["bind"].connect() as conn:
            n = await conn.scalar(
                text(
                    "select count(*) from pg_stat_activity where datname = current_database()"
                    " and state like 'idle in transaction%' and pid <> pg_backend_pid()"
                )
            )
        seen.append(int(n or 0))
        return await FAKE_FIT.handle_async_request(request)

    head = await fit_with_sessions(
        db, URL, "k", "touches_ui", transport=httpx.MockTransport(handler)
    )
    assert seen == [0], "a transaction was held open while the router trained"
    assert (head.question, head.version, head.rows, head.active) == ("touches_ui", 1, 60, False)
    async with db() as s:
        stored = (await s.scalars(select(LayaHead))).all()
    assert len(stored) == 1


@pytest.mark.db
async def test_the_assertion_detects_the_old_single_session_behaviour(db):  # noqa: F811
    """fit_question on one caller-owned session (as the CLI used to run it) keeps its read
    transaction open through the router call: this is the state Postgres killed after 300 s."""
    from swfactory.laya.fit import fit_question

    async with db() as s, s.begin():
        await seed_touches_ui(s, 60)
    seen: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        async with db.kw["bind"].connect() as conn:
            n = await conn.scalar(
                text(
                    "select count(*) from pg_stat_activity where datname = current_database()"
                    " and state like 'idle in transaction%' and pid <> pg_backend_pid()"
                )
            )
        seen.append(int(n or 0))
        return await FAKE_FIT.handle_async_request(request)

    async with db() as s, s.begin():
        await fit_question(s, URL, "k", "touches_ui", transport=httpx.MockTransport(handler))
    assert seen == [1]
