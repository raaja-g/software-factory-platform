"""A small synchronous client for the factory API, used by the CLI."""

import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from swfactory.settings import Settings

# Tests install a mock transport here; production leaves it None.
DEFAULT_TRANSPORT: httpx.BaseTransport | None = None


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class RefreshTimeout(Exception):
    pass


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"
    d = body.get("detail") if isinstance(body, dict) else None
    if isinstance(d, str):
        return d
    if d is not None:
        return str(d)
    return r.text.strip() or f"HTTP {r.status_code}"


def _ref(ref: str) -> str:
    return quote(ref, safe=":")


class ApiClient:
    def __init__(
        self, base_url: str, token: str | None, transport: httpx.BaseTransport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        headers = {"authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport or DEFAULT_TRANSPORT,
            timeout=30.0,
        )

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ApiClient":
        token: str | None = None
        if settings.api_token is not None:
            token = settings.api_token.get_secret_value().strip() or None
        if token is None:
            try:
                if settings.api_token_file.is_file():
                    token = settings.api_token_file.read_text().strip() or None
            except OSError:
                token = None
        return cls(settings.api_url, token)

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise ApiError(r.status_code, _detail(r))
        return r.json() if r.content else None

    # jobs
    def create_job(
        self,
        app: str,
        feature: str,
        *,
        title: str | None = None,
        profile: str | None = None,
        budget: Decimal | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"app": app, "feature": feature, "source": "cli"}
        if title is not None:
            body["title"] = title
        if profile is not None:
            body["profile"] = profile
        if budget is not None:
            body["budget_usd"] = str(budget)
        return self._call("POST", "/api/jobs", json=body)

    def list_jobs(self, app: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("app", app), ("stage", stage)) if v}
        return self._call("GET", "/api/jobs", params=params)

    def show_job(self, ref: str) -> dict[str, Any]:
        return self._call("GET", f"/api/jobs/{_ref(ref)}")

    def job_runs(self, ref: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/api/jobs/{_ref(ref)}/runs")

    def action(
        self,
        ref: str,
        action: str,
        note: str | None,
        head_sha: str | None = None,
        amount_usd: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"action": action, "note": note, "head_sha": head_sha}
        if amount_usd is not None:
            body = {"action": action, "note": note, "amount_usd": amount_usd}
        return self._call("POST", f"/api/jobs/{_ref(ref)}/actions", json=body)

    # apps, login
    def list_apps(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/apps")

    def login_code(self) -> str:
        return self._call("POST", "/api/login-code")["code"]

    # models
    def refresh_models(
        self,
        aliases: Sequence[str] | None,
        *,
        poll_s: float = 2.0,
        timeout_s: float = 180.0,
        sleep: Callable[[float], None] | None = None,
    ) -> list[dict[str, Any]]:
        rid = self._call(
            "POST", "/api/models/refresh", json={"aliases": list(aliases or []) or None}
        )["request_id"]
        waited = 0.0
        while True:
            st = self._call("GET", f"/api/models/refresh/{rid}")
            if st.get("done"):
                return st.get("models", [])
            if waited >= timeout_s:
                raise RefreshTimeout("refresh still running; see factory models list")
            (sleep or time.sleep)(poll_s)
            waited += poll_s

    def list_models(self, available: bool = False) -> list[dict[str, Any]]:
        return self._call("GET", "/api/models", params={"available": "true"} if available else {})

    # backlogs
    def list_backlogs(self, app: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/api/backlogs", params={"app": app} if app else {})

    def show_backlog(self, ref: str) -> dict[str, Any]:
        return self._call("GET", f"/api/backlogs/{_ref(ref)}")

    def backlog_action(self, ref: str, action: str) -> Any:
        return self._call("POST", f"/api/backlogs/{_ref(ref)}/actions", json={"action": action})

    def import_backlog(self, app: str, title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call(
            "POST", "/api/backlogs/import", json={"app": app, "title": title, "items": items}
        )

    # point-and-run
    def create_run(self, body: dict[str, Any]) -> str:
        return str(self._call("POST", "/api/runs", json=body)["request_id"])

    def get_run(self, request_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/runs/{quote(request_id, safe='')}")
