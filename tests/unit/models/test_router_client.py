import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from swfactory.definition.models import Defaults
from swfactory.models.router_client import RouterClient, make_router_client
from swfactory.models.triage import TRIAGE_QUESTIONS
from swfactory.settings import Settings

FIX = Path(__file__).parents[2] / "fixtures" / "laya"
STATE = {"title": "Fix 403 on origin null", "description": "CORS check rejects null origin"}
RESPONSE = json.loads((FIX / "batch.response.json").read_text())


class Rec:
    def __init__(self, handler):
        self.handler, self.reqs = handler, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.reqs.append(request)
        return self.handler(request)


def replay(drop: str | None = None) -> Rec:
    def h(_r):
        data = json.loads(json.dumps(RESPONSE))
        if drop:
            data["results"][0]["answers"].pop(drop)
        return httpx.Response(200, json=data, headers={"x-factory-router-version": "laya 0.4.31"})

    return Rec(h)


def status(code: int) -> Rec:
    return Rec(lambda _r: httpx.Response(code))


def body(b: bytes) -> Rec:
    return Rec(lambda _r: httpx.Response(200, content=b))


def refused() -> Rec:
    def h(r):
        raise httpx.ConnectError("refused", request=r)

    return Rec(h)


def slow() -> Rec:
    def h(r):
        raise httpx.ReadTimeout("slow", request=r)

    return Rec(h)


def client(rec: Rec) -> RouterClient:
    return RouterClient("http://router:8000", "k", transport=httpx.MockTransport(rec))


async def test_decide_parses_fixture():
    rec = replay()
    a = await client(rec).decide(STATE, TRIAGE_QUESTIONS)
    assert a["touches_ui"].choice in {"A", "B"} and a["touches_ui"].route in {"auto", "review"}
    assert a["kind"].error is None and a["kind"].router_version == "laya 0.4.31"
    assert a["kind"].head_version is None and a["kind"].probabilities
    req = rec.reqs[0]
    assert json.loads(req.content)["states"] == [STATE]
    assert req.headers["authorization"] == "Bearer k" and req.url.path == "/v1/systemone/batch"


async def test_head_version_filled():
    data = json.loads(json.dumps(RESPONSE))
    data["results"][0]["answers"]["kind"]["head_version"] = "kind@v3"
    rec = Rec(lambda _r: httpx.Response(200, json=data))
    a = await client(rec).decide(STATE, TRIAGE_QUESTIONS)
    assert a["kind"].head_version == "kind@v3"


@pytest.mark.parametrize(
    "rec,err",
    [
        (slow(), "timeout"),
        (refused(), "unreachable"),
        (status(401), "unauthorized"),
        (status(503), "http 503"),
        (body(b"<html>"), "bad response"),
    ],
)
async def test_errors_become_answers(rec, err):
    a = await client(rec).decide(STATE, TRIAGE_QUESTIONS)
    assert {q: x.error for q, x in a.items()} == {q: err for q in TRIAGE_QUESTIONS}
    assert all(x.choice is None for x in a.values())


async def test_missing_question():
    a = await client(replay(drop="risk")).decide(STATE, TRIAGE_QUESTIONS)
    assert a["risk"].error == "missing answer" and a["kind"].error is None


async def test_pins_sent_only_when_given():
    r1 = replay()
    await client(r1).decide(STATE, TRIAGE_QUESTIONS)
    assert "pins" not in json.loads(r1.reqs[0].content)
    r2 = replay()
    await client(r2).decide(STATE, TRIAGE_QUESTIONS, pins={"kind": 3})
    assert json.loads(r2.reqs[0].content)["pins"] == {"kind": 3}


def test_make_router_client(monkeypatch):
    defn = SimpleNamespace(factory=SimpleNamespace(defaults=Defaults(router_timeout_s=3)))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.delenv("FACTORY_LAYA_URL", raising=False)
    assert make_router_client(s.model_copy(update={"laya_url": None}), defn) is None  # pyright: ignore
    c = make_router_client(s.model_copy(update={"laya_url": "http://r:8000"}), defn)  # type: ignore[arg-type]
    assert c is not None and c._timeout_s == 3.0
    assert isinstance(c, RouterClient)
