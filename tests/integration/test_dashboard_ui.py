"""Dashboard v2 in a headless browser: create_app serves web/, /api/** comes from fixtures."""

from __future__ import annotations

import base64
import fnmatch
import json
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, Route, expect
from tests.unit.api.api_support import settings

from swfactory.api.app import create_app

pytestmark = pytest.mark.ui

FIXTURE_HEAD = "a" * 40
FIXTURE_SHA = "0123456789abcdef0123456789abcdef01234567"
AVAILABLE_ALIASES = ["opus", "sonnet"]
LIGHT_BG = "rgb(229, 233, 234)"
DARK_BG = "rgb(15, 19, 21)"
XSS = "<img src=x onerror=alert(1)>"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

SPEC_MD = f"""# Spec

Some **bold** text {XSS}

[a](javascript:alert(1)) [b](data:text/html,x) [c](JaVaScRiPt:x) [d](https://example.com)
"""


def _jid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _job(n: int, slug: str, app: str, stage: str, column: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"{n:03d}-{slug}",
        "job_id": _jid(n),
        "ref": f"{app}:{n:03d}",
        "app": app,
        "title": slug.replace("-", " "),
        "branch": f"factory/{n:03d}-{slug}",
        "stage": stage,
        "column": column,
        "round": 1,
        "reviews": {},
        "history": [],
        "decision": None,
        "files": ["spec.md"],
        "updated": "2026-01-01T00:00:00+00:00",
        "gate_reason": None,
        "retry_offered": False,
        "head_sha": None,
        "stuck": False,
        "cost_usd": "0.500000",
        "budget_usd": "5.00",
        "models": {},
        "fallback": False,
        "merge_policy": "local_merge",
        "pr_url": None,
        "kind": "normal",
        "backlog_ref": None,
        "backlog_pos": None,
    }
    return base | kw


JOBS = [
    _job(1, "queued-one", "shop", "queued", "queued", files=[]),
    _job(2, "triage-one", "shop", "triage", "triage", files=[]),
    _job(
        3,
        "todo-web",
        "todo",
        "verify",
        "build",
        cost_usd="2.100000",
        fallback=True,
        files=["spec.md", "triage.md"],
        models={
            "builder": {"alias": "opus", "engine": "opencode", "mode": "fixed", "fallback": True},
            "spec_writer": {
                "alias": "sonnet",
                "engine": "claude_code",
                "mode": "fixed",
                "fallback": False,
            },
        },
    ),  # fmt: skip
    _job(4, "gallery", "shop", "review", "review", files=["spec.md", "screenshots/home.png"]),
    _job(
        5,
        "gate",
        "todo",
        "needs_human",
        "needs_human",
        head_sha=FIXTURE_HEAD,
        files=["spec.md"],
        gate_reason="escalated",
    ),  # fmt: skip
    _job(6, "failed-one", "todo", "failed", "needs_human", retry_offered=True, files=[]),
    _job(7, "stuck", "shop", "build", "build", stuck=True, files=[]),
    _job(8, "xss", "shop", "build", "build", title=XSS, files=["logs/build.log"]),
    _job(
        9,
        "done-one",
        "todo",
        "merged",
        "done",
        files=[],
        quality={"badge": "good", "judges_passed": 1, "judges_total": 2},
    ),
    _job(10, "budget-stop", "todo", "needs_human", "needs_human", gate_reason="budget", files=[]),
    _job(
        11,
        "delivered-one",
        "todo",
        "delivered",
        "done",
        files=[],
        pr_url="https://github.com/acme/web/pull/1",
        merge_policy="pr_manual",
    ),  # fmt: skip
]

BENCH_RUN_ID = "00000000-0000-0000-0000-0000000000a1"
ESTIMATE_FIXTURE = {
    "suite": "v1-parity", "configs": ["gemini", "claude", "economy"], "repetitions": 1,
    "trials": 12, "estimate_usd": "57.48", "max_usd": "120.48", "budget_usd": "40",
    "per_config": {}, "basis": {},
}  # fmt: skip


def _cfg(name: str, rate: float, iv: list[float], cost: str) -> dict[str, Any]:
    return {
        "name": name, "trials": 4, "completed": 4, "not_run": 0, "hidden_pass_rate": rate,
        "judge_pass_rate": None, "judge_pass_rate_uncalibrated": None, "quality": rate,
        "mean_rounds": 2.0, "total_cost_usd": "4.8", "mean_cost_usd": cost,
        "median_duration_s": 60.0, "hidden_pass_interval": iv, "task_pass": {},
    }  # fmt: skip


BENCH_REPORT = {
    "id": BENCH_RUN_ID, "run_id": BENCH_RUN_ID, "suite": "v1-parity", "definition_sha": "a" * 40,
    "status": "done", "budget_usd": "40", "cost_usd": "20", "recommended": "gemini",
    "recommendation_reason": "gemini: quality 1.00 at $1.20 per task (claude: 1.00 at $2.40)",
    "lowest_cost": "economy", "best_quality": "gemini", "pareto": ["economy", "gemini"],
    "configs": [
        _cfg("claude", 1.0, [0.51, 1.0], "2.40"), _cfg("economy", 0.75, [0.30, 0.95], "0.50"),
        _cfg("gemini", 1.0, [0.51, 1.0], "1.20"),
    ],
    "chart": [
        {"config": "claude", "cost_usd": "2.400000", "quality": 1.0, "pareto": False},
        {"config": "economy", "cost_usd": "0.500000", "quality": 0.75, "pareto": True},
        {"config": "gemini", "cost_usd": "1.200000", "quality": 1.0, "pareto": True},
    ],
    "common_tasks": [], "invalid_tasks": [], "invalid_reasons": {},
}  # fmt: skip
BENCH_SUITES = [
    {"name": "v1-parity", "description": "", "tasks": 4,
     "configurations": ["gemini", "claude", "economy"], "budget_usd": "40"},
]  # fmt: skip
BENCH_RUNS = [
    {"id": BENCH_RUN_ID, "suite": "v1-parity", "status": "done", "cost_usd": "20",
     "budget_usd": "40"},
]  # fmt: skip

METRICS = {
    "available": True, "window_days": 30, "jobs": 4, "escalation_rate": 0.25,
    "cost_per_merged_usd": "3.000000", "fallback_rate": 0.1,
    "rounds": {"mean": 1.5, "histogram": [{"rounds": 1, "jobs": 2}, {"rounds": 2, "jobs": 2}]},
    "pass_rate": {
        "role": [{"key": "builder", "n": 4, "pass_rate": 0.75}],
        "model": [{"key": "opus", "n": 4, "pass_rate": 0.75}],
        "engine": [{"key": "opencode", "n": 4, "pass_rate": 0.75}],
        "profile": [{"key": "fast", "n": 4, "pass_rate": 0.75}],
    },
    "scorers": [
        {"scorer": "merge-readiness", "judge_model": "gemini-lite@vertex", "n": 3,
         "pass_rate": 0.5, "calibrated": False, "labels": 3, "kappa": None}
    ],
}  # fmt: skip

FILES: dict[str, dict[str, str]] = {
    _jid(3): {"spec.md": SPEC_MD, "triage.md": "# Triage\nsmall change"},
    _jid(4): {"spec.md": "# Gallery"},
    _jid(5): {"spec.md": "# Gate"},
    _jid(8): {"logs/build.log": f"line {XSS}\n"},
}

RUNS = [
    {
        "id": "r-1", "role": "builder", "round": 1, "attempt": 1, "status": "succeeded",
        "verdict": None, "engine": "opencode", "engine_version": "1.4.2",
        "requested_model": "opus", "used_models": ["sonnet"], "selection_mode": "fixed",
        "model_alias": "opus", "input_tokens": 1200, "output_tokens": 340,
        "cache_read_tokens": 0, "cost_usd": "1.250000", "cost_source": "gateway",
        "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:02:05+00:00",
    }
]  # fmt: skip

EVENTS = {
    _jid(8): [
        {"id": 1, "at": "2026-01-01T00:00:00+00:00", "kind": "job.note", "data": {"note": XSS}}
    ],
    _jid(3): [
        {"id": 2, "at": "2026-01-01T00:00:00+00:00", "kind": "stage.entered", "data": {"s": "x"}}
    ],
}

MODELS = [
    {
        "alias": a, "provider": "anthropic", "upstream_id": a, "strength": "strong",
        "price_in_per_mtok": "3", "price_out_per_mtok": "15", "context_window": 200000,
        "supports_tools": True, "messages_tools_ok": True, "available": ok, "error_class": err,
        "latency_ms": 900, "last_error": None,
    }
    for a, ok, err in (("opus", True, None), ("sonnet", True, None), ("haiku", False, "quota"))
]  # fmt: skip

PROFILES = [
    {
        "name": "default", "description": "d", "continuity": "session", "unavailable": [],
        "roles": {
            "builder": {"engine": "opencode", "model": "opus"},
            "spec_writer": {"engine": "claude_code", "model": "sonnet"},
        },
    }
]  # fmt: skip

APPS = [
    {"slug": "shop", "repo_kind": "folder", "default_branch": "main", "profile": "default",
     "merge_policy": "local_merge", "max_rounds": 3, "budget_usd": "5", "counts": {},
     "cost_today_usd": "0"},
    {"slug": "todo", "repo_kind": "folder", "default_branch": "main", "profile": "default",
     "merge_policy": "local_merge", "max_rounds": 3, "budget_usd": "5", "counts": {},
     "cost_today_usd": "0"},
]  # fmt: skip

DEFINITION = {
    "active_sha": FIXTURE_SHA,
    "source": "git",
    "loaded_at": "2026-01-01T00:00:00+00:00",
    "last_rejected": {"sha": "f" * 40, "errors": ["apps.todo.profile: unknown profile 'x'"]},
}


class Rec:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []
        self.sse: list[str] = []
        self.labelled: set[str] = set()


_RECS: dict[int, Rec] = {}


def rec(page: Page) -> Rec:
    return _RECS[id(page)]


def _json(route: Route, body: Any, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def install(page: Page, role: str, sse_status: int = 200) -> Rec:
    r = Rec()
    _RECS[id(page)] = r

    def handle(route: Route) -> None:
        req = route.request
        url = urlparse(req.url)
        path = unquote(url.path)
        q = parse_qs(url.query)
        if path == "/api/events":
            r.sse.append(req.url)
            if sse_status != 200:
                route.fulfill(status=sse_status, body="no")
            else:
                route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"}, body=": ok\n\n"
                )
            return
        if req.method == "POST":
            try:
                body = req.post_data_json
            except Exception:
                body = None
            r.posts.append((path, body))
            if path == "/api/jobs":
                return _json(route, {"job_id": _jid(10), "ref": "todo:010"}, 201)
            if path == "/api/runs":
                return _json(route, {"request_id": "r1"}, 202)
            if path == "/api/models/refresh":
                return _json(route, {"request_id": 7}, 202)
            if path == "/api/definition/sync":
                return _json(route, {"active_sha": FIXTURE_SHA, "apps": {}})
            if path == "/api/bench/estimate":
                r.posts.append((path, req.post_data_json))
                return _json(route, ESTIMATE_FIXTURE)
            return _json(route, {"command_id": "c1"}, 202)
        r.gets.append(path)
        if path == "/api/me":
            return _json(route, {"email": "t@x.io", "role": role, "mode": "personal", "via": "t"})
        if path == "/api/board":
            app = (q.get("app") or [None])[0]
            jobs = [j for j in JOBS if app is None or j["app"] == app]
            return _json(
                route, {"jobs": jobs, "apps": ["shop", "todo"], "last_event_id": 42,
                        "generated_at": "2026-01-01T00:00:00+00:00"},
            )  # fmt: skip
        if path == "/api/runs/r1":
            return _json(
                route,
                {"status": "started", "backlog": "todo-app:3f2a9c1e", "job_ref": "todo-app:001"},
            )
        if path == "/api/models":
            return _json(route, MODELS)
        if path.startswith("/api/models/refresh/"):
            return _json(route, {"done": False})
        if path == "/api/profiles":
            return _json(route, PROFILES)
        if path == "/api/apps":
            return _json(route, APPS)
        if path == "/api/definition":
            return _json(route, DEFINITION)
        if path == "/api/bench/suites":
            return _json(route, BENCH_SUITES)
        if path == "/api/bench/runs":
            return _json(route, BENCH_RUNS)
        if path == f"/api/bench/runs/{BENCH_RUN_ID}":
            return _json(route, BENCH_REPORT)
        if path == "/api/metrics":
            return _json(route, METRICS)
        if path == "/api/jobs/todo:003/scores/merge-readiness/label" and req.method == "PUT":
            r.posts.append((path, req.post_data_json))
            r.labelled.add("merge-readiness")
            return _json(route, {"stored": True})
        if path == "/api/jobs/todo:003/judges":
            shown = "merge-readiness" in r.labelled
            row = {
                "scorer": "merge-readiness", "judge_model": "gemini-lite@vertex",
                "label": "B" if shown else None,
                "reason": "needs a test for empty input" if shown else None,
                "hidden": not shown, "your_label": "A" if shown else None,
                "labels": ["A", "B", "C"],
                "calibration": f"uncalibrated ({int(shown)} of 30 labels)",
            }  # fmt: skip
            return _json(route, {"scores": [row]})
        parts = path.split("/")  # "", api, jobs, ref, tail...
        if len(parts) >= 5 and parts[2] == "jobs":
            job = next((j for j in JOBS if j["ref"] == parts[3]), None)
            if job is None:
                return _json(route, {"detail": "no"}, 404)
            tail = "/".join(parts[4:])
            if tail == "runs":
                return _json(route, RUNS if job["id"].startswith("003") else [])
            if tail == "events":
                return _json(route, EVENTS.get(job["job_id"], []))
            if tail.startswith("artifacts/"):
                name = tail[len("artifacts/") :]
                if name.endswith(".png"):
                    return route.fulfill(status=200, content_type="image/png", body=PNG)
                text = FILES.get(job["job_id"], {}).get(name)
                if text is None:
                    return _json(route, {"detail": "no such artifact"}, 404)
                return route.fulfill(status=200, content_type="text/plain", body=text)
        return _json(route, {"detail": "unmocked " + path}, 404)

    page.route("**/api/**", handle)
    return r


def _static_deps() -> Any:
    from swfactory.api.app import ApiDeps

    n: Any = SimpleNamespace()
    return ApiDeps(
        sessions=n, events=n, artifacts=n, catalog=n, definitions=n, users=n, writer=None,
        settings=settings(),
    )  # fmt: skip


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    app = create_app(settings(), _static_deps(), on_gce=lambda: False)  # pyright: ignore[reportArgumentType]
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def _make(
    browser: Browser, base_url: str, role: str, *, scheme: str = "light", sse: int = 200,
    clock: bool = False,
) -> Iterator[Page]:  # fmt: skip
    ctx = browser.new_context(base_url=base_url, color_scheme=scheme)  # pyright: ignore[reportArgumentType]
    p = ctx.new_page()
    p.on("console", lambda m: _CONSOLE.setdefault(id(p), []).append(m.text))
    install(p, role, sse)
    if clock:
        p.clock.install()
    p.goto("/")
    expect(p.locator(".station-head h2").first).to_be_visible()
    yield p
    ctx.close()


_CONSOLE: dict[int, list[str]] = {}


def console_messages(page: Page) -> list[str]:
    return _CONSOLE.get(id(page), [])


@pytest.fixture
def page(browser: Browser, base_url: str) -> Iterator[Page]:  # pyright: ignore[reportIncompatibleVariableOverride]
    yield from _make(browser, base_url, "admin")


@pytest.fixture
def page_viewer(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _make(browser, base_url, "viewer")


@pytest.fixture
def page_approver(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _make(browser, base_url, "approver")


@pytest.fixture
def page_dark(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _make(browser, base_url, "admin", scheme="dark")


@pytest.fixture
def page_light(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _make(browser, base_url, "admin", scheme="light")


@pytest.fixture
def page_sse_500(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _make(browser, base_url, "admin", sse=500, clock=True)


# ---- helpers -------------------------------------------------------------------------------


def eventually(
    page: Page, fn: Callable[[], Any], want: Callable[[Any], bool], seconds: float = 4.0
) -> Any:
    end = time.monotonic() + seconds
    while True:
        v = fn()
        if want(v) or time.monotonic() > end:
            return v
        page.wait_for_timeout(50)  # pumps Playwright so the route handlers run


def goto(page: Page, route: str) -> None:
    page.evaluate("h => { location.hash = h }", route)
    page.wait_for_timeout(50)


def card_of(page: Page, ident: str):  # noqa: ANN201
    return page.locator(f".card[data-id^='{ident}']").first


def open_card(page: Page, ident: str) -> None:
    card_of(page, ident).click()
    expect(page.locator("dialog[open]")).to_be_visible()


def tabs(page: Page) -> list[str]:
    return page.locator("dialog[open] [role=tab]").all_inner_texts()


def click_tab(page: Page, name: str) -> None:
    page.locator("dialog[open]").get_by_role("tab", name=name).click()


def open_file(page: Page, path: str, card: str = "003") -> None:
    if page.locator("dialog[open]").count() == 0:
        open_card(page, card)
    click_tab(page, "Files")
    page.locator("dialog[open]").get_by_role("button", name=path, exact=True).click()
    page.wait_for_timeout(100)


def chip(card, role: str):  # noqa: ANN001, ANN201
    return card.locator(f".model-chip[data-role='{role}']")


def headers(page: Page, sel: str) -> list[str]:
    page.locator(f"{sel} th").first.wait_for()
    return page.locator(f"{sel} th").all_inner_texts()


def row(page: Page, sel: str, i: int):  # noqa: ANN201
    return page.locator(f"{sel} tbody tr").nth(i)


def all_cards_app(page: Page) -> set[str]:
    page.wait_for_load_state("networkidle")
    return set(page.locator(".card .app-tag").all_inner_texts())


def last_sse_url(page: Page) -> str:
    page.wait_for_load_state("networkidle")
    return rec(page).sse[-1]


def posted_all(page: Page, pattern: str) -> list[Any]:
    def find() -> list[Any]:
        return [b for p, b in rec(page).posts if fnmatch.fnmatch(p, pattern)]

    return eventually(page, find, bool)


def posted(page: Page, pattern: str) -> Any:
    return posted_all(page, pattern)[-1]


def posted_count(page: Page, pattern: str) -> int:
    posted_all(page, pattern)
    page.wait_for_timeout(150)
    return len(posted_all(page, pattern))


def board_fetches(page: Page) -> int:
    return len([g for g in rec(page).gets if g == "/api/board"])


def options(page: Page, label: str) -> list[str]:
    return page.get_by_label(label).locator("option").all_inner_texts()


def bg(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


def tab_to(page: Page, selector: str) -> None:
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate("s => document.activeElement.matches(s)", selector):
            return
    raise AssertionError(f"could not tab to {selector}")


# ---- tests ---------------------------------------------------------------------------------


def test_columns(page):
    assert page.locator(".station-head h2").all_inner_texts() == [
        "Queued", "Triage", "Spec", "Build", "Review", "Approve", "Needs human", "Done",
    ]  # fmt: skip


def test_card_cost_models_fallback(page):
    card = card_of(page, "003-todo-web")
    expect(card).to_contain_text("$2.10")
    expect(card.get_by_label("Model fallback detected")).to_be_visible()
    assert card.get_by_label("Model fallback detected").count() == 1


def test_model_chip_hover_shows_engine_and_mode(page):
    assert chip(card_of(page, "003-todo-web"), "builder").get_attribute("title") == (
        "opencode · fixed"
    )


def test_stuck_badge(page):
    expect(card_of(page, "007-stuck")).to_contain_text("No progress for 30 min")


def test_app_filter(page):
    page.get_by_label("App").select_option("todo")
    assert all_cards_app(page) == {"todo"} and "app=todo" in last_sse_url(page)


def test_drawer_tabs_and_screenshot(page):
    open_card(page, "004")
    assert tabs(page) == ["Files", "Runs", "Timeline", "Triage", "Scores"]
    expect(page.locator("img[src$='screenshots/home.png']")).to_be_visible()


def test_runs_tab_columns(page):
    open_card(page, "003")
    click_tab(page, "Runs")
    assert headers(page, ".runs") == [
        "Role", "Round", "Engine", "Model (requested → used)", "Mode", "Tokens", "Cost",
        "Duration", "Status",
    ]  # fmt: skip
    expect(row(page, ".runs", 0)).to_contain_text("opencode 1.4.2")


def test_viewer_sees_no_action_buttons(page_viewer):
    open_card(page_viewer, "005")
    assert page_viewer.get_by_role("button", name="Send back for rework").count() == 0


def test_retry_button_follows_retry_offered(page_approver):  # fixture retry_offered=false
    open_card(page_approver, "005")
    assert page_approver.get_by_role("button", name="Retry step").count() == 0


def test_failed_card_offers_retry_job_only(page_approver):
    open_card(page_approver, "006")
    assert page_approver.get_by_role("button", name="Retry job").count() == 1
    assert page_approver.get_by_role("button", name="Send back for rework").count() == 0


def test_rework_posts_note(page_approver):
    open_card(page_approver, "005")
    page_approver.get_by_label("Rework note").fill("Add a socket timeout")
    page_approver.get_by_role("button", name="Send back for rework").click()
    assert posted(page_approver, "/api/jobs/*/actions") == {
        "action": "rework",
        "note": "Add a socket timeout",
    }


def test_approve_sends_shown_head_sha(page_approver):
    open_card(page_approver, "005")
    page_approver.get_by_role("button", name="Approve and merge").click()
    assert posted(page_approver, "/api/jobs/*/actions") == {
        "action": "approve",
        "note": None,
        "head_sha": FIXTURE_HEAD,
    }


def test_new_job_form_posts(page):
    goto(page, "#/new")
    page.get_by_label("App").select_option("todo")
    page.get_by_label("Feature").fill("Add x")
    page.get_by_role("button", name="Create job").click()
    assert posted(page, "/api/jobs") == {"app": "todo", "feature": "Add x", "source": "dashboard"}


def test_new_run_form_posts_text(page):
    goto(page, "#/run")
    page.get_by_label("Repo").fill("~/work/todo-app")
    page.get_by_label("Requirement", exact=True).fill("A todo list")
    page.get_by_role("button", name="Start run").click()
    assert posted(page, "/api/runs") == {
        "repo": "~/work/todo-app",
        "requirement": "A todo list",
        "ci_safe": False,
    }


def test_new_run_form_sends_an_uploaded_file_as_text(page):
    goto(page, "#/run")
    page.get_by_label("Repo").fill("acme/web")
    page.get_by_label("Requirement file").set_input_files(
        {"name": "req.md", "mimeType": "text/markdown", "buffer": b"# Todo\nA list"}
    )
    page.get_by_role("button", name="Start run").click()
    assert posted(page, "/api/runs")["requirement"] == "# Todo\nA list"


def test_new_run_shows_the_result(page):
    goto(page, "#/run")
    page.get_by_label("Repo").fill("acme/web")
    page.get_by_label("Requirement", exact=True).fill("x")
    page.get_by_role("button", name="Start run").click()
    expect(page.locator(".run-status")).to_have_text("Started: requirements job todo-app:001")


def test_new_run_shows_a_failure_as_text(page):
    def failed(route: Route) -> None:
        _json(route, {"status": "failed", "detail": f"{XSS} is not a git repository"})

    page.route("**/api/runs/r1", failed)
    goto(page, "#/run")
    page.get_by_label("Repo").fill("/x")
    page.get_by_label("Requirement", exact=True).fill("x")
    page.get_by_role("button", name="Start run").click()
    expect(page.locator(".run-status")).to_contain_text(XSS)
    assert page.locator(".run-status img").count() == 0


def test_models_page_refresh(page):
    goto(page, "#/models")
    page.get_by_role("button", name="Refresh").click()
    assert posted_count(page, "/api/models/refresh") == 1
    expect(page.locator(".models-status")).to_have_text("Refresh requested")


def test_definition_page(page):
    goto(page, "#/definition")
    expect(page.locator(".active-sha")).to_have_text(FIXTURE_SHA[:12])
    expect(page.locator(".rejected")).to_contain_text("apps.todo.profile: unknown profile 'x'")
    page.get_by_role("button", name="Sync now").click()
    assert posted_count(page, "/api/definition/sync") == 1


def test_markdown_escapes_html(page):
    open_file(page, "spec.md")
    expect(page.locator(".md")).to_contain_text("Some bold text")
    assert page.locator(".md img").count() == 0


def test_markdown_links_only_http(page):
    open_file(page, "spec.md")
    expect(page.locator(".md a")).to_have_count(1)
    assert page.locator(".md a").evaluate_all("els => els.map(e => e.getAttribute('href'))") == [
        "https://example.com"
    ]
    assert page.locator(".md a").get_attribute("rel") == "noopener noreferrer"


def test_untrusted_fields_render_as_text(page):
    expect(card_of(page, "008-xss")).to_contain_text(XSS)
    open_card(page, "008-xss")
    click_tab(page, "Timeline")
    expect(page.locator(".timeline")).to_contain_text(XSS)
    open_file(page, "logs/build.log")
    expect(page.locator("dialog[open] pre")).to_contain_text(XSS)
    assert page.locator("img[src='x']").count() == 0


def test_no_csp_violations(page):
    open_card(page, "003-todo-web")
    page.keyboard.press("Escape")
    goto(page, "#/models")
    goto(page, "#/profiles")
    page.wait_for_timeout(300)
    assert [m for m in console_messages(page) if "Content Security Policy" in m] == []


def test_sse_failure_falls_back_to_polling(page_sse_500):
    page_sse_500.wait_for_selector("body[data-live='poll']")
    assert board_fetches(page_sse_500) == 1
    page_sse_500.clock.fast_forward(4000)
    assert eventually(page_sse_500, lambda: board_fetches(page_sse_500), lambda n: n >= 2) == 2


def test_profile_editor_lists_only_available_models(page):
    goto(page, "#/profiles")
    expect(page.get_by_label("builder model")).to_be_visible()
    assert options(page, "builder model") == AVAILABLE_ALIASES


def test_keyboard_drawer(page):
    tab_to(page, "[data-id='003-todo-web']")
    page.keyboard.press("Enter")
    expect(page.locator("dialog[open]")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert page.evaluate("document.activeElement.dataset.id") == "003-todo-web"


def test_dark_theme(page_dark, page_light):
    assert (bg(page_light), bg(page_dark)) == (LIGHT_BG, DARK_BG)


def test_signed_out_message(page):
    page.route("**/api/board", lambda r: r.fulfill(status=401, body="{}"))
    page.reload()
    expect(page.locator("#notice")).to_contain_text("Signed out")


def test_add_budget_on_budget_stop_for_admins(page, page_approver):
    open_card(page_approver, "010")
    assert page_approver.get_by_role("button", name="Add budget").count() == 0
    open_card(page, "005")
    assert page.get_by_role("button", name="Add budget").count() == 0
    page.keyboard.press("Escape")
    open_card(page, "010")
    page.get_by_label("Amount (USD)").fill("5")
    page.get_by_role("button", name="Add budget").click()
    assert posted(page, "/api/jobs/*/actions") == {
        "action": "add_budget",
        "note": None,
        "amount_usd": "5",
    }


def test_follow_up_button_only_on_delivered(page_approver):
    open_card(page_approver, "011")
    btn = page_approver.get_by_role("button", name="Send back as a follow-up")
    assert btn.is_visible()
    page_approver.get_by_label("Follow-up note").fill("rename X")
    btn.click()
    assert posted(page_approver, "/api/jobs/*/follow-up") == {"note": "rename X"}
    page_approver.keyboard.press("Escape")
    open_card(page_approver, "005")
    assert page_approver.get_by_role("button", name="Send back as a follow-up").count() == 0


def test_label_control_is_blind(page):
    open_card(page, "003")
    click_tab(page, "Scores")
    expect(page.get_by_text("needs a test for empty input")).to_have_count(0)
    expect(page.get_by_text("judge result hidden until you label")).to_be_visible()
    page.get_by_label("Your label for merge-readiness").select_option("A")
    expect(page.get_by_text("needs a test for empty input")).to_be_visible()
    expect(page.get_by_text("uncalibrated (1 of 30 labels)")).to_be_visible()
    assert ("/api/jobs/todo:003/scores/merge-readiness/label", {"label": "A"}) in rec(page).posts


@pytest.mark.ui
def test_metrics_page(page: Page) -> None:
    goto(page, "#/metrics")
    expect(page.get_by_role("heading", name="Pass rate by role")).to_be_visible()
    expect(page.get_by_text("Cost per merged job")).to_be_visible()
    expect(page.get_by_text("uncalibrated, not counted")).to_be_visible()


@pytest.mark.ui
def test_done_card_quality_badge(page: Page) -> None:
    badge = card_of(page, "009-done-one").get_by_text("Quality: good")
    expect(badge).to_be_visible()
    assert badge.get_attribute("title") == "1 of 2 judges passed"
    expect(card_of(page, "011-delivered-one").get_by_text("Quality:")).to_have_count(0)


def pick_suite(page: Page, name: str) -> None:
    page.get_by_label("Suite").select_option(name)


@pytest.mark.ui
def test_benchmarks_page(page: Page) -> None:
    goto(page, f"#/benchmarks/{BENCH_RUN_ID}")
    expect(page.locator("svg.bench-chart circle")).to_have_count(3)
    expect(page.get_by_text("Recommended: gemini")).to_be_visible()
    expect(page.get_by_text("0.75 (n=4, 0.30\u20130.95)")).to_be_visible()
    goto(page, "#/benchmarks")
    start = page.get_by_role("button", name="Start benchmark run")
    expect(start).to_be_disabled()
    pick_suite(page, "v1-parity")
    expect(page.get_by_text("Estimated $57.48 (up to $120.48)")).to_be_visible()
    expect(start).to_be_enabled()
