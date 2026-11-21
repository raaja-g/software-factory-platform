"""The improvements routes against a real database (no fakes behind the real handlers)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import httpx
import pytest
from tests.support.pg import truncate_app_tables
from tests.unit.api.api_support import FakeKeys, fake_deps, server

from swfactory.api.app import create_app
from swfactory.api.auth import hash_token
from swfactory.db.tables import Improvement

pytestmark = pytest.mark.db


class Users:
    async def role_of(self, email):
        return "viewer"

    async def token_email(self, token_hash):
        return "v@x.io" if token_hash == hash_token("sfp_v") else None


@pytest.fixture
async def world(sessions):
    async with sessions() as s:
        await truncate_app_tables(await s.connection())
    async with sessions() as s, s.begin():
        s.add(
            Improvement(
                trigger="manual", role_group="builder", status="rejected", base_sha="a" * 40,
                suite="v1-parity", cost_usd=Decimal("48.6"),
                evidence={
                    "signals": [{"job_id": "x", "job_ref": "todo:1", "role": "builder",
                                 "kind": "failed", "detail": "d", "at": "t"}],
                    "violations": [{"rule": "path", "path": "runners/x.yaml", "detail": "no"}],
                },
            )
        )  # fmt: skip
    yield sessions
    async with sessions() as s:
        await truncate_app_tables(await s.connection())


async def test_list_and_detail_through_the_real_handlers(world):
    s = server()
    deps = dataclasses.replace(
        fake_deps(s),
        sessions=world,
        users=Users(),  # pyright: ignore[reportArgumentType]
    )
    app = create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        headers={"authorization": "Bearer sfp_v"},
    ) as c:
        rows = (await c.get("/api/improvements")).json()
        assert [(r["role_group"], r["cost_usd"], r["result"]) for r in rows] == [
            ("builder", "48.600000", "unmeasured")
        ]
        d = (await c.get(f"/api/improvements/{rows[0]['id']}")).json()
    assert d["evidence"]["signals"][0]["job_ref"] == "todo:1"
    assert d["evidence"]["violations"][0]["rule"] == "path"
    assert d["evidence"]["costs"]["total_usd"] == "48.600000"
