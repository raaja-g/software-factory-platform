import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from tests.fakes.gateway import FakeGateway

from swfactory.definition.loader import LoadResult
from swfactory.definition.models import (
    Definition,
    FactoryConfig,
    ModelAliasDef,
    Profile,
)
from swfactory.definition.store import DefinitionStore
from swfactory.gateway.client import GatewayRateLimited, ProbeResult
from swfactory.models.catalog import (
    Catalog,
    aliases_in_use,
    messages_aliases,
    refresh_due,
    to_model_info,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
OK_PROBE = ProbeResult(True, True, None, 10, None, None)
PRICE = (Decimal("1"), Decimal("2"), 100_000)


def mdef(**kw) -> ModelAliasDef:
    base = {"provider": "vertex", "upstream_id": "x", "strength": "mid"}
    return ModelAliasDef.model_validate({**base, **kw})


def test_refresh_due_nightly_full():
    assert refresh_due({"a": NOW - timedelta(hours=25), "b": NOW}, {"b"}, NOW) == ["a", "b"]


def test_refresh_due_unprobed_is_full():
    assert refresh_due({"a": None, "b": NOW}, set(), NOW) == ["a", "b"]


def test_refresh_due_hourly_in_use():
    assert refresh_due(
        {"a": NOW - timedelta(hours=2), "b": NOW - timedelta(minutes=61)}, {"b"}, NOW
    ) == ["b"]
    assert refresh_due({"a": NOW, "b": NOW}, {"b"}, NOW) is None


def test_refresh_due_quota_rows_after_5_min():
    assert refresh_due({"a": NOW - timedelta(minutes=6), "b": NOW}, set(), NOW, quota={"a"}) == [
        "a"
    ]
    assert refresh_due({"a": NOW - timedelta(minutes=4), "b": NOW}, set(), NOW, quota={"a"}) is None


def test_definition_price_overrides_gateway_map():
    i = to_model_info(
        "flash@gemini",
        mdef(price_in_per_mtok=Decimal("0.30")),
        OK_PROBE,
        (Decimal("0.50"), Decimal("2"), 1_048_576),
    )
    assert (i.price_in_per_mtok, i.price_out_per_mtok, i.context_window) == (
        Decimal("0.30"),
        Decimal("2"),
        1_048_576,
    )


def test_quota_probe_marks_unavailable():
    i = to_model_info(
        "pro@vertex", mdef(), ProbeResult(False, None, None, None, "quota", "429"), PRICE
    )
    assert (i.available, i.error_class) == (False, "quota")


def test_paid_alias_without_price_is_unpriced():
    i = to_model_info("byo@openai", mdef(), OK_PROBE, None)
    assert (i.available, i.error_class, i.last_error) == (
        False,
        "unpriced",
        "no price: set price_in_per_mtok and price_out_per_mtok",
    )
    assert (i.price_in_per_mtok, i.price_out_per_mtok) == (0, 0)


def test_definition_price_makes_alias_priced():
    m = mdef(price_in_per_mtok=Decimal("1"), price_out_per_mtok=Decimal("2"))
    assert to_model_info("byo@openai", m, OK_PROBE, None).available is True


def test_local_alias_needs_no_price():
    assert to_model_info("qwen@ollama", mdef(), OK_PROBE, None, local=True).available is True


def test_refresh_needs_a_gateway():
    with pytest.raises(RuntimeError, match="runs in the worker"):
        asyncio.run(Catalog(None, None, None).refresh())  # pyright: ignore[reportArgumentType]


def _defn() -> Definition:
    f = yaml.safe_load(Path("defaults/factory.yaml").read_text())
    f["models"] = {
        "flash@vertex": {"provider": "vertex", "upstream_id": "gf", "strength": "mid"},
        "pro@vertex": {"provider": "vertex", "upstream_id": "gp", "strength": "strong"},
        "sonnet@vertex": {"provider": "vertex", "upstream_id": "cs", "strength": "strong"},
    }
    fc = FactoryConfig.model_validate(f)
    prof = Profile.model_validate(
        {
            "roles": {
                "builder": {
                    "engine": "claude_code",
                    "model": "sonnet@vertex",
                    "fallback": ["pro@vertex"],
                },
                "judge": {"engine": "opencode", "model": "flash@vertex"},
            }
        }
    )
    return Definition(factory=fc, profiles={"p": prof})


def test_aliases_in_use_and_messages_aliases():
    d = _defn()
    assert aliases_in_use(d) == {"sonnet@vertex", "pro@vertex", "flash@vertex"}
    assert messages_aliases(d) == {"sonnet@vertex", "pro@vertex"}


@pytest.fixture
async def catalog(sessions):
    d = _defn()
    store = DefinitionStore(sessions)
    assert await store.save(LoadResult(sha="sha-cat", source="test", definition=d, errors=()))
    fake = FakeGateway()
    fake.prices = {a: PRICE for a in d.factory.models}
    fake.probes["pro@vertex"] = ProbeResult(False, None, None, None, "quota", "429")
    return Catalog(sessions, fake, store), fake, d  # pyright: ignore[reportArgumentType]


@pytest.mark.db
async def test_refresh_stores_rows_and_uses_messages_probe(catalog):
    cat, fake, defn = catalog
    infos = await cat.refresh()
    assert {i.alias for i in infos} == set(defn.factory.models)
    assert fake.probe_calls["sonnet@vertex"]["messages_format"] is True
    assert fake.probe_calls["flash@vertex"]["messages_format"] is False
    assert (await cat.get("pro@vertex")).last_error == "429"  # pyright: ignore[reportOptionalMemberAccess]
    assert [i.alias for i in await cat.list(available_only=True)] == [
        "flash@vertex",
        "sonnet@vertex",
    ]
    assert (await cat.get("sonnet@vertex")).messages_tools_ok is True  # pyright: ignore[reportOptionalMemberAccess]
    assert (await cat.get("flash@vertex")).messages_tools_ok is None  # pyright: ignore[reportOptionalMemberAccess]


@pytest.mark.db
async def test_mark_unavailable(catalog):
    cat, _, _ = catalog
    await cat.refresh()
    await cat.mark_unavailable("flash@vertex", "not_enabled", "404 Publisher Model")
    i = await cat.get("flash@vertex")
    assert i is not None and (i.available, i.error_class) == (False, "not_enabled")


@pytest.mark.db
async def test_unreachable_gateway_changes_no_rows(catalog):
    cat, fake, _ = catalog
    await cat.refresh()
    before = await cat.list()
    fake.probe_error = httpx.ConnectError("refused")
    with pytest.raises(httpx.ConnectError):
        await cat.refresh()
    assert await cat.list() == before


@pytest.mark.db
async def test_gateway_limit_leaves_alias_row_unchanged(catalog):
    cat, fake, _ = catalog
    await cat.refresh()
    before = await cat.get("pro@vertex")
    fake.probes["pro@vertex"] = GatewayRateLimited("429")
    await cat.refresh()
    assert await cat.get("pro@vertex") == before


@pytest.mark.db
async def test_removed_alias_row_is_marked(catalog, sessions):
    from sqlalchemy import update

    from swfactory.db.repo import upsert_models
    from swfactory.models.catalog import ModelInfo

    cat, _, _ = catalog
    gone = ModelInfo(
        "old@vertex",
        "vertex",
        "o",
        "mid",
        Decimal(0),
        Decimal(0),
        None,
        None,
        None,
        True,
        None,
        None,
        None,
    )
    async with sessions() as s, s.begin():
        await upsert_models(s, [gone])
    await cat.refresh()
    i = await cat.get("old@vertex")
    assert i is not None and (i.available, i.last_error) == (False, "not in definition")
    _ = update


@pytest.mark.db
async def test_refresh_if_due_runs_once_then_waits(catalog):
    cat, _, _ = catalog
    assert await cat.refresh_if_due() is not None  # nothing probed yet: a full refresh
    assert await cat.refresh_if_due() is None
    later = datetime.now(UTC) + timedelta(minutes=6)
    got = await cat.refresh_if_due(later)  # pro@vertex has a quota mark and is in use
    assert got is not None and [i.alias for i in got] == ["pro@vertex"]
