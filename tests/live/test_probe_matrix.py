"""The probe matrix per provider and model (section 23). Real gateway at FACTORY_GATEWAY_URL."""

import pytest

from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.definition.store import DefinitionStore
from swfactory.gateway.client import GatewayClient
from swfactory.models.catalog import Catalog
from swfactory.settings import get_settings

pytestmark = pytest.mark.live

OK_CLASSES = {"not_enabled", "quota", "auth", "other", "unpriced"}


async def test_probe_matrix():
    settings = get_settings()
    engine = make_engine(settings.sqlalchemy_url())
    gateway = GatewayClient(
        settings.gateway_url,  # pyright: ignore[reportAttributeAccessIssue]
        settings.gateway_master_key.get_secret_value(),  # pyright: ignore[reportAttributeAccessIssue]
    )
    try:
        sessions = make_sessionmaker(engine)
        store = DefinitionStore(sessions)
        _, defn = await store.active()
        infos = await Catalog(sessions, gateway, store).refresh()
        for i in sorted(infos, key=lambda x: (x.provider, x.alias)):
            print(
                f"{i.provider:12} {i.alias:28} available={i.available} tools={i.supports_tools} "
                f"msgs={i.messages_tools_ok} {i.latency_ms}ms {i.error_class or ''}"
            )
        assert {i.alias for i in infos} == set(defn.factory.models)
        assert all(i.available or i.error_class in OK_CLASSES for i in infos)
    finally:
        await gateway.aclose()
        await engine.dispose()
