from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.engines.registry import get_engine
from swfactory.gitops.service import GitDispatcher
from swfactory.sandbox.docker import DockerSandboxProvider
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.settings import Settings
from swfactory.worker import build_worker_deps


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def sessions():
    return async_sessionmaker(create_async_engine("postgresql+psycopg://u:p@127.0.0.1:1/x"))


@pytest.fixture
def happy(tmp_path: Path) -> Path:
    p = tmp_path / "happy.yaml"
    p.write_text("{}\n")
    return p


@pytest.fixture
def recorded_run_worker(monkeypatch):
    seen: dict = {}

    async def fake_run_worker(settings, **kw):
        seen.update(kw)

    monkeypatch.setattr("swfactory.worker.run_worker", fake_run_worker)
    return seen


def test_real_deps_without_fake_script(settings, sessions, monkeypatch):
    import docker

    def boom(*a, **k):
        raise AssertionError("docker must not be contacted")

    monkeypatch.setattr(docker, "DockerClient", boom)
    deps = build_worker_deps(settings, sessions, fake_script=None)
    assert isinstance(deps.sandboxes, DockerSandboxProvider)
    assert isinstance(deps.git, GitDispatcher)
    assert deps.gateway.base_url == settings.gateway_url == "http://127.0.0.1:4000"
    for name in ("opencode", "claude_code", "gemini_cli", "direct"):
        assert get_engine(name).name == name


def test_fake_script_still_uses_fakes(settings, sessions, happy):
    deps = build_worker_deps(settings, sessions, fake_script=happy)
    assert isinstance(deps.sandboxes, FakeSandboxProvider)


def test_cli_no_longer_exits_2(recorded_run_worker):
    r = CliRunner().invoke(app, ["worker", "--exit-when-idle"])
    assert r.exit_code == 0, r.output
    assert recorded_run_worker["fake_script"] is None
