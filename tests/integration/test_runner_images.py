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


@pytest.mark.docker
def test_runner_user_can_seed_the_opencode_cache():
    """OpenCode's pre-installed provider packages are copied into the home cache at run time.
    `npm rebuild opencode-ai` runs as root with HOME=/home/factory and used to leave `.cache` owned
    by root, so every run logged "opencode cache copy failed: Permission denied" and OpenCode had
    to fetch its provider package itself instead of using the offline copy."""
    assert run("stat -c %u /home/factory/.cache 2>/dev/null || echo missing") in (
        "10001",
        "missing",
    )
    seeded = _run(
        "mkdir -p /home/factory/.cache/opencode"
        ' && cp -an "$OPENCODE_CACHE"/. /home/factory/.cache/opencode/'
        " && ls /home/factory/.cache/opencode/node_modules | head -1"
    )
    assert seeded.returncode == 0, seeded.stderr
    assert seeded.stdout.strip() != ""
