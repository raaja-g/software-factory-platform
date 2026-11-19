"""Every scenario runs against GitHubClient (respx-backed in-memory server) and FakeGitHub."""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from tests.fakes.github import FakeGitHub

from swfactory.github.client import GitHubClient, MutationQueue

API = "https://api.github.com"
SHA = "a" * 40


class _Clock:
    def now(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


class _Auth:
    async def token_for(self, repo: str, permissions: Any) -> str:
        return "tok"

    async def org_token(self, org: str, permissions: Any) -> str:
        return "tok"


class Impl:
    """A GitHubClient or FakeGitHub plus test-side seeding/inspection that is identical in both."""

    api: Any

    def seed_branch(self, branch: str, sha: str) -> None: ...
    def seed_permission(self, login: str, perm: str) -> None: ...
    def labels(self, number: int) -> set[str]: ...
    def comment_bodies(self, number: int) -> list[str]: ...


class FakeImpl(Impl):
    def __init__(self) -> None:
        self.fake = FakeGitHub("acme/web")
        self.api = self.fake

    def seed_branch(self, branch: str, sha: str) -> None:
        self.fake.branches[branch] = sha

    def seed_permission(self, login: str, perm: str) -> None:
        self.fake.permissions[login] = perm

    def labels(self, number: int) -> set[str]:
        return set(self.fake.labels.get(number, set()))

    def comment_bodies(self, number: int) -> list[str]:
        return [c.body for c in self.fake.comments.get(number, [])]


class ClientImpl(Impl):
    """A tiny stateful GitHub REST server behind respx."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.prs: dict[int, dict[str, Any]] = {}
        self.branches: dict[str, str] = {}
        self.perms: dict[str, str] = {}
        self._labels: dict[int, set[str]] = {}
        self.known_labels: set[str] = set()
        self.comments: dict[int, list[str]] = {}
        self.checks: dict[tuple[str, str], dict[str, Any]] = {}
        self.merged: set[int] = set()
        self.api = GitHubClient(_Auth(), "acme/web", MutationQueue(0, _Clock()))
        R = r"/repos/acme/web"
        mock.route(method="POST", path=f"{R}/pulls").mock(side_effect=self._create_pr)
        mock.route(method="GET", path=f"{R}/pulls").mock(side_effect=self._list_prs)
        mock.route(method="GET", path__regex=rf"{R}/pulls/(\d+)$").mock(side_effect=self._get_pr)
        mock.route(method="PUT", path__regex=rf"{R}/pulls/(\d+)/merge-async$").mock(
            side_effect=self._merge
        )
        mock.route(method="POST", path="/graphql").mock(side_effect=self._graphql)
        mock.route(method="POST", path__regex=rf"{R}/issues/(\d+)/labels$").mock(
            side_effect=self._add_labels
        )
        mock.route(method="POST", path=f"{R}/labels").mock(side_effect=self._new_label)
        mock.route(method="DELETE", path__regex=rf"{R}/issues/(\d+)/labels/(.+)$").mock(
            side_effect=self._rm_label
        )
        mock.route(method="GET", path__regex=rf"{R}/issues/(\d+)/comments$").mock(
            side_effect=self._list_comments
        )
        mock.route(method="POST", path__regex=rf"{R}/issues/(\d+)/comments$").mock(
            side_effect=self._post_comment
        )
        mock.route(method="GET", path__regex=rf"{R}/commits/(\w+)/check-runs$").mock(
            side_effect=self._find_check
        )
        mock.route(method="POST", path=f"{R}/check-runs").mock(side_effect=self._create_check)
        mock.route(method="PATCH", path__regex=rf"{R}/check-runs/(\d+)$").mock(
            side_effect=self._patch_check
        )
        mock.route(method="GET", path__regex=rf"{R}/collaborators/(.+)/permission$").mock(
            side_effect=self._perm
        )
        mock.route(method="GET", path__regex=rf"{R}/git/ref/heads/(.+)$").mock(
            side_effect=self._ref
        )

    # seeding
    def seed_branch(self, branch: str, sha: str) -> None:
        self.branches[branch] = sha

    def seed_permission(self, login: str, perm: str) -> None:
        self.perms[login] = perm

    def labels(self, number: int) -> set[str]:
        return set(self._labels.get(number, set()))

    def comment_bodies(self, number: int) -> list[str]:
        return list(self.comments.get(number, []))

    # handlers
    @staticmethod
    def _n(req: httpx.Request, group: int = 1) -> str:
        pattern = r"/(\d+)(?:/|$)" if group == 1 else r"/(\d+)$"
        m = re.search(pattern, req.url.path)
        assert m
        return m.group(1)

    def _json(self, n: int) -> dict[str, Any]:
        d = self.prs[n]
        return {
            "number": n,
            "node_id": f"PR_n{n}",
            "html_url": f"https://github.com/acme/web/pull/{n}",
            "head": {"sha": d["head_sha"]},
            "state": "closed" if n in self.merged or d["closed"] else "open",
            "draft": d["draft"],
            "merged_at": "2026-11-11T09:00:00Z" if n in self.merged else None,
            "merge_commit_sha": "m" * 40 if n in self.merged else None,
        }

    def _create_pr(self, req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        n = len(self.prs) + 1
        self.prs[n] = {
            "head": b["head"],
            "head_sha": self.branches.get(b["head"], SHA),
            "draft": b["draft"],
            "closed": False,
        }
        return httpx.Response(201, json=self._json(n))

    def _list_prs(self, req: httpx.Request) -> httpx.Response:
        head = req.url.params.get("head", "").split(":", 1)[-1]
        return httpx.Response(
            200, json=[self._json(n) for n, d in self.prs.items() if d["head"] == head]
        )

    def _get_pr(self, req: httpx.Request) -> httpx.Response:
        n = int(self._n(req))
        if n not in self.prs:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=self._json(n))

    def _merge(self, req: httpx.Request) -> httpx.Response:
        n = int(self._n(req))
        if json.loads(req.content)["sha"] != self.prs[n]["head_sha"]:
            return httpx.Response(409, json={"message": "head branch was modified"})
        self.merged.add(n)
        return httpx.Response(202, json={"uuid": "u-1"})

    def _graphql(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        v = body["variables"]
        n = int(str(v["pullRequestId"]).removeprefix("PR_n"))
        if "markPullRequestReadyForReview" in body["query"]:
            self.prs[n]["draft"] = False
        else:
            self.prs[n]["auto_merge"] = (v["expectedHeadOid"], v["mergeMethod"])
        return httpx.Response(200, json={"data": {}})

    def _add_labels(self, req: httpx.Request) -> httpx.Response:
        n = int(self._n(req))
        names = set(json.loads(req.content)["labels"])
        if not names <= self.known_labels:
            return httpx.Response(422, json={"message": "label does not exist"})
        self._labels.setdefault(n, set()).update(names)
        return httpx.Response(200, json=[])

    def _new_label(self, req: httpx.Request) -> httpx.Response:
        self.known_labels.add(json.loads(req.content)["name"])
        return httpx.Response(201, json={})

    def _rm_label(self, req: httpx.Request) -> httpx.Response:
        m = re.search(r"/issues/(\d+)/labels/(.+)$", req.url.path)
        assert m
        self._labels.setdefault(int(m.group(1)), set()).discard(m.group(2))
        return httpx.Response(200, json=[])

    def _list_comments(self, req: httpx.Request) -> httpx.Response:
        n = int(self._n(req))
        return httpx.Response(
            200, json=[{"id": i, "body": b} for i, b in enumerate(self.comments.get(n, []))]
        )

    def _post_comment(self, req: httpx.Request) -> httpx.Response:
        n = int(self._n(req))
        self.comments.setdefault(n, []).append(json.loads(req.content)["body"])
        return httpx.Response(201, json={})

    def _find_check(self, req: httpx.Request) -> httpx.Response:
        m = re.search(r"/commits/(\w+)/check-runs", req.url.path)
        assert m
        name = req.url.params["check_name"]
        run = self.checks.get((m.group(1), name))
        return httpx.Response(200, json={"check_runs": [run] if run else []})

    def _create_check(self, req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        run = {"id": 500 + len(self.checks), "name": b["name"], **b}
        self.checks[(b["head_sha"], b["name"])] = run
        return httpx.Response(201, json=run)

    def _patch_check(self, req: httpx.Request) -> httpx.Response:
        cid = int(self._n(req, 2))
        for run in self.checks.values():
            if run["id"] == cid:
                run.update(json.loads(req.content))
                return httpx.Response(200, json=run)
        return httpx.Response(404, json={})

    def _perm(self, req: httpx.Request) -> httpx.Response:
        m = re.search(r"/collaborators/(.+)/permission$", req.url.path)
        assert m
        if m.group(1) not in self.perms:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"permission": self.perms[m.group(1)]})

    def _ref(self, req: httpx.Request) -> httpx.Response:
        branch = req.url.path.split("/git/ref/heads/", 1)[1]
        if branch not in self.branches:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"object": {"sha": self.branches[branch]}})


@pytest.fixture(params=["client", "fake"])
def impl(request: pytest.FixtureRequest):
    if request.param == "fake":
        yield FakeImpl()
        return
    with respx.mock(base_url=API, assert_all_called=False) as m:
        yield ClientImpl(m)


Scenario = Callable[[Impl], Awaitable[None]]


async def test_create_and_find(impl: Impl) -> None:
    api = impl.api
    assert await api.find_pr("factory/001-x") is None
    pr = await api.create_pr(head="factory/001-x", base="main", title="T", body="B")
    assert (pr.state, pr.merged, pr.draft, pr.merge_sha) == ("open", False, True, None)
    assert pr.head_sha == SHA
    assert await api.find_pr("factory/001-x") == pr
    assert await api.get_pr(pr.number) == pr


async def test_mark_ready(impl: Impl) -> None:
    pr = await impl.api.create_pr(head="b", base="main", title="T", body="B")
    await impl.api.mark_ready(pr)
    assert (await impl.api.get_pr(pr.number)).draft is False


async def test_merge(impl: Impl) -> None:
    pr = await impl.api.create_pr(head="b", base="main", title="T", body="B")
    assert await impl.api.merge_async(pr.number, sha=pr.head_sha, method="squash") == "u-1"
    merged = await impl.api.get_pr(pr.number)
    assert merged.merged and merged.state == "closed" and merged.merge_sha == "m" * 40


async def test_merge_refuses_another_head(impl: Impl) -> None:
    from swfactory.github.client import GitHubError

    pr = await impl.api.create_pr(head="b", base="main", title="T", body="B")
    with pytest.raises(GitHubError) as e:
        await impl.api.merge_async(pr.number, sha="b" * 40, method="squash")
    assert e.value.status == 409
    assert not (await impl.api.get_pr(pr.number)).merged


async def test_auto_merge_accepts_the_call(impl: Impl) -> None:
    pr = await impl.api.create_pr(head="b", base="main", title="T", body="B")
    await impl.api.enable_auto_merge(pr, expected_head_oid=pr.head_sha, method="squash")


async def test_labels_are_created_on_demand_and_removed(impl: Impl) -> None:
    await impl.api.add_labels(5, ["factory", "ui"])
    assert impl.labels(5) == {"factory", "ui"}
    await impl.api.add_labels(5, ["factory"])
    await impl.api.remove_label(5, "ui")
    assert impl.labels(5) == {"factory"}


async def test_comment_once(impl: Impl) -> None:
    assert await impl.api.comment_once(5, "hello <!-- m -->", marker="<!-- m -->") is True
    assert await impl.api.comment_once(5, "hello again <!-- m -->", marker="<!-- m -->") is False
    assert await impl.api.comment_once(5, "other <!-- n -->", marker="<!-- n -->") is True
    assert len(impl.comment_bodies(5)) == 2


async def test_check_run_upsert(impl: Impl) -> None:
    kw = dict(head_sha=SHA, name="factory/review", title="t", summary="s", details_url="https://d")
    assert await impl.api.has_check_run(SHA, "factory/review") is False
    first = await impl.api.upsert_check_run(conclusion="neutral", **kw)
    assert await impl.api.has_check_run(SHA, "factory/review") is True
    assert await impl.api.upsert_check_run(conclusion="success", **kw) == first
    assert await impl.api.has_check_run("c" * 40, "factory/review") is False


async def test_collaborator_permission(impl: Impl) -> None:
    impl.seed_permission("ana", "write")
    assert await impl.api.collaborator_permission("ana") == "write"
    assert await impl.api.collaborator_permission("mallory") == "none"


async def test_branch_sha(impl: Impl) -> None:
    impl.seed_branch("main", "d" * 40)
    impl.seed_branch("factory/001-x", "e" * 40)
    assert await impl.api.branch_sha("main") == "d" * 40
    assert await impl.api.branch_sha("factory/001-x") == "e" * 40
    assert await impl.api.branch_sha("nope") is None


async def test_the_fake_counts_calls() -> None:
    f = FakeGitHub()
    await f.find_pr("x")
    await f.find_pr("y")
    assert f.calls["find_pr"] == 2
