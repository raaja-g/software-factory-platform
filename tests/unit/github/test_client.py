import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from swfactory.github.client import (
    GitHubClient,
    LabelEvent,
    MutationQueue,
    PullRef,
    RateLimited,
)

API = "https://api.github.com"
R = "/repos/acme/web"
T1 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
SHA = "a" * 40


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []
        self.call_times: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class SpyQueue(MutationQueue):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.calls: list[str] = []

    async def run(self, key: str, call: Callable[[], Awaitable[Any]]) -> Any:
        self.calls.append(key)
        return await super().run(key, call)


class TokenAuth:
    async def token_for(self, repo: str, permissions: Any) -> str:
        return "tok"

    async def org_token(self, org: str, permissions: Any) -> str:
        return "tok"

    def app_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer jwt"}


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mock():
    with respx.mock(base_url=API, assert_all_called=False) as m:
        yield m


@pytest.fixture
def spy_queue(fake_clock: FakeClock) -> SpyQueue:
    return SpyQueue(min_interval_s=1.0, clock=fake_clock)


@pytest.fixture
def client(spy_queue: SpyQueue):
    return GitHubClient(TokenAuth(), "acme/web", spy_queue)


def pr_json(n: int = 5, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "number": n,
        "node_id": f"PR_n{n}",
        "html_url": f"https://github.com/acme/web/pull/{n}",
        "head": {"sha": SHA},
        "state": "open",
        "draft": True,
        "merged_at": None,
    }
    d.update(kw)
    return d


def body_of(route: respx.Route, i: int = -1) -> Any:
    return json.loads(route.calls[i].request.content)


async def test_mutations_are_serial_and_spaced(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=1.0, clock=fake_clock)

    async def ok() -> str:
        fake_clock.call_times.append(fake_clock.now())
        await asyncio.sleep(0)
        return "ok"

    await asyncio.gather(q.run("inst-7", ok), q.run("inst-7", ok))
    assert fake_clock.call_times == [0.0, 1.0]


async def test_other_keys_do_not_wait(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=1.0, clock=fake_clock)

    async def ok() -> str:
        fake_clock.call_times.append(fake_clock.now())
        return "ok"

    await asyncio.gather(q.run("a", ok), q.run("b", ok))
    assert fake_clock.call_times == [0.0, 0.0]


async def _flaky(headers: dict[str, str], q: MutationQueue) -> str:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited(403, headers)
        return "ok"

    return await q.run("inst-7", flaky)


async def test_retry_after_is_honoured(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=1.0, clock=fake_clock)
    assert await _flaky({"retry-after": "7"}, q) == "ok"
    assert fake_clock.slept == [7.0]


async def test_ratelimit_reset_is_honoured(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=1.0, clock=fake_clock)
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(fake_clock.now() + 30)}
    assert await _flaky(headers, q) == "ok"
    assert fake_clock.slept == [30.0]


async def test_backoff_doubles_and_gives_up_after_three_tries(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=0, clock=fake_clock)
    n = 0

    async def always() -> str:
        nonlocal n
        n += 1
        raise RateLimited(429, {})

    with pytest.raises(RateLimited):
        await q.run("k", always)
    assert n == 3 and fake_clock.slept == [60.0, 120.0]


async def test_a_waiting_key_blocks_reads_too(fake_clock: FakeClock) -> None:
    q = MutationQueue(min_interval_s=1.0, clock=fake_clock)
    await _flaky({"retry-after": "7"}, q)
    fake_clock.slept.clear()
    fake_clock.t = 0.0  # still inside the block window recorded earlier? it was cleared by sleeping
    q._blocked_until["inst-7"] = 5.0

    async def read() -> str:
        return "r"

    assert await q.read("inst-7", read) == "r"
    assert fake_clock.slept == [5.0]


async def test_list_issues_304_is_not_modified(client: GitHubClient, mock) -> None:
    route = mock.get(f"{R}/issues").respond(304)
    assert await client.list_issues(label="factory", etag='"abc"') == ([], '"abc"', True)
    assert route.calls[0].request.headers["if-none-match"] == '"abc"'
    assert route.calls[0].request.url.params["labels"] == "factory"


async def test_list_issues_skips_pull_requests(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/issues").respond(
        200, json=[{"number": 1}, {"number": 2, "pull_request": {}}], headers={"etag": '"e"'}
    )
    assert await client.list_issues(label="factory", etag=None) == ([{"number": 1}], '"e"', False)


async def test_reads_bypass_queue(client: GitHubClient, spy_queue: SpyQueue, mock) -> None:
    mock.get(f"{R}/pulls/1").respond(200, json=pr_json(1))
    mock.get(f"{R}/pulls").respond(200, json=[pr_json(1)])
    mock.get(f"{R}/git/ref/heads/main").respond(200, json={"object": {"sha": SHA}})
    await client.get_pr(1)
    assert (await client.find_pr("factory/1")) is not None
    assert await client.branch_sha("main") == SHA
    assert spy_queue.calls == []


async def test_reads_honour_retry_after(client: GitHubClient, fake_clock: FakeClock, mock) -> None:
    mock.get(f"{R}/pulls/1").mock(
        side_effect=[
            httpx.Response(403, headers={"retry-after": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json=pr_json(1)),
        ]
    )
    assert (await client.get_pr(1)).number == 1
    assert fake_clock.slept == [7.0]


async def test_plain_403_is_an_error_not_a_retry(client: GitHubClient, fake_clock, mock) -> None:
    from swfactory.github.client import GitHubError

    mock.get(f"{R}/pulls/1").respond(403, json={"message": "Resource not accessible"})
    with pytest.raises(GitHubError):
        await client.get_pr(1)
    assert fake_clock.slept == []


async def test_list_prs_304_is_not_modified(client: GitHubClient, mock) -> None:
    route = mock.get(f"{R}/pulls").respond(304)
    assert await client.list_prs(etag='"p1"') == ([], '"p1"', True)
    p = route.calls[0].request.url.params
    assert dict(p) == {
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": "100",
        "page": "1",
    }


async def test_list_issue_comments_since(client: GitHubClient, mock) -> None:
    route = mock.get(f"{R}/issues/comments").respond(
        200,
        json=[
            {
                "id": 9,
                "user": {"login": "ana", "type": "User"},
                "body": "/approve",
                "created_at": "2026-11-11T09:30:00Z",
                "updated_at": "2026-11-11T09:31:00Z",
                "issue_url": f"{API}{R}/issues/12",
            }
        ],
    )
    comments, _, nm = await client.list_issue_comments(since=T1, etag=None)
    assert route.calls[0].request.url.params["since"] == "2026-11-11T09:00:00Z"
    assert not nm and comments[0].issue_number == 12 and comments[0].login == "ana"


async def test_collaborator_permission_404_is_none(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/collaborators/mallory/permission").respond(404, json={})
    assert await client.collaborator_permission("mallory") == "none"


async def test_issue_label_event_is_the_latest_for_the_label(client: GitHubClient, mock) -> None:
    def ev(kind: str, label: str, who: str, at: str) -> dict[str, Any]:
        return {
            "event": kind,
            "label": {"name": label},
            "actor": {"login": who, "type": "User"},
            "created_at": at,
        }

    mock.get(f"{R}/issues/12/events", params={"page": "2"}).respond(
        200, json=[ev("labeled", "factory", "bob", "2026-11-11T10:00:00Z")]
    )
    mock.get(f"{R}/issues/12/events").respond(
        200,
        json=[
            ev("labeled", "factory", "ana", "2026-11-11T09:00:00Z"),
            ev("unlabeled", "factory", "ana", "2026-11-11T09:05:00Z"),
            ev("labeled", "ui", "ana", "2026-11-11T09:06:00Z"),
        ],
        headers={"link": f'<{API}{R}/issues/12/events?per_page=100&page=2>; rel="next"'},
    )
    assert await client.issue_label_event(12, "factory") == LabelEvent("bob", "User", T2)


async def test_branch_rules_request(client: GitHubClient, mock) -> None:
    rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
    mock.get(f"{R}/rules/branches/main").respond(200, json=rules)
    assert await client.branch_rules("main") == rules


async def test_close_pr_goes_through_the_queue(
    client: GitHubClient, spy_queue: SpyQueue, mock
) -> None:
    route = mock.patch(f"{R}/pulls/5").respond(200, json=pr_json(5, state="closed"))
    await client.close_pr(5)
    assert body_of(route) == {"state": "closed"} and len(spy_queue.calls) == 1


async def test_create_pr_request(client: GitHubClient, mock) -> None:
    route = mock.post(f"{R}/pulls").respond(201, json=pr_json(5))
    pr = await client.create_pr(head="factory/001-x", base="main", title="T", body="B")
    assert body_of(route) == {
        "head": "factory/001-x",
        "base": "main",
        "title": "T",
        "body": "B",
        "draft": True,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert pr == PullRef(
        5, "PR_n5", "https://github.com/acme/web/pull/5", SHA, "open", False, True, None
    )


async def test_merged_pr_has_merge_sha(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/pulls/5").respond(
        200,
        json=pr_json(
            5,
            state="closed",
            merged_at="2026-11-11T09:00:00Z",
            merge_commit_sha="m" * 40,
            updated_at="2026-11-11T09:00:00Z",
        ),
    )
    pr = await client.get_pr(5)
    assert pr.merged and pr.merge_sha == "m" * 40 and pr.state == "closed" and pr.updated_at == T1


async def test_merge_async_request(client: GitHubClient, mock) -> None:
    route = mock.put(f"{R}/pulls/5/merge-async").respond(202, json={"uuid": "u-1"})
    assert await client.merge_async(5, sha=SHA, method="squash") == "u-1"
    assert body_of(route) == {"sha": SHA, "merge_method": "squash", "merge_action": "default"}


def _pr5() -> PullRef:
    return PullRef(5, "PR_n5", "u", SHA, "open", False, True, None)


async def test_enable_auto_merge_graphql(client: GitHubClient, mock) -> None:
    route = mock.post("/graphql").respond(200, json={"data": {}})
    await client.enable_auto_merge(_pr5(), expected_head_oid=SHA, method="squash")
    assert body_of(route)["variables"] == {
        "pullRequestId": "PR_n5",
        "mergeMethod": "SQUASH",
        "expectedHeadOid": SHA,
    }
    assert "enablePullRequestAutoMerge" in body_of(route)["query"]


async def test_mark_ready_graphql(client: GitHubClient, mock) -> None:
    route = mock.post("/graphql").respond(200, json={"data": {}})
    await client.mark_ready(_pr5())
    assert body_of(route)["variables"] == {"pullRequestId": "PR_n5"}


async def test_graphql_errors_raise(client: GitHubClient, mock) -> None:
    from swfactory.github.client import GitHubError

    mock.post("/graphql").respond(200, json={"errors": [{"message": "nope"}]})
    with pytest.raises(GitHubError):
        await client.mark_ready(_pr5())


async def test_add_labels_creates_missing_on_422(client: GitHubClient, mock) -> None:
    mock.post(f"{R}/issues/5/labels").mock(
        side_effect=[httpx.Response(422, json={}), httpx.Response(200, json=[])]
    )
    mock.post(f"{R}/labels").respond(201, json={})
    await client.add_labels(5, ["factory"])
    sent = [f"{c.request.method} {c.request.url.path}" for r in mock.routes for c in r.calls]
    assert sorted(sent) == sorted(
        [
            "POST /repos/acme/web/issues/5/labels",
            "POST /repos/acme/web/labels",
            "POST /repos/acme/web/issues/5/labels",
        ]
    )


async def test_comment_once_searches_every_page(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/issues/5/comments", params={"page": "2"}).respond(
        200, json=[{"id": 2, "body": "x <!-- m -->"}]
    )
    mock.get(f"{R}/issues/5/comments").respond(
        200,
        json=[{"id": 1, "body": "hi"}],
        headers={"link": f'<{API}{R}/issues/5/comments?per_page=100&page=2>; rel="next"'},
    )
    post = mock.post(f"{R}/issues/5/comments").respond(201, json={})
    assert await client.comment_once(5, "x <!-- m -->", marker="<!-- m -->") is False
    assert not post.called


async def test_comment_once_posts_when_absent(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/issues/5/comments").respond(200, json=[])
    post = mock.post(f"{R}/issues/5/comments").respond(201, json={})
    assert await client.comment_once(5, "x <!-- m -->", marker="<!-- m -->") is True
    assert body_of(post) == {"body": "x <!-- m -->"}


async def test_bot_identity(client: GitHubClient, mock) -> None:
    app = mock.get("/app").respond(200, json={"slug": "fac"})
    mock.get("/users/fac[bot]").respond(200, json={"id": 42})
    assert await client.bot_identity() == ("fac[bot]", "42+fac[bot]@users.noreply.github.com")
    assert app.calls[0].request.headers["authorization"] == "Bearer jwt"


async def test_upsert_check_run_creates_then_patches(client: GitHubClient, mock) -> None:
    mock.get(f"{R}/commits/{SHA}/check-runs").mock(
        side_effect=[
            httpx.Response(200, json={"check_runs": []}),
            httpx.Response(200, json={"check_runs": [{"id": 77, "name": "factory/review"}]}),
        ]
    )
    post = mock.post(f"{R}/check-runs").respond(201, json={"id": 77})
    patch = mock.patch(f"{R}/check-runs/77").respond(200, json={"id": 77})
    kw = dict(
        head_sha=SHA,
        name="factory/review",
        conclusion="success",
        title="t",
        summary="s",
        details_url="https://d",
    )
    assert await client.upsert_check_run(**kw) == 77
    assert await client.upsert_check_run(**kw) == 77
    assert post.call_count == 1 and patch.call_count == 1
    assert body_of(post)["output"] == {"title": "t", "summary": "s"}
