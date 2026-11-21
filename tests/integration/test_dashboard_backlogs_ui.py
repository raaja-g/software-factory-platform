"""Dashboard Backlogs view in a headless browser, with backlog and requirements fixtures."""

# ruff: noqa: F811

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlparse

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.integration import test_dashboard_ui as base
from tests.integration.test_dashboard_ui import (  # noqa: F401  (fixtures used by name)
    JOBS,
    base_url,
    card_of,
    install,
    rec,
)

pytestmark = pytest.mark.ui

REQ_MD = (
    "## Backlog\n\n1. Set up the project <script>alert(1)</script> <img src=x onerror=alert(1)>\n"
)


def _item(pos: int, jid: str, title: str, ref: str, stage: str, column: str) -> dict[str, Any]:
    return {
        "pos": pos, "id": jid, "title": title, "job_ref": ref, "stage": stage, "column": column,
        "cost_usd": "0.500000",
    }  # fmt: skip


def _row(
    n: int, title: str, status: str, merged: int, total: int, req: str | None
) -> dict[str, Any]:
    return {
        "backlog_id": f"00000000-0000-4000-8000-{n:012d}", "ref": f"todo:b{n:03d}", "app": "todo",
        "title": title, "status": status, "created_by": "t@x.io", "approved_by": None,
        "approved_at": None, "updated_at": "2026-01-01T00:00:00+00:00", "jobs_total": total,
        "jobs_merged": merged, "requirements_job_ref": req,
    }  # fmt: skip


BACKLOGS: dict[str, dict[str, Any]] = {
    "todo:b001": _row(1, "Todo app", "running", 1, 3, "todo:009")
    | {
        "blocked_job": None,
        "requirements_job": None,
        "items": [
            _item(1, "i1", "Set up the project", "todo:001", "merged", "done"),
            _item(2, "i2", "Add todos", "todo:002", "queued", "queued"),
            _item(3, "i3", "Mark done", "todo:003", "queued", "queued"),
        ],
    },
    "todo:b002": _row(2, "Shop app", "paused", 0, 2, "todo:008")
    | {
        "blocked_job": "todo:001",
        "requirements_job": None,
        "items": [
            _item(1, "i1", "Catalogue", "todo:001", "closed", "done"),
            _item(2, "i2", "Basket", "todo:002", "queued", "queued"),
        ],
    },
    "todo:b003": _row(3, "Notes app", "awaiting_approval", 0, 0, "todo:010")
    | {
        "blocked_job": None,
        "requirements_job": {
            "ref": "todo:010", "stage": "needs_human", "gate_reason": "approval",
            "retry_offered": False,
        },
        "items": [],
    },
}  # fmt: skip


def _mk(browser: Browser, base_url: str, role: str, status_409: bool = False) -> Iterator[Page]:
    ctx = browser.new_context(base_url=base_url)  # pyright: ignore[reportArgumentType]
    p = ctx.new_page()
    r = install(p, role)
    state: dict[str, Any] = {"paused": False}
    p._bl_state = state  # type: ignore[attr-defined]
    jobs = [j for j in JOBS if j["ref"] != "todo:010"] + [
        base._job(
            10, "requirements", "todo", "needs_human", "needs_human", kind="requirements",
            backlog_ref="todo:b003", gate_reason="approval",
        ),
    ]  # fmt: skip

    def handle(route: Route) -> None:
        req = route.request
        path = unquote(urlparse(req.url).path)
        if req.method == "POST":
            if status_409 and path.startswith("/api/backlogs/"):
                r.posts.append((path, req.post_data_json))
                body = {"detail": "job todo:001 is closed; skip it or cancel the backlog"}
                return route.fulfill(
                    status=409, content_type="application/json", body=json.dumps(body)
                )
            return route.fallback()
        if path == "/api/board":
            body = {"jobs": jobs, "apps": ["shop", "todo"], "last_event_id": 42,
                    "generated_at": "2026-01-01T00:00:00+00:00"}  # fmt: skip
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        if path == "/api/backlogs":
            rows = [
                {
                    k: v
                    for k, v in b.items()
                    if k not in ("items", "blocked_job", "requirements_job")
                }
                for b in BACKLOGS.values()
            ]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(rows))
        if path.startswith("/api/backlogs/"):
            ref = path.rsplit("/", 1)[-1]
            b = BACKLOGS.get(ref)
            if b is None:
                return route.fulfill(
                    status=404, content_type="application/json", body='{"detail":"no"}'
                )
            if ref == "todo:b001" and state["paused"]:
                b = b | {"status": "paused"}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(b))
        if path == "/api/jobs/todo:010/artifacts/requirements.md":
            return route.fulfill(status=200, content_type="text/plain", body=REQ_MD)
        return route.fallback()

    p.route("**/api/**", handle)
    p.goto("/")
    expect(p.locator(".station-head h2").first).to_be_visible()
    yield p
    ctx.close()


@pytest.fixture
def page(browser: Browser, base_url: str) -> Iterator[Page]:  # pyright: ignore[reportIncompatibleVariableOverride]
    yield from _mk(browser, base_url, "admin")


@pytest.fixture
def page_viewer(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _mk(browser, base_url, "viewer")


@pytest.fixture
def page_approver(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _mk(browser, base_url, "approver")


@pytest.fixture
def page_approver_409(browser: Browser, base_url: str) -> Iterator[Page]:
    yield from _mk(browser, base_url, "approver", status_409=True)


def posts(page: Page) -> list[tuple[str, Any]]:
    page.wait_for_timeout(150)
    return list(rec(page).posts)


def last_post(page: Page) -> tuple[str, Any]:
    base.eventually(page, lambda: rec(page).posts, bool)
    return rec(page).posts[-1]


def push_sse(page: Page, kind: str, data: dict[str, Any]) -> None:
    """Make the next event stream deliver one event, then reconnect the page to it."""
    if kind == "backlog.paused":
        page._bl_state["paused"] = True  # type: ignore[attr-defined]
    body = f"id: 99\nevent: {kind}\ndata: {json.dumps(data)}\n\n"
    page.route(
        "**/api/events*",
        lambda route: route.fulfill(
            status=200, headers={"content-type": "text/event-stream"}, body=body
        ),
    )
    page.evaluate("openSse()")


def test_backlogs_link_and_list(page):
    page.get_by_role("link", name="Backlogs").click()
    rows = page.locator(".backlogs tbody tr")
    expect(rows).to_have_count(3)
    assert "running" in rows.nth(0).inner_text() and "1 of 3 merged" in rows.nth(0).inner_text()


def test_detail_lists_items_in_order_with_stages(page):
    page.goto("#/backlog/todo:b001")
    items = page.locator(".backlog-items li")
    expect(items).to_have_count(3)
    assert [i.inner_text().split("\n")[0] for i in items.all()] == [
        "1  Set up the project", "2  Add todos", "3  Mark done",
    ]  # fmt: skip
    expect(items.nth(0)).to_contain_text("merged")
    expect(items.nth(1)).to_contain_text("queued")
    items.nth(1).get_by_role("link", name="todo:002").click()
    expect(page).to_have_url(re.compile(r"#/job/"))


def test_gate_offers_approve_rework_reject(page_approver):
    page_approver.goto("#/backlog/todo:b003")
    for name in ("Approve backlog", "Send back for rework", "Reject backlog"):
        expect(page_approver.get_by_role("button", name=name)).to_be_visible()


def test_approve_posts_to_the_requirements_job(page_approver):
    page_approver.goto("#/backlog/todo:b003")
    page_approver.get_by_role("button", name="Approve backlog").click()
    assert last_post(page_approver) == (
        "/api/jobs/todo:010/actions", {"action": "approve", "note": None, "head_sha": None},
    )  # fmt: skip


def test_rework_needs_a_note(page_approver):
    page_approver.goto("#/backlog/todo:b003")
    page_approver.get_by_role("button", name="Send back for rework").click()
    expect(page_approver.get_by_text("Say what to change")).to_be_visible()
    assert posts(page_approver) == []
    page_approver.get_by_label("Note").fill("Split sign-in out")
    page_approver.get_by_role("button", name="Send back for rework").click()
    assert last_post(page_approver)[1] == {
        "action": "rework", "note": "Split sign-in out", "head_sha": None,
    }  # fmt: skip


def test_the_requirements_document_is_shown_as_text(page_approver):
    page_approver.goto("#/backlog/todo:b003")
    expect(page_approver.locator(".requirements-md")).to_contain_text("## Backlog")
    assert page_approver.locator(".requirements-md script, .requirements-md img").count() == 0
    expect(page_approver.locator(".requirements-md")).to_contain_text("<script>alert(1)</script>")


def test_paused_backlog_names_the_blocker_and_offers_resume_skip_cancel(page_approver):
    page_approver.goto("#/backlog/todo:b002")
    expect(page_approver.locator(".blocked")).to_contain_text("todo:001 is closed")
    for name in ("Resume", "Skip blocked job", "Cancel backlog"):
        expect(page_approver.get_by_role("button", name=name)).to_be_visible()
    page_approver.get_by_role("button", name="Skip blocked job").click()
    assert last_post(page_approver) == ("/api/backlogs/todo:b002/actions", {"action": "skip"})


def test_cancel_asks_first(page_approver):
    page_approver.goto("#/backlog/todo:b002")
    page_approver.once("dialog", lambda d: d.dismiss())
    page_approver.get_by_role("button", name="Cancel backlog").click()
    assert posts(page_approver) == []


def test_cancel_confirmed_posts(page_approver):
    page_approver.goto("#/backlog/todo:b002")
    page_approver.once("dialog", lambda d: d.accept())
    page_approver.get_by_role("button", name="Cancel backlog").click()
    assert last_post(page_approver) == ("/api/backlogs/todo:b002/actions", {"action": "cancel"})


def test_viewer_sees_no_backlog_buttons(page_viewer):
    for ref in ("todo:b002", "todo:b003"):
        page_viewer.goto(f"#/backlog/{ref}")
        expect(page_viewer.locator(".backlog-status")).to_be_visible()
        # no backlog actions (the header's Sign out button is not one)
        buttons = page_viewer.get_by_role("button").filter(has_not_text="Sign out")
        assert buttons.count() == 0


def test_action_error_is_shown(page_approver_409):
    page_approver_409.goto("#/backlog/todo:b002")
    page_approver_409.get_by_role("button", name="Resume").click()
    expect(page_approver_409.get_by_role("alert")).to_contain_text("skip it or cancel the backlog")


def test_a_backlog_event_refreshes_the_page(page_approver):
    page_approver.goto("#/backlog/todo:b001")
    expect(page_approver.locator(".backlog-status")).to_have_text("running")
    push_sse(page_approver, "backlog.paused", {"ref": "todo:b001", "app": "todo"})
    expect(page_approver.locator(".backlog-status")).to_have_text("paused")


def test_requirements_card_has_a_tag(page):
    card = card_of(page, "010")
    expect(card).to_contain_text("Requirements")
    assert card_of(page, "003-todo-web").get_by_text("Requirements").count() == 0
