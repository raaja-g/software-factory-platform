import subprocess
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.docker
IMG = "swfactory-platform:dev"


def sh(cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def build() -> None:
    assert sh(f"docker build -f docker/platform.Dockerfile -t {IMG} .").returncode == 0


def docker_run(*args: str) -> str:
    r = subprocess.run(["docker", "run", "--rm", *args], capture_output=True, text=True)
    return r.stdout.strip()


def http(url: str, data: bytes | None = None) -> int:
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_image_non_root_and_cli():
    build()
    assert docker_run("--entrypoint", "id", IMG, "-u") == "10001"
    assert "serve" in docker_run(IMG, "--help")


def test_image_multi_arch():
    cmd = (
        "docker buildx build --platform linux/amd64,linux/arm64 "
        "-f docker/platform.Dockerfile --output type=cacheonly ."
    )
    assert sh(cmd).returncode == 0


def test_make_up_ready_and_down():
    out = sh("make up").stdout
    try:
        assert "http://127.0.0.1:7080/login?code=" in out
        assert http("http://127.0.0.1:7080/readyz") == 200
        assert http("http://127.0.0.1:7080/api/jobs", b"{}") == 401
    finally:
        sh("make down")


def test_fresh_stack_serves_the_active_definition():
    import json
    import os

    sh("rm -f var/litellm.yaml && make up")
    try:
        key = os.environ.get("LITELLM_MASTER_KEY", "")
        req = urllib.request.Request(
            "http://127.0.0.1:7400/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        served = {m["id"] for m in json.load(urllib.request.urlopen(req, timeout=10))["data"]}
        from swfactory.definition.loader import load_definition

        r = load_definition(os.environ["FACTORY_DEFINITION_PATH"])
        assert r.definition is not None
        assert served == set(r.definition.factory.models)
    finally:
        sh("make down")
