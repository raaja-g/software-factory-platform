import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text
from tests.unit.api.api_support import settings

import swfactory.api.routes.hooks as hooks
from swfactory.api.app import ApiDeps, create_app
from swfactory.automations.webhooks import sign_headers
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef, AutomationDef, WebhookDef
from swfactory.definition.store import DefinitionStore, NoActiveDefinition

pytestmark = pytest.mark.db

SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def hook(**kw) -> WebhookDef:
    base = {
        "auth_mode": "signature",
        "signature_scheme": "standard_webhooks",
        "secret_env": "WEBHOOK_DEMO_SECRET",
    }
    return WebhookDef.model_validate({**base, **kw})


def auto(**kw) -> AutomationDef:
    base = {
        "trigger": {
            "provider": "webhook",
            "event": "received",
            "filter": {"webhook_ids": ["demo"]},
        },
        "app": "demo-gh",
        "feature_template": "Fix this:\n{{ title }}",
        "max_open_jobs": 5,
    }
    return AutomationDef.model_validate({**base, **kw})


async def activate(sessions, *, max_open_jobs=5):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    gh = AppDef.model_validate(
        {
            "repo_kind": "github",
            "repo_url": "https://github.com/acme/web",
            "runner": "none",
            "github": {"ci_safe": True},
        }
    )
    fac = r.definition.factory.model_copy(update={"apps": {"demo-gh": gh}})
    defn = r.definition.model_copy(
        update={
            "factory": fac,
            "webhooks": {"demo": hook(), "disabled-hook": hook(enabled=False)},
            "automations": {"demo-auto": auto(max_open_jobs=max_open_jobs)},
        }
    )
    assert await DefinitionStore(sessions).save(
        replace(r, sha=f"sha-{time.time()}", definition=defn)
    )


async def client(sessions):
    cfg = settings()
    deps = ApiDeps(
        sessions=sessions,
        events=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        artifacts=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=DefinitionStore(sessions),
        users=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=cfg,
    )
    app = create_app(cfg, deps=deps)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def post_signed(c, name, *, msg, sig=None, ts=None):
    body = json.dumps({"title": "boom", "msg": msg}).encode()
    h = sign_headers(hook(), SECRET, body, now=ts or int(time.time()), msg_id=msg)
    if sig is not None:
        h["webhook-signature"] = sig
    return await c.post(f"/hooks/{name}", content=body, headers=h)


async def jobs(sessions):
    async with sessions() as s:
        r = await s.execute(text("select source, requested_by, feature from app.jobs"))
        return r.all()


@pytest.fixture
def env_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEMO_SECRET", SECRET)


async def test_route_sequence(sessions, env_secret):
    await activate(sessions)
    now = int(time.time())
    async with await client(sessions) as c:
        assert (await post_signed(c, "demo", msg="msg_1")).status_code == 202
        r = await post_signed(c, "demo", msg="msg_1")
        assert r.status_code == 200 and r.json() == {"duplicate": True}
        assert (await post_signed(c, "demo", msg="msg_2", sig="v1,AAAA")).status_code == 401
        assert (await post_signed(c, "demo", msg="msg_3", ts=now - 301)).status_code == 401
        assert (await post_signed(c, "disabled-hook", msg="msg_4")).status_code == 404
        assert (await post_signed(c, "nope", msg="msg_5")).status_code == 404
    rows = await jobs(sessions)
    assert len(rows) == 1 and rows[0].source == "webhook"
    assert "boom" in rows[0].feature and "UNTRUSTED DATA" in rows[0].feature


async def test_missing_secret_is_500(sessions, monkeypatch):
    monkeypatch.delenv("WEBHOOK_DEMO_SECRET", raising=False)
    await activate(sessions)
    async with await client(sessions) as c:
        assert (await post_signed(c, "demo", msg="msg_1")).status_code == 500


async def test_route_respects_the_open_job_cap(sessions, env_secret):
    await activate(sessions, max_open_jobs=1)
    async with await client(sessions) as c:
        assert (await post_signed(c, "demo", msg="msg_1")).status_code == 202
        r = await post_signed(c, "demo", msg="msg_2")
        assert r.status_code == 202 and r.json()["skipped"] == {"demo-auto": "max_open_jobs"}
    rows = await jobs(sessions)
    assert len(rows) == 1 and rows[0].requested_by == "webhook:demo/demo-auto"


async def test_failed_enqueue_is_not_recorded_as_delivered(sessions, env_secret, monkeypatch):
    await activate(sessions)
    async with await client(sessions) as c:
        with monkeypatch.context() as m:
            m.setattr(hooks, "enqueue_job", AsyncMock(side_effect=NoActiveDefinition()))
            assert (await post_signed(c, "demo", msg="msg_9")).status_code == 503
        assert (await post_signed(c, "demo", msg="msg_9")).status_code == 202
    assert len(await jobs(sessions)) == 1
