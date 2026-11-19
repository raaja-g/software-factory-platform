"""Process-wide engine registry."""

import importlib
from typing import TYPE_CHECKING

from swfactory.engines.base import Engine

if TYPE_CHECKING:
    from swfactory.settings import Settings

ENGINE_NAMES = ("opencode", "claude_code", "gemini_cli", "direct", "fake")

_engines: dict[str, Engine] = {}

# engine name -> (module, class name, takes settings)
_BUILDERS = {
    "opencode": ("swfactory.engines.opencode", "OpenCodeEngine"),
    "claude_code": ("swfactory.engines.claude_code", "ClaudeCodeEngine"),
    "gemini_cli": ("swfactory.engines.gemini_cli", "GeminiCliEngine"),
    "direct": ("swfactory.engines.direct", "DirectEngine"),
}


def register_engine(engine: Engine) -> None:
    _engines[engine.name] = engine


def register_engines(settings: "Settings") -> None:
    """Register one instance of each engine module that exists."""
    for module, cls in _BUILDERS.values():
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name != module:
                raise
            continue
        register_engine(getattr(mod, cls)(settings))
    if "fake" not in _engines:
        from swfactory.engines.fake import FakeEngine, FakeScript

        register_engine(FakeEngine(FakeScript()))


def get_engine(name: str) -> Engine:
    try:
        return _engines[name]
    except KeyError:
        raise KeyError(f"unknown engine: {name}") from None
