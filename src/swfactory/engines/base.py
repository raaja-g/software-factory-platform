"""The Engine protocol (spine). fallback_detected and register_engines arrive in M6."""

from typing import Any, Protocol

from swfactory.domain.types import AgentResult, AgentTask, EngineSupport

# Sandbox (M2.2b) and ModelInfo (M5) do not exist yet; they are typed loosely until they land.
Sandbox = Any
ModelInfo = Any


class Engine(Protocol):
    name: str

    def support_for(self, model: ModelInfo) -> EngineSupport: ...

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        """box is None only for "direct" without attachments.

        Adapters never raise for CLI or gateway failures: they return FAILED / TIMEOUT / BUDGET
        with the text in .error and its class in .meta["error_class"].
        """
        ...
