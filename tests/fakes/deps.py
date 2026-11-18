"""Test helpers: a seeded definition and ready-made Deps."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from tests.fakes.artifacts import InMemoryArtifactStore

from swfactory.artifacts import ArtifactStore
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef, Concurrency, Defaults
from swfactory.definition.store import DefinitionStore
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import register_engine
from swfactory.events import EventBus
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.deps import Deps, unavailable
from swfactory.sandbox.base import ExecResult
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.settings import get_settings

DEFAULT_TIMEOUTS: dict[str, int] = dict(Defaults().step_timeouts_min)


async def seed_demo(
    sessions: Any,
    *,
    app: Mapping[str, Any] = {},  # noqa: B006
    defaults: Mapping[str, Any] = {},  # noqa: B006
    apps: Sequence[str] = ("demo",),
    concurrency: Mapping[str, int] | None = None,
) -> str:
    """Save and activate a definition (unique sha) with the given apps; return its sha."""
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    app_defs = {
        slug: AppDef.model_validate({**base.model_dump(mode="json"), **app}) for slug in apps
    }
    new_defaults = Defaults.model_validate(
        {**r.definition.factory.defaults.model_dump(), **defaults}
    )
    fac = r.definition.factory.model_copy(update={"apps": app_defs, "defaults": new_defaults})
    if concurrency is not None:
        fac = fac.model_copy(
            update={
                "concurrency": Concurrency.model_validate(
                    {**fac.concurrency.model_dump(by_alias=True), **concurrency}
                )
            }
        )
    sha = f"sha-{uuid.uuid4().hex}"
    r = replace(r, sha=sha, definition=r.definition.model_copy(update={"factory": fac}))
    assert await DefinitionStore(sessions).save(r)
    return sha


def make_test_deps(
    sessions: Any,
    script: FakeScript,
    *,
    exec_results: Sequence[ExecResult] = (),
    git: FakeGitService | None = None,
) -> Deps:
    register_engine(FakeEngine(script))
    return Deps(
        sessions=sessions,
        events=EventBus(sessions, ""),
        artifacts=ArtifactStore(sessions),
        sandboxes=FakeSandboxProvider(exec_results),  # pyright: ignore[reportArgumentType]
        selector=unavailable("selector", "M5"),
        catalog=unavailable("catalog", "M5"),
        gateway=unavailable("gateway", "M5"),
        budgets=unavailable("budgets", "M5"),
        git=git or FakeGitService(),
        roles=unavailable("roles", "M3"),
        triage=unavailable("triage", "M3"),
        definitions=DefinitionStore(sessions),
        scoring=unavailable("scoring", "M10"),
        settings=get_settings(),
    )


def make_unit_deps(script: FakeScript | None = None) -> Deps:
    register_engine(FakeEngine(script or FakeScript()))
    return Deps(
        sessions=unavailable("sessions", "the db tests"),
        events=EventBus(None, ""),
        artifacts=InMemoryArtifactStore(),  # type: ignore[arg-type]
        sandboxes=FakeSandboxProvider(),  # pyright: ignore[reportArgumentType]
        selector=unavailable("selector", "M5"),
        catalog=unavailable("catalog", "M5"),
        gateway=unavailable("gateway", "M5"),
        budgets=unavailable("budgets", "M5"),
        git=FakeGitService(),
        roles=unavailable("roles", "M3"),
        triage=unavailable("triage", "M3"),
        definitions=unavailable("definitions", "the db tests"),
        scoring=unavailable("scoring", "M10"),
        settings=get_settings(),
    )
