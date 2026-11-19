"""Read-only PyPI proxy with name-prefix routing (stdlib only).

Serves /root/pypi/+simple/<name>/ from the route whose prefix starts the normalized name, else from
the default index. A denied prefix answers 404, so a private name never resolves publicly. Only GET
and HEAD are answered; everything else is 405. Credentials come from the routes file, are sent only
to the route's own index host, and never reach the client. SIGHUP reloads the routes file.
"""

import base64
import json
import re
import signal
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

PORT = 3141
BASE = "/root/pypi"
HREF = re.compile(r'(href=")([^"]+)(")')
_state: dict = {"routes": {"routes": [], "default": {}, "upload": False}}
_lock = threading.Lock()


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load(path: str) -> None:
    with open(path) as f:
        doc = json.load(f)
    with _lock:
        _state["routes"] = doc


def pick(name: str) -> dict | None:
    """The route for a project name: a private route, a deny, or the default."""
    with _lock:
        doc = _state["routes"]
    n = norm(name)
    for route in sorted(doc["routes"], key=lambda r: -len(r["prefix"])):
        if n.startswith(norm(route["prefix"])):
            return route
    return doc["default"] or None


def _auth_header(route: dict) -> dict[str, str]:
    a = route.get("auth")
    if not a:
        return {}
    raw = f"{a['username']}:{a['password']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


def fetch(url: str, route: dict, accept: str = "text/html") -> tuple[int, str, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    index_host = urlsplit(route["index"]).hostname
    for _ in range(6):
        headers = {"Accept": accept, "User-Agent": "factory-pkg-mirror"}
        if urlsplit(url).hostname == index_host:
            headers.update(_auth_header(route))
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return r.status, r.headers.get("Content-Type", "application/octet-stream"), r.read()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc)
                continue
            return e.code, "text/plain", b""
        except (urllib.error.URLError, OSError):
            return 502, "text/plain", b""
    return 502, "text/plain", b""


def enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def dec(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()


def rewrite(page: str, page_url: str, route_id: str) -> str:
    def sub(m: re.Match) -> str:
        absolute = urljoin(page_url, m.group(2).split("#", 1)[0])
        frag = "#" + m.group(2).split("#", 1)[1] if "#" in m.group(2) else ""
        fname = urlsplit(absolute).path.rsplit("/", 1)[-1]
        return f"{m.group(1)}{BASE}/+f/{route_id}/{enc(absolute)}/{fname}{frag}{m.group(3)}"

    return HREF.sub(sub, page)


def _route_by_id(route_id: str) -> dict | None:
    with _lock:
        doc = _state["routes"]
    if route_id == "default":
        return doc["default"] or None
    for r in doc["routes"]:
        if r["prefix"] == route_id and not r.get("deny"):
            return r
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "pkg-mirror-pypi"

    def log_message(self, *a):
        pass

    def _send(self, code: int, ctype: str = "text/plain", body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, body=b"ok")
        simple = f"{BASE}/+simple/"
        if path.startswith(simple):
            name = path[len(simple) :].strip("/")
            if not name or "/" in name:
                return self._send(404)
            route = pick(name)
            if route is None or route.get("deny"):
                return self._send(404)
            url = route["index"].rstrip("/") + "/" + norm(name) + "/"
            code, _, body = fetch(url, route)
            if code != 200:
                return self._send(404 if code == 404 else 502)
            rid = route.get("prefix", "default")
            page = rewrite(body.decode("utf-8", "replace"), url, rid)
            return self._send(200, "text/html; charset=utf-8", page.encode())
        if path.startswith(f"{BASE}/+f/"):
            parts = path[len(f"{BASE}/+f/") :].split("/")
            if len(parts) < 2:
                return self._send(404)
            route = _route_by_id(parts[0])
            if route is None:
                return self._send(404)
            try:
                url = dec(parts[1])
            except ValueError:
                return self._send(404)
            host = urlsplit(url).hostname or ""
            allowed = host == urlsplit(route["index"]).hostname or (
                parts[0] == "default" and (host.endswith(".pythonhosted.org") or host == "pypi.org")
            )
            if not allowed or urlsplit(url).scheme != "https":
                return self._send(403)
            code, ctype, body = fetch(url, route, accept="*/*")
            return self._send(code if code in (200, 404) else 502, ctype, body)
        self._send(404)

    do_HEAD = do_GET  # noqa: N815


def main(argv: list[str]) -> None:
    path = argv[1]
    load(path)
    signal.signal(signal.SIGHUP, lambda *_: load(path))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main(sys.argv)
