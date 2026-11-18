"""Process-wide engine registry."""

from swfactory.engines.base import Engine

_engines: dict[str, Engine] = {}


def register_engine(engine: Engine) -> None:
    _engines[engine.name] = engine


def get_engine(name: str) -> Engine:
    try:
        return _engines[name]
    except KeyError:
        raise KeyError(f"unknown engine: {name}") from None
