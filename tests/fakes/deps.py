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
from swfactory.costs.budgets import BudgetService
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef, Concurrency, Defaults, TriageConfig
from swfactory.definition.store import DefinitionStore
from swfactory.engines.fake import FakeEngine, FakeRunGateway, FakeScript, FakeSelector
from swfactory.engines.registry import register_engine
from swfactory.events import EventBus
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.deps import Deps, unavailable
from swfactory.models.catalog import Catalog
from swfactory.models.triage import TriageService
from swfactory.roles.library import RoleLibrary
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
    triage_rules: Sequence[Mapping[str, Any]] = (),
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
    if triage_rules:
        tri = TriageConfig.model_validate({"rules": list(triage_rules)})
        fac = fac.model_copy(update={"triage": tri})
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
    selector: Any = None,
    gateway: Any = None,
) -> Deps:
    """Deps with a fake engine; ``selector`` and ``gateway`` default to the fake-engine ones."""
    register_engine(FakeEngine(script))
    artifacts = ArtifactStore(sessions, Path(tempfile.mkdtemp(prefix="artifacts-")))
    definitions = DefinitionStore(sessions)
    return Deps(
        sessions=sessions,
        events=EventBus(sessions, ""),
        artifacts=artifacts,
        sandboxes=FakeSandboxProvider(exec_results),  # pyright: ignore[reportArgumentType]
        selector=selector if selector is not None else FakeSelector(sessions),
        catalog=Catalog(sessions, None, definitions),
        gateway=gateway if gateway is not None else FakeRunGateway(),
        budgets=BudgetService(sessions, definitions),
        git=git or FakeGitService(),
        roles=RoleLibrary(),
        triage=TriageService(definitions, artifacts),
        definitions=definitions,
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
        roles=RoleLibrary(),
        triage=unavailable("triage", "M3"),
        definitions=unavailable("definitions", "the db tests"),
        scoring=unavailable("scoring", "M10"),
        settings=get_settings(),
    )
