"""Shared fakes for the M7.4b route tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from tests.unit.api.api_support import FakeKeys, fake_deps, jwt, server, settings

from swfactory.api.app import create_app
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.writer import DefinitionWriteError, SaveResult
from swfactory.models.catalog import ModelInfo

DIGEST = "sha256:" + "a" * 64


def info(alias, *, available=True, upstream="vertex_ai/gemini-2.5-pro", tools=True, msgs=True):
    return ModelInfo(
        alias=alias,
        provider="vertex",
        upstream_id=upstream,
        strength="mid",
        price_in_per_mtok=Decimal("1.5"),
        price_out_per_mtok=Decimal("6"),
        context_window=100000,
        supports_tools=tools,
        messages_tools_ok=msgs,
        available=available,
        error_class=None if available else "other",
        latency_ms=10,
        last_error=None,
    )


CATALOG = [
    info("gemini-pro@vertex"),
    info("gemini-flash@vertex"),
    info("gemini-lite@vertex"),
    info("claude-sonnet@vertex", upstream="vertex_ai/claude-sonnet-4"),
    info("claude-haiku@vertex", upstream="vertex_ai/claude-haiku-4"),
]


def default_definition():
    result = load_definition(str(defaults_dir()))
    assert result.definition is not None and not result.errors
    return result.definition


class FakeCatalog:
    def __init__(self, models=None):
        self.models = list(CATALOG if models is None else models)
        self.refresh_calls: list[Any] = []

    async def list(self, *, available_only=False):
        return [m for m in self.models if m.available or not available_only]


class FakeDefs:
    def __init__(self, defn):
        self.defn = defn

    async def active(self):
        return "sha1", self.defn


@dataclass
class FakeWriter:
    paths: list[str] = field(default_factory=list)
    fail: bool = False
    mode: str = "commit"

    async def save_file(self, path, content, *, message, author):
        if self.fail:
            raise DefinitionWriteError("dirty")
        self.paths.append(path)
        sha = "c0ffee" if self.mode == "commit" else None
        return SaveResult(self.mode, sha, None if sha else "https://gh/pr/1")  # pyright: ignore[reportArgumentType]


class _Sess:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self


def client_for(
    *,
    email="admin@x.io",
    role="admin",
    mode="personal",
    writer=None,
    defn=None,
    catalog=None,
    s=None,
    **deps_kw,
) -> TestClient:
    s = s or (settings() if mode == "personal" else server())
    deps = fake_deps(s)
    deps = type(deps)(
        **{
            **deps.__dict__,
            "catalog": catalog or FakeCatalog(),
            "definitions": FakeDefs(defn or default_definition()),
            "writer": writer,
            "sessions": lambda: _Sess(),
            **deps_kw,
        }
    )
    deps.users.roles[email] = role  # pyright: ignore[reportAttributeAccessIssue]
    app = create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    c = TestClient(app, base_url="http://127.0.0.1:8080")
    if mode == "personal":
        c.headers["authorization"] = "Bearer sfp_personal_test_token"
    else:
        c.headers["x-goog-iap-jwt-assertion"] = jwt(email=email)
    return c


NOW = datetime(2026, 1, 1, tzinfo=UTC)
NS = SimpleNamespace
