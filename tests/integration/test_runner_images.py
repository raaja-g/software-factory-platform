import subprocess

import pytest

IMAGE = "swfactory/runner:0.1.0"


def _run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", IMAGE, "sh", "-c", cmd], capture_output=True, text=True
    )


def run(cmd: str) -> str:
    return _run(cmd).stdout.strip()


def exit_code(cmd: str) -> int:
    return _run(cmd).returncode


@pytest.mark.docker
def test_runner_toolchain():
    assert run("id -u") == "10001"
    assert run("node --version").startswith("v24.")
    for tool in (
        "git --version",
        "python3 --version",
        "uv --version",
        "rg --version",
        "socat -V",
        "gcc --version",
        "make --version",
    ):
        assert exit_code(tool) == 0
