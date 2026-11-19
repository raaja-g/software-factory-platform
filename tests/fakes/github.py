"""In-memory FakeGitHub implementing the GitHubClient surface; counts calls per method."""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from swfactory.github.client import GitHubError, IssueComment, LabelEvent, PullRef


class FakeGitHub:
    def __init__(self, repo: str = "acme/web", *, slug: str = "fac", bot_id: int = 42) -> None:
        self.repo = repo
        self.slug = slug
        self.bot_id = bot_id
        self.calls: Counter[str] = Counter()
        self.prs: dict[int, dict[str, Any]] = {}
        self.branches: dict[str, str] = {}
        self.labels: dict[int, set[str]] = {}
        self.comments: dict[int, list[IssueComment]] = {}
        self.check_runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.permissions: dict[str, str] = {}
        self.issues: list[dict[str, Any]] = []
        self.label_events: dict[int, list[tuple[str, str, str, datetime]]] = {}
        self.rules: dict[str, list[dict[str, Any]]] = {}
        self.merges: dict[str, tuple[int, str, str]] = {}
        self.auto_merge: dict[int, tuple[str, str]] = {}
        self._next = 1
        self._next_id = 100

    def _count(self, name: str) -> None:
        self.calls[name] += 1

    def _ref(self, n: int) -> PullRef:
        d = self.prs[n]
        return PullRef(
            n,
            f"PR_n{n}",
            f"https://github.com/{self.repo}/pull/{n}",
            d["head_sha"],
            d["state"],
            d["merged"],
            d["draft"],
            d["merge_sha"],
            d.get("updated_at"),
        )

    # -- pull requests ------------------------------------------------------------

    async def find_pr(self, branch: str) -> PullRef | None:
        self._count("find_pr")
        found = [n for n, d in self.prs.items() if d["head"] == branch]
        open_ = [n for n in found if self.prs[n]["state"] == "open"]
        pick = open_ or found
        return self._ref(pick[0]) if pick else None

    async def get_pr(self, number: int) -> PullRef:
        self._count("get_pr")
        if number not in self.prs:
            raise GitHubError(404, "not found")
        return self._ref(number)

    async def create_pr(
        self, *, head: str, base: str, title: str, body: str, draft: bool = True
    ) -> PullRef:
        self._count("create_pr")
        n = self._next
        self._next += 1
        self.prs[n] = {
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "draft": draft,
            "state": "open",
            "merged": False,
            "merge_sha": None,
            "head_sha": self.branches.get(head, "a" * 40),
        }
        return self._ref(n)

    def merge_externally(self, number: int) -> None:
        """A person merges the PR on GitHub (no API call from the factory)."""
        self.prs[number].update(state="closed", merged=True, merge_sha="e" * 40)

    async def update_pr_body(self, number: int, body: str) -> None:
        self._count("update_pr_body")
        self.prs[number]["body"] = body

    async def close_pr(self, number: int) -> None:
        self._count("close_pr")
        self.prs[number]["state"] = "closed"

    async def mark_ready(self, pr: PullRef) -> None:
        self._count("mark_ready")
        self.prs[pr.number]["draft"] = False

    async def enable_auto_merge(self, pr: PullRef, *, expected_head_oid: str, method: str) -> None:
        self._count("enable_auto_merge")
        self.auto_merge[pr.number] = (expected_head_oid, method.upper())

    async def merge_async(self, number: int, *, sha: str, method: str) -> str:
        self._count("merge_async")
        if self.prs[number]["head_sha"] != sha:
            raise GitHubError(409, "head branch was modified")
        uuid = f"u-{len(self.merges) + 1}"
        self.merges[uuid] = (number, sha, method)
        d = self.prs[number]
        d.update(state="closed", merged=True, merge_sha="m" * 40)
        return uuid

    async def merge_async_status(self, number: int, uuid: str) -> str:
        self._count("merge_async_status")
        return "merged" if uuid in self.merges else "unknown"

    async def list_prs(
        self, *, etag: str | None, page: int = 1
    ) -> tuple[list[PullRef], str | None, bool]:
        self._count("list_prs")
        refs = [self._ref(n) for n in sorted(self.prs, reverse=True)]
        return refs[(page - 1) * 100 : page * 100], '"fake"', etag == '"fake"'

    # -- issues, labels, comments -------------------------------------------------

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self._count("add_labels")
        self.labels.setdefault(number, set()).update(labels)

    async def remove_label(self, number: int, label: str) -> None:
        self._count("remove_label")
        self.labels.setdefault(number, set()).discard(label)

    def add_comment(
        self,
        number: int,
        body: str,
        *,
        login: str = "someone",
        user_type: str = "User",
        id: int | None = None,
        perm: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> IssueComment:
        self._next_id += 1
        now = created_at or datetime.now(UTC)
        if perm is not None:
            self.permissions[login] = perm
        c = IssueComment(
            id if id is not None else self._next_id,
            login,
            user_type,
            body,
            now,
            updated_at or now,
            number,
        )
        self.comments.setdefault(number, []).append(c)
        return c

    async def comment_once(self, number: int, body: str, *, marker: str) -> bool:
        self._count("comment_once")
        if any(marker in c.body for c in self.comments.get(number, [])):
            return False
        self.add_comment(number, body, login=f"{self.slug}[bot]", user_type="Bot")
        return True

    async def get_comment(self, comment_id: int) -> IssueComment:
        self._count("get_comment")
        for cs in self.comments.values():
            for c in cs:
                if c.id == comment_id:
                    return c
        raise GitHubError(404, "not found")

    async def list_issues(
        self, *, label: str, etag: str | None
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self._count("list_issues")
        if etag == '"fake"':
            return [], etag, True
        return [i for i in self.issues if label in i.get("labels", [])], '"fake"', False

    async def list_issue_comments(
        self, *, since: datetime, etag: str | None
    ) -> tuple[list[IssueComment], str | None, bool]:
        self._count("list_issue_comments")
        if etag == '"fake"':
            return [], etag, True
        out = [
            c
            for cs in self.comments.values()
            for c in cs
            if (c.updated_at or c.created_at) >= since
        ]
        return out, '"fake"', False

    async def issue_label_event(self, number: int, label: str) -> LabelEvent | None:
        self._count("issue_label_event")
        hits = [e for e in self.label_events.get(number, []) if e[0] == "labeled" and e[1] == label]
        if not hits:
            return None
        _, _, login, at = hits[-1]
        return LabelEvent(login, "User", at)

    # -- checks, repo --------------------------------------------------------------

    async def has_check_run(self, head_sha: str, name: str) -> bool:
        self._count("has_check_run")
        return (head_sha, name) in self.check_runs

    async def upsert_check_run(
        self,
        *,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        details_url: str,
    ) -> int:
        self._count("upsert_check_run")
        key = (head_sha, name)
        if key not in self.check_runs:
            self._next_id += 1
            self.check_runs[key] = {"id": self._next_id}
        self.check_runs[key].update(
            conclusion=conclusion, title=title, summary=summary, details_url=details_url
        )
        return int(self.check_runs[key]["id"])

    async def collaborator_permission(self, login: str) -> str:
        self._count("collaborator_permission")
        return self.permissions.get(login, "none")

    async def branch_sha(self, branch: str) -> str | None:
        self._count("branch_sha")
        return self.branches.get(branch)

    async def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        self._count("branch_rules")
        return self.rules.get(branch, [])

    async def bot_identity(self) -> tuple[str, str]:
        self._count("bot_identity")
        return f"{self.slug}[bot]", f"{self.bot_id}+{self.slug}[bot]@users.noreply.github.com"
