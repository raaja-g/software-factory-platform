"""``/factory`` PR comment commands (M8.6): strict parsing, permission, replay and head checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.commands import CommandRefused, submit_command
from swfactory.db.repo import (
    command_by_source_ref,
    get_job_by_pr_url,
    last_stage_at,
    last_waiting_at,
)
from swfactory.definition.models import AppDef
from swfactory.domain.types import HumanAction, Stage
from swfactory.github.checks import CHECK_NAME
from swfactory.github.client import GitHubClient, GitHubError, IssueComment
from swfactory.github.followup import start_follow_up
from swfactory.roles.fence import fence_untrusted

log = structlog.get_logger(__name__)

COMMAND_RE = re.compile(r"^/factory (approve|reject|rework (?P<note>\S.*))[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_WRITE = frozenset({"admin", "maintain", "write"})


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    action: HumanAction
    note: str | None


def parse_command(body: str) -> ParsedCommand | None:
    fence: str | None = None
    for line in body.replace("\r\n", "\n").split("\n"):
        m = _FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif fence == m.group(1):
                fence = None
            continue
        if fence is not None or line.startswith(("    ", "\t")):
            continue
        hit = COMMAND_RE.match(line)
        if hit:
            word = hit.group(1).split(" ", 1)[0]
            return ParsedCommand(HumanAction(word), hit.group("note"))
    return None


def comment_payload(comment: IssueComment, *, repo: str, pr_url: str) -> dict[str, Any]:
    """The webhook payload shape for a polled comment."""
    return {
        "action": "created",
        "comment": {
            "id": comment.id,
            "body": comment.body,
            "created_at": comment.created_at.isoformat(),
            "user": {"login": comment.login, "type": comment.user_type},
        },
        "issue": {"number": comment.issue_number, "pull_request": {"html_url": pr_url}},
        "repository": {"full_name": repo},
        "sender": {"login": comment.login, "type": comment.user_type},
    }


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


async def _reply(client: GitHubClient, number: int, comment_id: int, text: str) -> None:
    marker = f"factory:cmd:{comment_id}"
    try:
        await client.comment_once(number, f"{text}\n\n<!-- {marker} -->", marker=marker)
    except GitHubError as exc:
        log.warning("github.command_reply_failed", comment_id=comment_id, error=repr(exc))


async def handle_comment(
    s: AsyncSession, client: GitHubClient, *, app: AppDef, payload: Mapping[str, Any]
) -> UUID | None:
    if payload.get("action") != "created":
        return None
    sender = payload.get("sender") or {}
    if sender.get("type") == "Bot":
        return None
    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        return None
    c = payload.get("comment") or {}
    cmd = parse_command(str(c.get("body") or ""))
    if cmd is None:
        return None
    pr_url = str((issue["pull_request"] or {}).get("html_url") or "")
    job = await get_job_by_pr_url(s, pr_url) if pr_url else None
    if job is None:
        return None
    delivered = job.stage == Stage.DELIVERED.value
    waited = await (last_stage_at if delivered else last_waiting_at)(s, job.id)
    created = _ts(c.get("created_at"))
    if waited is not None and (created is None or created <= waited):
        return None
    comment_id = int(c["id"])
    source_ref = f"gh-comment:{comment_id}"
    if await command_by_source_ref(s, source_ref) is not None:
        return None
    try:
        real = await client.get_comment(comment_id)
    except GitHubError:
        return None
    user = c.get("user") or {}
    if (real.login, real.user_type, real.body) != (
        user.get("login"),
        user.get("type"),
        c.get("body"),
    ):
        return None
    login = real.login
    try:
        perm = await client.collaborator_permission(login)
    except GitHubError:
        perm = "none"
    if cmd.action is HumanAction.APPROVE:
        ok = perm == "admin" or (perm in _WRITE and login in app.github.approvers)
    else:
        ok = perm in _WRITE
    if not ok:
        log.info("github.command_unauthorized", login=login, permission=perm, comment_id=comment_id)
        return None
    number = int(issue["number"])
    if delivered and cmd.action is not HumanAction.REWORK:
        await _reply(
            client, number, comment_id, "Not done: a delivered job accepts only /factory rework."
        )
        return None
    pr = await client.get_pr(number)
    if cmd.action is HumanAction.APPROVE and not await client.has_check_run(
        pr.head_sha, CHECK_NAME
    ):
        await _reply(
            client,
            number,
            comment_id,
            "Not approved: the branch changed since the review. Wait for a new review, then "
            "approve again.",
        )
        return None
    if delivered:
        try:
            new = await start_follow_up(
                s, job_id=job.id, note=cmd.note or "", requested_by=f"github:{login}"
            )
        except CommandRefused as exc:
            await _reply(client, number, comment_id, f"Not done: {exc}")
            return None
        return new.id
    note = fence_untrusted(f"PR comment by @{login}", cmd.note) if cmd.note is not None else None
    try:
        return await submit_command(
            s,
            job_id=job.id,
            action=cmd.action,
            note=note,
            requested_by=f"github:{login}",
            head_sha=pr.head_sha,
            source_ref=source_ref,
        )
    except CommandRefused as exc:
        await _reply(client, number, comment_id, f"Not done: {exc}")
        return None
