"""The spine's dependency bundle handed to every graph node."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.artifacts import ArtifactStore
    from swfactory.definition.store import DefinitionStore
    from swfactory.events import EventBus
    from swfactory.gitops.service import GitService
    from swfactory.sandbox.base import SandboxProvider
    from swfactory.settings import Settings


@dataclass(frozen=True)
class Deps:
    sessions: async_sessionmaker[AsyncSession]
    events: EventBus
    artifacts: ArtifactStore
    sandboxes: SandboxProvider
    selector: Any  # ModelSelector (M5)
    catalog: Any  # Catalog (M5)
    gateway: Any  # GatewayClient (M5)
    budgets: Any  # BudgetService (M5)
    git: GitService
    roles: Any  # RoleLibrary (M3)
    triage: Any  # TriageService (M3)
    definitions: DefinitionStore
    scoring: Any  # ScoringService (M10)
    settings: Settings


class _Unavailable:
    def __init__(self, name: str, milestone: str) -> None:
        object.__setattr__(self, "_msg", f"{name} arrives in {milestone}")

    def __getattr__(self, attr: str) -> Any:
        raise RuntimeError(self._msg)


def unavailable(name: str, milestone: str) -> Any:
    """A placeholder that raises ``RuntimeError("<name> arrives in <milestone>")`` on any use."""
    return _Unavailable(name, milestone)
