import subprocess
import uuid
from pathlib import Path

import docker
import pytest

from swfactory.definition.models import SmokeConfig
from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.docker import DockerSandboxProvider
from swfactory.verify.smoke import SmokeRunner

IMAGE = "swfactory/runner-web:0.1.0"
WEB = RunnerSpec("web", IMAGE, 2, 4096, 512, ())
WEB_SMOKE = SmokeConfig(
    start_command="python3 app.py", port=8000, paths=["/", "/about"], screenshots=["home", "about"]
)
APP = Path(__file__).parents[1] / "fixtures" / "repos" / "webapp" / "app.py"


def run_web(cmd: str) -> int:
    return subprocess.run(
        ["docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/home/factory",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges", IMAGE, "sh", "-c", cmd],
        capture_output=True,
    ).returncode  # fmt: skip


@pytest.mark.docker
def test_web_runner_launches_chromium_under_template():
    code = (
        "require('playwright').chromium.launch({args:['--disable-dev-shm-usage']})"
        ".then(b=>b.close())"
    )
    assert run_web(f'node -e "{code}"') == 0


@pytest.fixture
async def web_box(tmp_path):
    client = docker.from_env()
    provider = DockerSandboxProvider(lambda: client, spool_dir=tmp_path / "spool")
    jid = uuid.uuid4()
    try:
        box = await provider.ensure(jid, WEB)
        await box.write_text("/work/repo/app.py", APP.read_text())
        yield box
    finally:
        await provider.destroy(jid)


@pytest.mark.docker
async def test_web_fixture_screenshots(web_box):
    r = await SmokeRunner().run(web_box, WEB_SMOKE)
    assert r.passed, r.detail
    assert [n for n, _ in r.shots] == ["home", "about"]
    assert all(png.startswith(b"\x89PNG") for _, png in r.shots)
