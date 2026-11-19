import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pypi_proxy", Path("deploy/pkg-mirror/pypi_proxy.py").resolve()
)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class Upstream(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        Upstream.seen.append((self.path, self.headers.get("Authorization")))
        body = b'<a href="../../files/x-1.0.tar.gz#sha256=abc">x</a>'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def served(tmp_path):
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    index = f"http://127.0.0.1:{up.server_port}/simple/"
    routes = {
        "routes": [
            {
                "prefix": "acme-",
                "index": index,
                "auth": {"username": "__token__", "password": "pw"},
            },
            {"prefix": "secret-", "deny": True},
        ],
        "default": {"index": index},
        "upload": False,
    }
    p = tmp_path / "pypi.json"
    p.write_text(json.dumps(routes))
    proxy.load(str(p))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Upstream.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    up.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_private_prefix_uses_its_index_with_auth_and_hides_the_credential(served):
    code, body = get(f"{served}/root/pypi/+simple/Acme_Tools/")
    assert code == 200 and "pw" not in body and "/root/pypi/+f/acme-/" in body
    assert body.endswith('#sha256=abc">x</a>')
    assert Upstream.seen[0][0] == "/simple/acme-tools/" and (Upstream.seen[0][1] or "").startswith(
        "Basic "
    )


def test_public_names_use_the_default_index_without_auth(served):
    assert get(f"{served}/root/pypi/+simple/six/")[0] == 200
    assert Upstream.seen[0][1] is None


def test_denied_prefix_is_404_and_never_asks_upstream(served):
    assert get(f"{served}/root/pypi/+simple/secret-lib/")[0] == 404
    assert Upstream.seen == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_uploads_are_refused(served, method):
    req = urllib.request.Request(f"{served}/root/pypi/", data=b"{}", method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 405


WHEEL = "https://files.pythonhosted.org/packages/b7/ce/x/six-1.17.0-py2.py3-none-any.whl"


def test_metadata_request_fetches_the_metadata_file_not_the_wheel():
    # uv checks the PEP 658 metadata hash the index advertises; serving the wheel breaks installs
    assert (
        proxy.upstream_file_url(WHEEL, "six-1.17.0-py2.py3-none-any.whl.metadata")
        == WHEEL + ".metadata"
    )
    assert proxy.upstream_file_url(WHEEL, "six-1.17.0-py2.py3-none-any.whl") == WHEEL
    assert proxy.upstream_file_url(WHEEL + ".metadata", "x.whl.metadata") == WHEEL + ".metadata"
    assert proxy.upstream_file_url(WHEEL, "") == WHEEL
