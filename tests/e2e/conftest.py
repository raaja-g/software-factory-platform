"""Fixtures for the opt-in end-to-end tests: the compose stack must already be running."""

import asyncio
import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.definition.loader import load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.gateway.client import GatewayClient
from swfactory.gitops.local import LocalGit
from swfactory.models.catalog import Catalog
from swfactory.settings import Settings, get_settings
from swfactory.worker import run_worker

HERE = Path(__file__).parent
collect_ignore = ["fixtures"]  # the calc-app fixture has its own tests
ROOT = HERE.parents[1]
FIXTURE_APP = HERE / "fixtures" / "calc-app"
DEFINITION = HERE / "definition"
SERVICES = ("postgres", "gateway", "gateway-edge")
IMAGES = ("swfactory/runner:0.1.0",)
GATEWAY_URL = "http://127.0.0.1:4000"
GATEWAY_SANDBOX_URL = "http://gateway-edge:4000"


@dataclass
class Stack:
    settings: Settings
    sessions: object  # async_sessionmaker[AsyncSession]
    repos_dir: Path
    gateway: GatewayClient


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)


def _require_stack() -> None:
    if shutil.which("docker") is None:
        pytest.skip("opt-in: docker is not installed")
    if _run("docker", "info").returncode != 0:
        pytest.skip("opt-in: the local Docker socket does not answer `docker info`")
    ps = _run("docker", "compose", "ps", "--format", "{{.Service}} {{.Health}}", *SERVICES)
    healthy = {
        line.split()[0] for line in ps.stdout.splitlines() if line.strip().endswith("healthy")
    }
    if not set(SERVICES) <= healthy:
        pytest.skip(f"opt-in: compose services not all healthy: need {', '.join(SERVICES)}")
    for image in IMAGES:
        if _run("docker", "image", "inspect", image).returncode != 0:
            pytest.skip(f"opt-in: runner image {image} is not built")


@pytest_asyncio.fixture
async def stack(pg_url, tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    _require_stack()
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("FACTORY_DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("FACTORY_GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("FACTORY_GATEWAY_SANDBOX_URL", GATEWAY_SANDBOX_URL)
    monkeypatch.setenv("FACTORY_MODE", "personal")
    monkeypatch.setenv("FACTORY_REPOS_DIR", str(repos))
    monkeypatch.setenv("FACTORY_HOST_REPOS_ROOT", str(repos))
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    master = ROOT / "secrets" / "litellm_master_key"
    if master.is_file():
        monkeypatch.setenv("LITELLM_MASTER_KEY", master.read_text().strip())
    get_settings.cache_clear()
    settings = get_settings()

    LocalGit(repos).init_bare("calc-app", seed=FIXTURE_APP)

    # roles and runners come from the packaged defaults; the e2e dir adds only factory + profiles
    defn_dir = tmp_path / "definition"
    shutil.copytree(DEFINITION, defn_dir)
    shutil.copytree(ROOT / "defaults" / "roles", defn_dir / "roles", dirs_exist_ok=True)
    shutil.copytree(ROOT / "defaults" / "runners", defn_dir / "runners", dirs_exist_ok=True)
    result = load_definition(str(defn_dir), mode=settings.mode)
    assert not result.errors, result.errors

    engine = make_engine(pg_url)
    sessions = make_sessionmaker(engine)
    gateway = GatewayClient(GATEWAY_URL, settings.gateway_master_key.get_secret_value())
    await DefinitionStore(sessions, mode=settings.mode).save(result, activate=True)

    worker = asyncio.create_task(run_worker(settings, worker_id="e2e", max_jobs=1))
    try:
        yield Stack(settings, sessions, repos, gateway)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker
        await gateway.aclose()
        await engine.dispose()


@pytest_asyncio.fixture
async def catalog(stack):  # noqa: ANN001, ANN201
    store = DefinitionStore(stack.sessions, mode=stack.settings.mode)
    cat = Catalog(stack.sessions, stack.gateway, store)
    try:
        await cat.refresh()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"opt-in: gateway catalog refresh failed: {e}")
    return cat
