import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = "tests/fixtures/gateway/test.env"
MASTER = "sk-test-master"
MOCK_CHAT = {"model": "mock", "messages": [{"role": "user", "content": "ping"}]}

pytestmark = pytest.mark.docker


def _env() -> dict[str, str]:
    out = {}
    for line in (ROOT / ENV_FILE).read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--env-file", ENV_FILE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def _config() -> dict:
    return json.loads(_compose("config", "--format", "json").stdout)


_PROBE = """
import json, sys, urllib.request, urllib.error
method, url, key = sys.argv[1:4]
body = sys.stdin.buffer.read() or None
req = urllib.request.Request(url, data=body, method=method)
req.add_header("Authorization", "Bearer " + key)
if body:
    req.add_header("Content-Type", "application/json")
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
"""


@pytest.fixture(scope="module")
def stack():
    _compose("up", "-d", "--wait", "--wait-timeout", "300", "gateway", "gateway-edge")
    yield
    _compose("stop", "gateway", "gateway-edge", check=False)


def _gw(method: str, path: str, **kw):
    import httpx

    return httpx.request(
        method,
        f"http://127.0.0.1:4000{path}",
        headers={"Authorization": f"Bearer {MASTER}"},
        timeout=60,
        **kw,
    )


@pytest.fixture(scope="module")
def run_key(stack) -> str:
    r = _gw(
        "POST", "/key/generate", json={"models": ["mock"], "max_budget": 0.01, "duration": "600s"}
    )
    return r.json()["key"]


@pytest.fixture
def job_net(stack):
    name = f"factory-net-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "network", "create", name], check=True, capture_output=True)
    edge = _compose("ps", "-q", "gateway-edge").stdout.strip()
    subprocess.run(
        ["docker", "network", "connect", "--alias", "gateway-edge", name, edge],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(["docker", "network", "disconnect", "-f", name, edge], capture_output=True)
    subprocess.run(["docker", "network", "rm", name], capture_output=True)


def _edge_status(net: str, method: str, path: str, key: str, body: dict | None = None) -> int:
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            net,
            "--entrypoint",
            "python",
            _env()["LITELLM_IMAGE"],
            "-c",
            _PROBE,
            method,
            f"http://gateway-edge:4000{path}",
            key,
        ],
        input=json.dumps(body) if body is not None else "",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return int(r.stdout.strip().splitlines()[-1])


def test_gateway_service_config():
    cfg = _config()
    g = cfg["services"]["gateway"]
    assert g["image"] == _env()["LITELLM_IMAGE"]
    assert g["image"].startswith("ghcr.io/berriai/litellm-non_root@sha256:")
    assert g["user"] not in ("", "0", "root")
    assert g["read_only"] is True and g["cap_drop"] == ["ALL"]
    assert [(p["host_ip"], p["published"]) for p in g["ports"]] == [("127.0.0.1", "4000")]
    env = g["environment"]
    assert env["LITELLM_MASTER_KEY"].startswith("sk-") and env["DISABLE_ADMIN_UI"] == "True"
    assert env["DISABLE_SCHEMA_UPDATE"] == "true" and env["STORE_MODEL_IN_DB"] == "False"
    assert env["DATABASE_URL"].endswith("/litellm")
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/secrets/adc.json"
    assert "GOOGLE_CLOUD_PROJECT" in env and not any(k.startswith("GCP_PROJECT") for k in env)
    assert any(v["target"] == "/secrets/adc.json" and v["read_only"] for v in g["volumes"])
    assert set(g["networks"]) == {"core-net"}
    assert "/health/readiness" in " ".join(g["healthcheck"]["test"])
    assert not any(
        "adc.json" in str(s.get("volumes", ""))
        for n, s in cfg["services"].items()
        if n != "gateway"
    )


def test_edge_service_config():
    cfg = _config()
    e = cfg["services"]["gateway-edge"]
    assert "@sha256:" in e["image"] and set(e["networks"]) == {"core-net"} and "ports" not in e
    assert e["read_only"] is True and e["cap_drop"] == ["ALL"]
    assert "sandbox-net" not in cfg["networks"]


def test_stack_mints_key(run_key):
    assert _gw("GET", "/health/liveliness").status_code == 200
    assert run_key.startswith("sk-")


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/key/generate"),
        ("GET", "/key/info"),
        ("GET", "/spend/logs"),
        ("GET", "/health"),
        ("GET", "/ui"),
        ("POST", "/model/new"),
        (
            "POST",
            "/vertex_ai/v1/projects/p/locations/global/publishers/google/models/m:generateContent",
        ),
        ("POST", "/gemini/v1beta/models/m:generateContent"),
        ("POST", "/anthropic/v1/messages"),
    ],
)
def test_edge_refuses_non_model_routes(job_net, run_key, method, path):
    assert _edge_status(job_net, method, path, run_key, {} if method == "POST" else None) == 403


def test_gateway_not_on_job_network(job_net):
    # Container labels are not on the network object; resolve each attached container instead.
    ids = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            job_net,
            "--format",
            "{{range $k, $v := .Containers}}{{$k}} {{end}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    services = set()
    for cid in ids:
        services.add(
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    cid,
                    "--format",
                    '{{index .Config.Labels "com.docker.compose.service"}}',
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    assert services == {"gateway-edge"}


def test_edge_forwards_model_routes(job_net, run_key):
    assert _edge_status(job_net, "POST", "/v1/chat/completions", run_key, MOCK_CHAT) == 200
    assert _edge_status(job_net, "GET", "/v1/models", run_key) == 200


def test_edge_passes_large_body(job_net, run_key):
    big = {**MOCK_CHAT, "messages": [{"role": "user", "content": "x" * 5_000_000}]}
    assert _edge_status(job_net, "POST", "/v1/chat/completions", run_key, big) == 200


def test_edge_follows_recreated_gateway(job_net, run_key):
    _compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "300", "gateway")
    deadline = time.monotonic() + 30
    status = 0
    while time.monotonic() < deadline:
        status = _edge_status(job_net, "POST", "/v1/chat/completions", run_key, MOCK_CHAT)
        if status == 200:
            break
        time.sleep(2)
    assert status == 200
