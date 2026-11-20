import json
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import docker
import pytest

from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.docker import DockerSandboxProvider

pytestmark = pytest.mark.docker
IMG = "swfactory-router:dev"
SKIP = "opt-in: build swfactory-router:dev (make router)"
RUNNER = RunnerSpec("standard", "swfactory/runner:0.1.0", 1, 1024, 512, ())

BATCH_FIXTURE = {
    "states": ["Add a dark mode toggle to the page"],
    "questions": {
        "touches_ui": {
            "type": "choice",
            "instructions": "Does the change touch the user interface?",
            "criteria": {"A": "no user interface change", "B": "changes the user interface"},
        }
    },
}

# Runs inside the container (which has no network): a tiny HTTP client.
CLIENT = """
import json, sys, urllib.error, urllib.request as u
path, body, key = sys.argv[1], sys.argv[2], sys.argv[3]
hdrs = {"content-type": "application/json"}
if key:
    hdrs["authorization"] = "Bearer " + key
req = u.Request("http://127.0.0.1:8000" + path, data=body.encode() if body else None, headers=hdrs)
try:
    r = u.urlopen(req, timeout=60); status, hdr, out = r.status, dict(r.headers), r.read().decode()
except urllib.error.HTTPError as e:
    status, hdr, out = e.code, dict(e.headers), e.read().decode()
print(json.dumps({"status": status, "headers": hdr, "body": out}))
"""


@pytest.fixture(scope="module", autouse=True)
def image():
    r = subprocess.run(["docker", "image", "inspect", IMG], capture_output=True)
    if r.returncode != 0:
        pytest.skip(SKIP)


def docker_run_rc(*args: str, env: dict[str, str] | None = None) -> int:
    cmd = ["docker", "run", "--rm"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    return subprocess.run([*cmd, *args], capture_output=True, timeout=120).returncode


class Running:
    def __init__(self, name: str):
        self.name = name

    def restart(self) -> None:
        subprocess.run(["docker", "restart", self.name], check=True, capture_output=True)
        wait_healthy(self.name)

    def post(self, path: str, body: dict, bearer: str = "") -> dict:
        r = self.request(path, body, bearer)
        assert r["status"] == 200, r
        return json.loads(r["body"])

    def request(self, path: str, body: dict | None = None, bearer: str = "") -> dict:
        r = subprocess.run(
            [
                "docker",
                "exec",
                self.name,
                "python",
                "-c",
                CLIENT,
                path,
                json.dumps(body) if body else "",
                bearer,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def wait_healthy(name: str) -> None:
    deadline = time.time() + 120
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "exec", name, "python", "-c", CLIENT, "/health", "", ""],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and json.loads(r.stdout)["status"] == 200:
            return
        time.sleep(2)
    raise AssertionError(
        subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr
    )


@contextmanager
def running(env: dict[str, str], network: str = "none", heads_volume: str | None = None):
    name = f"router-test-{uuid.uuid4().hex[:8]}"
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
    ]
    # /heads is a volume (compose mounts `laya-heads`); a read-only root filesystem needs one
    cmd += ["-v", f"{heads_volume}:/heads"] if heads_volume else ["--tmpfs", "/heads:uid=10001"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    subprocess.run([*cmd, IMG], check=True, capture_output=True)
    try:
        c = Running(name)
        wait_healthy(name)
        yield c
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_image_offline_non_root_and_keyed():
    uid = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "id", IMG, "-u"], capture_output=True, text=True
    )
    assert uid.stdout.strip() == "10001"
    assert docker_run_rc(IMG) != 0  # no LAYA_API_KEY -> refuses to start
    assert docker_run_rc(IMG, env={"LAYA_API_KEY": ""}) != 0
    with running({"LAYA_API_KEY": "k"}) as c:  # weights are baked, so no network is needed
        assert c.request("/health")["status"] == 200
        assert c.request("/v1/systemone/batch", BATCH_FIXTURE)["status"] == 401
        r = c.request("/v1/systemone/batch", BATCH_FIXTURE, bearer="k")
        assert r["status"] == 200
        assert r["headers"]["x-factory-router-version"] == "laya 0.4.31"
        results = json.loads(r["body"])["results"]
        assert results[0]["head_version"] is None
        assert results[0]["answers"]["touches_ui"]["choice"] in {"A", "B"}
        one = c.request(
            "/v1/systemone",
            {"state": BATCH_FIXTURE["states"][0], "questions": BATCH_FIXTURE["questions"]},
            "k",
        )
        assert one["status"] == 200 and json.loads(one["body"])["head_version"] is None


FIXTURE_CSV = Path(__file__).parents[1] / "fixtures" / "laya" / "touches_ui.csv"
TOUCHES_UI = {
    "type": "choice",
    "instructions": "Does the change touch the browser user interface?",
    "criteria": {
        "A": "changes browser-rendered UI or browser behaviour",
        "B": "backend or CLI only",
    },
}
TOUCHES_UI_BATCH = {
    "states": ["Add a dark mode toggle to the page"],
    "questions": {"touches_ui": TOUCHES_UI},
}


def touches_ui_version(c: Running, **extra) -> str | None:
    results = c.post("/v1/systemone/batch", {**TOUCHES_UI_BATCH, **extra}, "k")["results"]
    return results[0]["answers"]["touches_ui"]["head_version"]


def test_fit_roundtrip():
    volume = f"router-heads-{uuid.uuid4().hex[:8]}"
    fit_body = {
        "question_id": "touches_ui",
        "questions": {"touches_ui": TOUCHES_UI},
        "csv": FIXTURE_CSV.read_text(),
        "version": 1,
    }
    try:
        with running({"LAYA_API_KEY": "k"}, heads_volume=volume) as c:
            assert c.request("/v1/factory/fit", fit_body)["status"] == 401  # keyed like the rest
            r = c.post("/v1/factory/fit", fit_body, "k")
            assert r["head_version"] == "touches_ui@v1" and len(r["head_sha256"]) == 64
            assert r["metrics"]["rows"] == 60
            assert touches_ui_version(c) is None  # a candidate serves nothing
            assert touches_ui_version(c, pins={"touches_ui": 1}) == "touches_ui@v1"
            assert touches_ui_version(c) is None  # and a pin does not stick
            bad = c.request(
                "/v1/factory/heads/activate",
                {"question_id": "touches_ui", "version": 1, "head_sha256": "0" * 64},
                "k",
            )
            assert bad["status"] == 422 and touches_ui_version(c) is None
            act = {"question_id": "touches_ui", "version": 1, "head_sha256": r["head_sha256"]}
            c.post("/v1/factory/heads/activate", act, "k")
            assert touches_ui_version(c) == "touches_ui@v1"
            listed = json.loads(c.request("/v1/factory/heads", None, "k")["body"])["heads"]
            assert [(h["question_id"], h["version"], h["active"]) for h in listed] == [
                ("touches_ui", 1, True)
            ]
            edited = {
                **TOUCHES_UI_BATCH,
                "questions": {
                    "touches_ui": {**TOUCHES_UI, "criteria": {"A": "other", "B": "backend"}}
                },
            }
            r2 = c.post("/v1/systemone/batch", edited, "k")["results"][0]
            assert r2["answers"]["touches_ui"]["head_version"] is None  # edited tiers: unfitted
            c.restart()
            assert touches_ui_version(c) == "touches_ui@v1"  # loaded from /heads
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def provider(client, tmp_path):
    return DockerSandboxProvider(lambda: client, spool_dir=tmp_path / "spool")


@pytest.fixture
def job_id(provider):
    import asyncio

    jid = uuid.uuid4()
    yield jid
    asyncio.run(provider.destroy(jid))


@pytest.fixture
def standins(client):
    made = []
    for svc in ("gateway-edge", "router"):
        made.append(
            client.containers.run(
                RUNNER.image,
                ["sleep", "infinity"],
                detach=True,
                labels={"com.docker.compose.service": svc},
                name=f"{svc}-standin-{uuid.uuid4().hex[:8]}",
            )
        )
    yield {c.labels["com.docker.compose.service"]: c for c in made}
    for c in made:
        c.remove(force=True)


async def test_router_not_on_job_networks(client, provider, job_id, standins):
    await provider.ensure(job_id, RUNNER)
    net = client.networks.get(f"factory-net-{job_id}")
    net.reload()
    members = {c["Name"] for c in net.attrs["Containers"].values()}
    assert standins["gateway-edge"].name in members
    assert standins["router"].name not in members
    assert len(members) == 2  # the job's own container and gateway-edge


def test_multi_arch():
    r = subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64,linux/arm64",
            "-f",
            "docker/router.Dockerfile",
            "--output",
            "type=cacheonly",
            ".",
        ],
        timeout=1500,
    )
    assert r.returncode == 0
