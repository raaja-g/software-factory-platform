from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.engines.registry import ENGINE_NAMES, get_engine, register_engines
from swfactory.models.catalog import ModelInfo
from swfactory.models.profiles import engine_support
from swfactory.sandbox.paths import REPO

FIXTURES = Path(__file__).parent / "fixtures"


def info(alias: str, upstream: str, **kw) -> ModelInfo:
    base = dict(
        alias=alias, provider="p", upstream_id=upstream, strength="mid",
        price_in_per_mtok=Decimal(1), price_out_per_mtok=Decimal(2), context_window=100000,
        supports_tools=True, messages_tools_ok=True, available=True, error_class=None,
        latency_ms=10, last_error=None,
    )  # fmt: skip
    base.update(kw)
    return ModelInfo(**base)  # type: ignore[arg-type]


SONNET = info("sonnet@vertex", "claude-sonnet-4-6")
FLASH = info("flash@vertex", "gemini-4-flash")
QWEN_NO_TOOLS = info("qwen@ollama", "qwen/qwen3-coder", supports_tools=False)


def test_get_engine_unknown_raises():
    with pytest.raises(KeyError, match="aider"):
        get_engine("aider")


@pytest.mark.parametrize(
    "model",
    [
        SONNET,
        FLASH,
        replace(FLASH, supports_tools=None),
        replace(FLASH, supports_tools=False),
        replace(SONNET, messages_tools_ok=False),
        replace(FLASH, available=False),
        QWEN_NO_TOOLS,
    ],
)
def test_support_matches_profiles_table(settings, model):
    register_engines(settings)
    for name in ENGINE_NAMES:
        try:
            engine = get_engine(name)
        except KeyError:
            continue  # adapter module not built yet
        assert engine.support_for(model) == engine_support(name, model)


async def test_stub_replays_fixture(tmp_path):
    box = StubSandbox(tmp_path, FIXTURES / "claude_code/success.jsonl")
    r = await box.exec(["claude", "-p"], stdin="hi", env={"X": "1"})
    assert r.stdout == (FIXTURES / "claude_code/success.jsonl").read_text()
    assert r.exit_code == 0
    assert box.calls == [{"argv": ["claude", "-p"], "env": {"X": "1"}, "stdin": "hi", "cwd": REPO}]


async def test_stub_reports_timeout(tmp_path):
    r = await StubSandbox(tmp_path, None, exit_code=124, timed_out=True).exec(["opencode", "run"])
    assert (r.exit_code, r.timed_out, r.stdout) == (124, True, "")


async def test_stub_maps_sandbox_paths_in_argv(tmp_path):
    box = StubSandbox(tmp_path, None)
    await box.write_text("/home/factory/x/y.txt", "1")
    assert (await box.exec(["rm", "-rf", "/home/factory/x"])).exit_code == 0
    assert await box.read_text("/home/factory/x/y.txt") is None
