import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import swfactory.gateway.render as render_mod


@pytest.fixture(autouse=True)
def _fast_ready_poll(monkeypatch):
    monkeypatch.setattr(render_mod, "READY_POLL_S", 0.01)


class _Container:
    def __init__(self, docker, service):
        self.docker, self.service = docker, service

    @property
    def attrs(self):
        ts = self.docker.started_at.get(self.service, time.time() + 3600)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1e9):09d}Z"
        return {"State": {"StartedAt": iso}}

    def reload(self):
        pass

    def restart(self, timeout=10):
        self.docker.restarts.append(self.service)


class _Containers:
    def __init__(self, docker):
        self.docker = docker

    def list(self, filters=None, **kw):
        label = (filters or {}).get("label", "")
        prefix = "com.docker.compose.service="
        assert label.startswith(prefix)
        return [_Container(self.docker, label[len(prefix) :])]


class FakeDocker:
    def __init__(self):
        self.restarts: list[str] = []
        self.started_at: dict[str, float] = {}
        self.containers = _Containers(self)


@pytest.fixture
def fake_docker():
    return FakeDocker()


class Ready:
    def __init__(self):
        self.script = [200]
        self.calls = 0
        self.base = ""

    @property
    def url(self):
        return self.base + "/health/readiness"


@pytest.fixture
def ready():
    r = Ready()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            code = r.script[min(r.calls, len(r.script) - 1)]
            r.calls += 1
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    r.base = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield r
    srv.shutdown()
    srv.server_close()
