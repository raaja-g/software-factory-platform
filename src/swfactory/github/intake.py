"""Issue intake, webhook signatures, merge detection and the GitHub poller (M8.5)."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.commands import format_job_ref
from swfactory.db.repo import set_stage
from swfactory.db.tables import App, GithubPollState, IssueIntake, Job, JobEvent
from swfactory.definition.models import AppDef, Definition
from swfactory.definition.store import NoActiveDefinition, load_active
from swfactory.domain.types import TERMINAL_STAGES, Stage
from swfactory.events import record_event
from swfactory.github.client import GitHubClient, GitHubError
from swfactory.queue import UnknownApp, enqueue_job
from swfactory.settings import Settings
from swfactory.watchdog import job_link

log = structlog.get_logger(__name__)

WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_GH_REPO = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")

ClientFor = Callable[[str], GitHubClient]


def repo_of(app: AppDef) -> str | None:
    """``owner/name`` of a github app, or None."""
    m = _GH_REPO.match(app.repo_url)
    return f"{m.group(1)}/{m.group(2)}" if app.repo_kind == "github" and m else None


def app_for_repo(defn: Definition, repo: str) -> tuple[str, AppDef] | None:
    for slug, app in defn.factory.apps.items():
        if repo_of(app) == repo:
            return slug, app
    return None


def verify_github_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    want = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode(), header.encode())


def _label_names(issue: Mapping[str, Any]) -> list[str]:
    return [
        str(lb["name"]) if isinstance(lb, Mapping) else str(lb) for lb in issue.get("labels") or []
    ]


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _permission(client: GitHubClient, login: str) -> str:
    try:
        return await client.collaborator_permission(login)
    except GitHubError as e:
        if e.status == 404:
            return "none"
        raise


async def intake_issue(
    s: AsyncSession,
    client: GitHubClient,
    *,
    app: AppDef,
    repo: str,
    issue: Mapping[str, Any],
) -> UUID | None:
    """Take one labelling of an issue in, in the caller's transaction; returns the new job's id."""
    number = int(issue["number"])
    event = await client.issue_label_event(number, app.github.intake_label)
    if event is None or event.user_type == "Bot" or event.login.endswith("[bot]"):
        return None
    author = str((issue.get("user") or {}).get("login") or "")
    author_perm = await _permission(client, author) if author else "none"
    labeler_perm = await _permission(client, event.login)

    source_ref = f"{repo}#{number}"
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": source_ref})
    inserted = await s.scalar(
        pg_insert(IssueIntake)
        .values(
            repo=repo,
            issue_number=number,
            labeled_at=event.at,
            labeled_by=event.login,
            author=author,
            author_permission=author_perm,
            labeler_permission=labeler_perm,
        )
        .on_conflict_do_nothing()
        .returning(IssueIntake.issue_number)
    )
    if inserted is None:
        return None
    active = await s.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.source_ref == source_ref,
            Job.source == "github_issue",
            Job.stage.notin_([st.value for st in TERMINAL_STAGES]),
        )
    )
    if active:
        return None
    slug = await s.scalar(
        select(App.slug).where(
            App.repo_url == f"https://github.com/{repo}", App.archived_at.is_(None)
        )
    )
    if slug is None:
        if await load_active(s) is None:
            raise NoActiveDefinition("no active definition")
        raise UnknownApp(repo)
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    job = await enqueue_job(
        s,
        app_slug=slug,
        feature=f"{title}\n\n{body}",
        title=" ".join(title.split())[:120] or None,
        source="github_issue",
        source_ref=source_ref,
        requested_by=f"github:{event.login}",
        require_human_approval=(
            author_perm not in WRITE_PERMISSIONS or labeler_perm not in WRITE_PERMISSIONS
        ),
    )
    job.triage = {"labels": _label_names(issue)}
    await s.execute(
        text(
            "UPDATE app.issue_intakes SET job_id = :j "
            "WHERE repo = :r AND issue_number = :n AND labeled_at = :t"
        ),
        {"j": job.id, "r": repo, "n": number, "t": event.at},
    )
    return job.id


async def comment_intake(
    s: AsyncSession, client: GitHubClient, *, app: AppDef, job_id: UUID
) -> None:
    """Post the intake comment; call after the intake's transaction committed."""
    try:
        job = await s.get(Job, job_id)
        if job is None or job.source_ref is None:
            return
        row = (
            await s.execute(
                select(IssueIntake)
                .where(IssueIntake.job_id == job_id)
                .order_by(IssueIntake.labeled_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        slug = await s.scalar(select(App.slug).where(App.id == job.app_id))
        if row is None or slug is None:
            return
        text_ = f"Queued as `{format_job_ref(slug, job.seq)}`."
        if job.require_human_approval:
            if row.author_permission not in WRITE_PERMISSIONS:
                who, how = row.author, "who opened this issue"
            else:
                who, how = row.labeled_by, "who added the label"
            text_ += (
                " A person must approve this job in the factory before it is delivered, "
                f"because @{who}, {how}, does not have write access."
            )
        marker = f"factory:intake:{job_id}"
        await client.comment_once(row.issue_number, f"{text_}\n\n<!-- {marker} -->", marker=marker)
    except Exception as exc:
        log.warning("github.intake_comment_failed", job_id=str(job_id), error=repr(exc))


# -- merge detection ----------------------------------------------------------------


def _reconcilable(pr_url: str):
    no_live_lease = or_(Job.lease_owner.is_(None), Job.lease_until < func.now())
    return (
        Job.pr_url == pr_url,
        or_(
            Job.stage == Stage.DELIVERED.value,
            (Job.stage == Stage.NEEDS_HUMAN.value) & no_live_lease,
        ),
    )


async def _lock_reconcilable(s: AsyncSession, pr_url: str) -> Job | None:
    return (
        await s.execute(
            select(Job).where(*_reconcilable(pr_url)).with_for_update().limit(1),
            execution_options={"populate_existing": True},
        )
    ).scalar_one_or_none()


async def mark_merged(s: AsyncSession, *, pr_url: str, merge_sha: str, head_sha: str) -> bool:
    job = await _lock_reconcilable(s, pr_url)
    if job is None:
        return False
    moved = await set_stage(s, job.id, Stage.MERGED, finished_at=datetime.now(UTC))
    if moved:
        await record_event(
            s,
            job.id,
            "delivery.merged",
            {"merge_sha": merge_sha, "head_sha": head_sha, "pr_url": pr_url},
        )
    return moved


async def mark_closed(s: AsyncSession, *, pr_url: str) -> bool:
    job = await _lock_reconcilable(s, pr_url)
    if job is None:
        return False
    moved = await set_stage(s, job.id, Stage.CLOSED, finished_at=datetime.now(UTC))
    if moved:
        await record_event(s, job.id, "delivery.closed", {"pr_url": pr_url})
    return moved


# -- issue outcomes ---------------------------------------------------------------------


def outcome_comment(
    *, stage: Stage, ref: str, label: str, pr_url: str | None, expired_days: int | None
) -> str:
    """The comment that tells an issue how its job ended."""
    again = f"Add the `{label}` label again to request another run."
    if stage is Stage.MERGED:
        where = f" in {pr_url}" if pr_url else ""
        return f"Job {ref} was merged{where}. {again}"
    if stage is Stage.CLOSED:
        if expired_days is not None:
            return f"Job {ref} was closed after waiting {expired_days} days for a person. {again}"
        where = f" ({pr_url})" if pr_url else ""
        return f"Job {ref} was closed without merging{where}. {again}"
    return f"Job {ref} was cancelled. {again}"


async def report_outcomes(
    s: AsyncSession, clients: Callable[[str], GitHubClient], *, limit: int = 20
) -> int:
    """Take the label off issues whose job ended and say how, once each; rows reported.

    Every step checks before it acts (a gone label, a comment with the marker), so a crash
    between steps never posts twice. Commits after each row.
    """
    found = await load_active(s)
    await s.rollback()
    if found is None:
        return 0
    defn = found[1]
    rows = (
        await s.execute(
            select(
                IssueIntake.repo,
                IssueIntake.issue_number,
                IssueIntake.labeled_at,
                Job.id,
                Job.seq,
                Job.stage,
                Job.pr_url,
                App.slug,
            )
            .join(Job, Job.id == IssueIntake.job_id)
            .join(App, App.id == Job.app_id)
            .where(
                IssueIntake.reported_at.is_(None),
                Job.stage.in_([st.value for st in TERMINAL_STAGES]),
            )
            .order_by(IssueIntake.labeled_at, IssueIntake.issue_number)
            .limit(limit)
        )
    ).all()
    await s.rollback()
    done = 0
    for repo, number, labeled_at, job_id, seq, stage, pr_url, slug in rows:
        hit = app_for_repo(defn, repo)
        if hit is None:
            continue
        app = hit[1]
        expired = await s.scalar(
            select(JobEvent.data["waited_days"].astext)
            .where(JobEvent.job_id == job_id, JobEvent.kind == "job.expired")
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
        client = clients(repo)
        try:
            try:
                await client.remove_label(number, app.github.intake_label)
            except GitHubError as e:
                if e.status != 404:
                    raise
            marker = f"factory:outcome:{job_id}"
            body = outcome_comment(
                stage=Stage(stage),
                ref=format_job_ref(slug, seq),
                label=app.github.intake_label,
                pr_url=pr_url,
                expired_days=int(expired) if expired is not None else None,
            )
            await client.comment_once(number, f"{body}\n\n<!-- {marker} -->", marker=marker)
        except GitHubError as exc:
            log.warning("github.outcome_failed", issue=f"{repo}#{number}", error=repr(exc))
            await s.rollback()
            continue
        await s.execute(
            update(IssueIntake)
            .where(
                IssueIntake.repo == repo,
                IssueIntake.issue_number == number,
                IssueIntake.labeled_at == labeled_at,
            )
            .values(reported_at=func.now())
        )
        await s.commit()
        done += 1
    return done


# -- webhook handler registry ---------------------------------------------------------


@dataclass(slots=True)
class HookCtx:
    s: AsyncSession
    sessions: async_sessionmaker[AsyncSession]
    defn: Definition
    client_for: ClientFor
    settings: Settings


AfterCommit = Callable[[], Awaitable[None]]
Handler = Callable[[HookCtx, Mapping[str, Any]], Awaitable[AfterCommit | None]]


class GitHubEventHandlers:
    """Maps ``(event, action)`` to a handler; M8.6 and M8.9 register more."""

    def __init__(self) -> None:
        self._h: dict[tuple[str, str], Handler] = {}

    def register(self, event: str, action: str, handler: Handler) -> None:
        self._h[(event, action)] = handler

    def get(self, event: str, action: str) -> Handler | None:
        return self._h.get((event, action))


async def _on_issue_labeled(ctx: HookCtx, payload: Mapping[str, Any]) -> AfterCommit | None:
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    found = app_for_repo(ctx.defn, repo)
    if found is None:
        return None
    _, app = found
    if (payload.get("label") or {}).get("name") != app.github.intake_label:
        return None
    client = ctx.client_for(repo)
    job_id = await intake_issue(ctx.s, client, app=app, repo=repo, issue=payload["issue"])
    if job_id is None:
        return None

    async def after() -> None:
        async with ctx.sessions() as s2:
            await comment_intake(s2, client, app=app, job_id=job_id)

    return after


async def _on_pr_closed(ctx: HookCtx, payload: Mapping[str, Any]) -> AfterCommit | None:
    pr = payload.get("pull_request") or {}
    url = str(pr.get("html_url") or "")
    if not url:
        return None
    if pr.get("merged"):
        await mark_merged(
            ctx.s,
            pr_url=url,
            merge_sha=str(pr.get("merge_commit_sha") or ""),
            head_sha=str((pr.get("head") or {}).get("sha") or ""),
        )
    else:
        await mark_closed(ctx.s, pr_url=url)
    return None


async def _on_issue_comment(ctx: HookCtx, payload: Mapping[str, Any]) -> AfterCommit | None:
    from swfactory.github.commands import handle_comment

    repo = str((payload.get("repository") or {}).get("full_name") or "")
    found = app_for_repo(ctx.defn, repo)
    if found is None:
        return None
    await handle_comment(ctx.s, ctx.client_for(repo), app=found[1], payload=payload)
    return None


def default_handlers() -> GitHubEventHandlers:
    h = GitHubEventHandlers()
    h.register("issue_comment", "created", _on_issue_comment)
    h.register("issues", "labeled", _on_issue_labeled)
    h.register("pull_request", "closed", _on_pr_closed)
    from swfactory.definition.sync import PR_ACTIONS, definition_handler

    h.register("push", "", definition_handler("push"))
    for action in sorted(PR_ACTIONS):
        h.register("pull_request", action, definition_handler("pull_request"))
    return h


# -- poller -----------------------------------------------------------------------------


class GitHubPoller:
    """Polls github apps' issues and pull requests with one ETag per list."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        definitions: Any,
        client_for: ClientFor,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._definitions = definitions
        self._client_for = client_for
        self._settings = settings

    async def tick(self) -> None:
        try:
            _, defn = await self._definitions.active()
        except NoActiveDefinition:
            return
        for slug, app in defn.factory.apps.items():
            repo = repo_of(app)
            if repo is None:
                continue
            client = self._client_for(repo)
            for name, step in (
                ("issues", self._poll_issues),
                ("prs", self._poll_prs),
                ("waiting", self._comment_waiting),
                ("comments", self._poll_comments),
            ):
                try:
                    await step(client, app, repo)
                except Exception as exc:
                    log.warning("github.poll_failed", app=slug, step=name, error=repr(exc))
        try:
            await self._report_outcomes()
        except Exception as exc:
            log.warning("github.poll_failed", step="outcomes", error=repr(exc))

    async def _state(self, key: str) -> tuple[str | None, datetime | None]:
        async with self._sessions() as s:
            row = await s.get(GithubPollState, key)
            return (row.etag, row.polled_at) if row else (None, None)

    async def _save_state(self, key: str, etag: str | None) -> None:
        async with self._sessions.begin() as s:
            await s.execute(
                pg_insert(GithubPollState)
                .values(key=key, etag=etag, polled_at=func.now())
                .on_conflict_do_update(
                    index_elements=[GithubPollState.key],
                    set_={"etag": etag, "polled_at": func.now()},
                )
            )

    async def _poll_issues(self, client: GitHubClient, app: AppDef, repo: str) -> None:
        key = f"issues:{repo}"
        etag, _ = await self._state(key)
        items, new_etag, not_modified = await client.list_issues(
            label=app.github.intake_label, etag=etag
        )
        if not_modified:
            return
        ok = True
        for issue in items:
            if "pull_request" in issue or issue.get("state", "open") != "open":
                continue
            number = int(issue["number"])
            updated = _parse_ts(issue.get("updated_at"))
            async with self._sessions() as s:
                newest = await s.scalar(
                    select(func.max(IssueIntake.labeled_at)).where(
                        IssueIntake.repo == repo, IssueIntake.issue_number == number
                    )
                )
            if newest is not None and updated is not None and updated <= newest:
                continue
            try:
                async with self._sessions.begin() as s:
                    job_id = await intake_issue(s, client, app=app, repo=repo, issue=issue)
                if job_id is not None:
                    async with self._sessions() as s:
                        await comment_intake(s, client, app=app, job_id=job_id)
            except Exception as exc:
                ok = False
                log.warning("github.intake_failed", issue=f"{repo}#{number}", error=repr(exc))
        if ok:
            await self._save_state(key, new_etag)

    async def _poll_prs(self, client: GitHubClient, app: AppDef, repo: str) -> None:
        key = f"prs:{repo}"
        etag, polled_at = await self._state(key)
        async with self._sessions() as s:
            open_urls = set(
                (
                    await s.scalars(
                        select(Job.pr_url).where(
                            Job.pr_url.like(f"https://github.com/{repo}/pull/%"),
                            Job.stage.in_([Stage.DELIVERED.value, Stage.NEEDS_HUMAN.value]),
                        )
                    )
                ).all()
            )
        first_etag: str | None = None
        page = 1
        while True:
            prs, got_etag, not_modified = await client.list_prs(
                etag=etag if page == 1 else None, page=page
            )
            if page == 1:
                if not_modified:
                    return
                first_etag = got_etag
            for pr in prs:
                if pr.url not in open_urls or pr.state != "closed":
                    continue
                async with self._sessions.begin() as s:
                    if pr.merged:
                        await mark_merged(
                            s, pr_url=pr.url, merge_sha=pr.merge_sha or "", head_sha=pr.head_sha
                        )
                    else:
                        await mark_closed(s, pr_url=pr.url)
            more = (
                bool(prs)
                and len(prs) >= 100
                and polled_at is not None
                and all(p.updated_at is not None and p.updated_at > polled_at for p in prs)
            )
            if not more:
                break
            page += 1
        await self._save_state(key, first_etag)

    async def _poll_comments(self, client: GitHubClient, app: AppDef, repo: str) -> None:
        from datetime import timedelta

        from swfactory.db.repo import last_waiting_at
        from swfactory.github.commands import comment_payload, handle_comment

        key = f"comments:{repo}"
        etag, polled_at = await self._state(key)
        like = f"https://github.com/{repo}/pull/%"
        async with self._sessions() as s:
            jobs = (
                await s.execute(
                    select(Job.id, Job.pr_url).where(
                        Job.pr_url.like(like),
                        Job.stage.in_([Stage.DELIVERED.value, Stage.NEEDS_HUMAN.value]),
                    )
                )
            ).all()
            by_number = {int(str(u).rpartition("/")[2]): str(u) for _, u in jobs}
            if polled_at is not None:
                since = polled_at - timedelta(seconds=60)
            else:
                waits = [await last_waiting_at(s, j) for j, _ in jobs]
                since = min((w for w in waits if w is not None), default=datetime.now(UTC))
        comments, new_etag, not_modified = await client.list_issue_comments(since=since, etag=etag)
        if not_modified:
            return
        ok = True
        for c in comments:
            url = by_number.get(c.issue_number or -1)
            if url is None or (c.updated_at is not None and c.updated_at != c.created_at):
                continue
            try:
                async with self._sessions.begin() as s:
                    await handle_comment(
                        s, client, app=app, payload=comment_payload(c, repo=repo, pr_url=url)
                    )
            except Exception as exc:
                ok = False
                log.warning("github.comment_failed", comment_id=c.id, error=repr(exc))
        if ok:
            await self._save_state(key, new_etag)

    async def _report_outcomes(self) -> None:
        async with self._sessions() as s:
            await report_outcomes(s, self._client_for)

    async def _comment_waiting(self, client: GitHubClient, app: AppDef, repo: str) -> None:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(Job.id, Job.seq, Job.source_ref, Job.gate_reason, App.slug)
                    .join(App, App.id == Job.app_id)
                    .where(
                        Job.source == "github_issue",
                        Job.stage == Stage.NEEDS_HUMAN.value,
                        Job.pr_url.is_(None),
                        Job.source_ref.like(f"{repo}#%"),
                    )
                )
            ).all()
            for job_id, seq, source_ref, reason, slug in rows:
                wait_id = await s.scalar(
                    select(func.max(JobEvent.id)).where(
                        JobEvent.job_id == job_id, JobEvent.kind == "human.waiting"
                    )
                )
                if wait_id is None or source_ref is None:
                    continue
                number = int(str(source_ref).rpartition("#")[2])
                body = (
                    f"Job {format_job_ref(slug, seq)} needs a person: {reason or 'it is waiting'}. "
                    f"Open it in the factory: {job_link(self._settings, job_id)}"
                )
                marker = f"factory:waiting:{job_id}:{wait_id}"
                await client.comment_once(number, f"{body}\n\n<!-- {marker} -->", marker=marker)


def make_client_factory(
    settings: Settings, *, broker: bool = False, permissions: Mapping[str, str] | None = None
) -> ClientFor:
    """One GitHubClient per repo, sharing auth and the serial mutation queue.

    `permissions` is the installation-token set every client asks for (the delivery set when
    omitted); a caller that needs less, like the improver, names the narrower set."""
    from swfactory.github.auth import make_auth
    from swfactory.github.broker import BrokerAuth
    from swfactory.github.client import MutationQueue

    if broker and settings.broker_token_file:
        auth: Any = BrokerAuth(settings.broker_url, settings.broker_token_file)
    else:
        auth = make_auth(settings)
    queue = MutationQueue()
    cache: dict[str, GitHubClient] = {}

    def client_for(repo: str) -> GitHubClient:
        if repo not in cache:
            cache[repo] = (
                GitHubClient(auth, repo, queue)
                if permissions is None
                else GitHubClient(auth, repo, queue, permissions=permissions)
            )
        return cache[repo]

    return client_for
