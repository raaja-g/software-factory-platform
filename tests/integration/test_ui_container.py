"""The UI container, for real: built from docker/ui.Dockerfile, on a throwaway network in front of a
stand-in for the API that answers every request with what it received."""

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.docker

IMAGE = "swfactory-ui:test"
STUB = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _any(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps({"method": self.command, "path": self.path,
                           "host": self.headers.get("host"), "origin": self.headers.get("origin"),
                           "xff": self.headers.get("x-forwarded-for")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _any
ThreadingHTTPServer(("0.0.0.0", 7080), H).serve_forever()
"""


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=240)


@pytest.fixture(scope="module")
def ui():
    net = f"ui-test-{uuid.uuid4().hex[:8]}"
    sh("docker", "build", "-q", "-f", "docker/ui.Dockerfile", "-t", IMAGE, ".")
    sh("docker", "network", "create", net)
    stub = ui_c = None
    try:
        # the stand-in for the API must be called `api` on the network, as in the compose stack
        stub = sh("docker", "run", "-d", "--rm", "--network", net, "--network-alias", "api",
                  "python:3.13-slim", "python", "-c", STUB).stdout.strip()  # fmt: skip
        run_ui = ["docker", "run", "-d", "--rm", "--network", net, "-p", "127.0.0.1::7000"]
        run_ui += ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", IMAGE]
        ui_c = sh(*run_ui).stdout.strip()
        port = sh("docker", "port", ui_c, "7000/tcp").stdout.split(":")[-1].strip()
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                if urllib.request.urlopen(base + "/ui-health", timeout=2).status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(sh("docker", "logs", ui_c, check=False).stderr)
        for _ in range(60):  # the stand-in API starts slower than nginx; wait until /api answers
            try:
                if urllib.request.urlopen(base + "/api/ready-check", timeout=2).status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("the stand-in API never answered through the UI container")
        yield base, ui_c
    finally:
        for c in (ui_c, stub):
            if c:
                sh("docker", "rm", "-f", c, check=False)
        sh("docker", "network", "rm", net, check=False)


def call(base: str, method: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(base + path, method=method, headers=headers or {})
    if method == "POST":
        req.data = b"{}"
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_serves_the_dashboard_with_security_headers(ui):
    base, _ = ui
    status, headers, body = call(base, "GET", "/")
    assert status == 200 and "<html" in body.lower()
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert call(base, "GET", "/app.js")[0] == 200


def test_forwards_api_calls_with_host_and_origin_intact(ui):
    base, _ = ui
    status, _, body = call(base, "POST", "/api/jobs", {"Origin": base})
    seen = json.loads(body)
    assert status == 200 and seen["method"] == "POST" and seen["path"] == "/api/jobs"
    assert seen["origin"] == base and seen["host"] == base.removeprefix("http://")


def test_forwards_the_login_link_with_its_query(ui):
    base, _ = ui
    seen = json.loads(call(base, "GET", "/login?code=abc")[2])
    assert seen["path"] == "/login?code=abc"


def test_does_not_forward_anything_else(ui):
    base, _ = ui
    assert call(base, "POST", "/login")[0] == 403
    assert call(base, "POST", "/")[0] == 403
    assert call(base, "GET", "/hooks/anything")[0] == 404  # webhooks do not come through the UI
    assert call(base, "GET", "/readyz")[0] == 404


def test_runs_as_a_non_root_user(ui):
    _, container = ui
    assert sh("docker", "exec", container, "id", "-u").stdout.strip() != "0"
