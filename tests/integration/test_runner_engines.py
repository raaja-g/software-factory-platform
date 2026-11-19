import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "swfactory-runner:test"
OPENCODE_CACHE = "/opt/factory-engines/opencode-cache"

OFFLINE_OPENCODE_CMD = f"""
set -e
mkdir -p "$HOME/.config/opencode" "$HOME/.cache/opencode" "$HOME/work"
cp -r {OPENCODE_CACHE}/. "$HOME/.cache/opencode/"
cat > "$HOME/work/opencode.json" <<'EOF'
{{"$schema": "https://opencode.ai/config.json",
  "provider": {{"factory": {{"npm": "@ai-sdk/openai-compatible", "name": "factory",
    "options": {{"baseURL": "http://127.0.0.1:9/v1", "apiKey": "x"}},
    "models": {{"x": {{"name": "x"}}}}}}}}}}
EOF
cd "$HOME/work"
export XDG_CACHE_HOME="$HOME/.cache"
timeout 90 opencode run --format json -m factory/x "hi" 2>&1 || true
"""


def run_in(image: str, cmd: str, network: str | None = None, read_only: bool = False) -> str:
    args = ["docker", "run", "--rm"]
    if network:
        args += ["--network", network]
    if read_only:
        args += [
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home/factory:uid=10001,gid=10001",
            "--cap-drop",
            "ALL",
        ]
    args += [image, "sh", "-c", cmd]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
    return proc.stdout + proc.stderr if network else proc.stdout


@pytest.fixture(scope="module")
def runner_image() -> str:
    subprocess.run(
        ["docker", "build", "-f", "docker/runner.Dockerfile", "-t", IMAGE, "."],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return IMAGE


@pytest.mark.docker
def test_engine_clis_pinned(runner_image):
    out = run_in(
        runner_image,
        "opencode --version; claude --version; gemini --version; socat -V | head -2",
    )
    assert "1.19.5" in out and "2.1.322" in out and "0.68.0" in out and "socat" in out


@pytest.mark.docker
def test_versions_file_matches_lockfile(runner_image):
    want = {"opencode": "1.19.5", "claude_code": "2.1.322", "gemini_cli": "0.68.0"}
    assert json.loads(run_in(runner_image, "cat /etc/factory/engine-versions.json")) == want
    lock = json.loads((ROOT / "docker/engines/package-lock.json").read_text())["packages"]
    assert lock["node_modules/opencode-ai"]["version"] == "1.19.5"
    assert all("integrity" in p for k, p in lock.items() if k and not p.get("link"))
    assert run_in(runner_image, "id -u").strip() == "10001"


@pytest.mark.docker
def test_autoupdate_off(runner_image):
    env = run_in(runner_image, "env")
    assert "DISABLE_AUTOUPDATER=1" in env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in env


@pytest.mark.docker
def test_opencode_providers_preinstalled(runner_image):
    for pkg in ("@ai-sdk/openai-compatible", "@ai-sdk/anthropic"):
        cmd = f"test -f {OPENCODE_CACHE}/node_modules/{pkg}/package.json && echo yes"
        assert run_in(runner_image, cmd).strip() == "yes"


@pytest.mark.docker
def test_opencode_runs_offline(runner_image):
    out = run_in(runner_image, OFFLINE_OPENCODE_CMD, network="none", read_only=True)
    assert "ECONNREFUSED" in out or "Unable to connect" in out, out
    assert "registry.npmjs.org" not in out and "npm ERR" not in out
    assert "EROFS" not in out and "read-only file system" not in out.lower()


TRAILER_CMD = """
set -e
cd "$HOME" && rm -rf trepo && mkdir trepo && cd trepo
git init -q -b main
mkdir -p "$HOME/hooks"
cat > "$HOME/hooks/prepare-commit-msg" <<'HOOK'
__HOOK__
HOOK
chmod 0755 "$HOME/hooks/prepare-commit-msg"
git config core.hooksPath "$HOME/hooks"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export FACTORY_JOB=11111111-2222-3333-4444-555555555555
export FACTORY_ROLE=builder FACTORY_MODEL=flash@vertex
bash -c "echo x > f && git add f && git commit -qm 'add f'"
git log -1 --format='%(trailers:only,unfold)'
"""


def trailer_keys(output: str) -> list[str]:
    return [ln.split(":", 1)[0] for ln in output.splitlines() if ln.startswith("Factory-")]


@pytest.mark.docker
def test_builder_shell_commit_has_trailers(runner_image):
    from swfactory.gitops.hooks import PREPARE_COMMIT_MSG

    cmd = TRAILER_CMD.replace("__HOOK__", PREPARE_COMMIT_MSG.rstrip("\n"))
    out = run_in(runner_image, cmd)
    assert set(trailer_keys(out)) == {"Factory-Job", "Factory-Role", "Factory-Model"}
