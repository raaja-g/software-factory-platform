"""Objective signals (scorer names prefixed ``obj.``) computed from the facts of one job."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, text

from swfactory.domain.types import (
    Decision,
    MergePolicy,
    RunStatus,
    Stage,
    Verdict,
    VerifyStatus,
)

OBJ_PREFIX = "obj."
MEASURES = frozenset({"obj.fix_attempts", "obj.rounds", "obj.duration_s", "obj.cost_usd"})
OBJ_SCORERS = frozenset(
    {
        "obj.gate",
        "obj.fix_attempts",
        "obj.rounds",
        "obj.decision",
        "obj.human",
        "obj.delivery",
        "obj.fallback",
        "obj.duration_s",
        "obj.cost_usd",
    }
)
if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from swfactory.definition.models import AppDef

REVERT_WINDOW_DAYS = 30
NIGHTLY_NAME = "nightly_scoring"
OBJ_REVERTED = "obj.reverted"
_PASSING = (VerifyStatus.PASSED.value, VerifyStatus.SKIPPED.value)
_REVIEWER = "reviewer-"


@dataclass(frozen=True, slots=True)
class ScoreRow:
    scorer: str
    label: str
    score: Decimal | None
    passed: bool | None
    reason: str
    run_id: UUID | None = None
    judge_model: str | None = None
    cost_usd: Decimal = Decimal(0)
    rubric_sha: str | None = None
    judge_sha: str | None = None


@dataclass(frozen=True, slots=True)
class JobFacts:
    job: Mapping[str, Any]
    runs: Sequence[Mapping[str, Any]]
    gates: Sequence[Mapping[str, Any]]
    human_actions: Sequence[str]
    decision_event: Mapping[str, Any] | None
    delivery: Mapping[str, Any] | None


def _d(n: int | str | Decimal) -> Decimal:
    return Decimal(n)


def _order(r: Mapping[str, Any]) -> tuple[int, int]:
    return (int(r.get("round") or 0), int(r.get("attempt") or 0))


def _last_builder_run(runs: Sequence[Mapping[str, Any]]) -> UUID | None:
    ok = [r for r in runs if r.get("role") == "builder" and r.get("status") == RunStatus.OK.value]
    return max(ok, key=_order)["id"] if ok else None


def _gate_rows(gates: Sequence[Mapping[str, Any]], run_id: UUID | None) -> list[ScoreRow]:
    if not gates:
        return []
    last = max(gates, key=_order)
    status = str(last.get("status"))
    score = {VerifyStatus.PASSED.value: "1.0", VerifyStatus.SKIPPED.value: "0.5"}.get(status, "0")
    exit_code = last.get("exit_code")
    protected = list(last.get("protected") or ())
    reason = (
        f"tests: {'none' if exit_code is None else f'exit {exit_code}'}"
        f" · protected paths: {', '.join(protected) if protected else 'clean'}"
        f" · screenshots: {len(last.get('screenshots') or ())}"
    )
    if last.get("reason"):
        reason += f" · {last['reason']}"
    return [ScoreRow("obj.gate", status, _d(score), status in _PASSING, reason, run_id)]


def _verdict_rows(runs: Sequence[Mapping[str, Any]], run_id: UUID | None) -> list[ScoreRow]:
    kinds: dict[str, dict[int, Mapping[str, Any]]] = {}
    for r in sorted(runs, key=_order):
        role = str(r.get("role"))
        if role.startswith(_REVIEWER) and r.get("verdict"):
            kinds.setdefault(role[len(_REVIEWER) :], {})[int(r.get("round") or 0)] = r
    out: list[ScoreRow] = []
    for kind, by_round in sorted(kinds.items()):
        verdicts = [str(by_round[k]["verdict"]) for k in sorted(by_round)]
        ok = {Verdict.PASS.value, Verdict.SKIPPED.value}
        final_ok = verdicts[-1] in ok
        score = _d("1.0") if verdicts[0] in ok else _d("0.5") if final_ok else _d(0)
        out.append(
            ScoreRow(
                f"obj.verdict.{kind}",
                ">".join(verdicts),
                score,
                final_ok,
                f"{kind} reviewer: {' then '.join(verdicts)}",
                run_id,
            )
        )
    return out


def _human_row(actions: Sequence[str], run_id: UUID | None) -> ScoreRow:
    seen = set(actions)
    parts = [
        n
        for a, n in (("rework", "rework"), ("reject", "reject"), ("approve", "override"))
        if a in seen
    ]
    if not parts:
        return ScoreRow("obj.human", "none", _d("1.0"), True, "no human action", run_id)
    label = "+".join(parts)
    return ScoreRow(
        "obj.human", label, _d(0), False, f"human actions: {', '.join(actions)}", run_id
    )


def _delivery_row(f: JobFacts, run_id: UUID | None) -> ScoreRow:
    stage = str(f.job.get("stage"))
    policy = f.job.get("merge_policy")
    if stage == Stage.DELIVERED.value:
        return ScoreRow("obj.delivery", "delivered", None, None, "PR open, not merged yet", run_id)
    if stage != Stage.MERGED.value:
        return ScoreRow("obj.delivery", stage, _d(0), False, f"job ended {stage}", run_id)
    if policy != MergePolicy.PR_MANUAL.value:
        return ScoreRow("obj.delivery", "merged_unchanged", _d("1.0"), True, str(policy), run_id)
    reviewed = (f.decision_event or {}).get("head_sha")
    merged = (f.delivery or {}).get("head_sha")
    if not reviewed or not merged:
        return ScoreRow("obj.delivery", "merged", _d("1.0"), True, "head sha not recorded", run_id)
    if reviewed == merged:
        return ScoreRow(
            "obj.delivery", "merged_unchanged", _d("1.0"), True, "merged the approved head", run_id
        )
    return ScoreRow(
        "obj.delivery", "merged_changed", _d("0.5"), True, "head changed after approval", run_id
    )


def _run_rows(runs: Sequence[Mapping[str, Any]]) -> list[ScoreRow]:
    out: list[ScoreRow] = []
    for r in sorted(runs, key=_order):
        status = str(r.get("status"))
        failed = r.get("verdict") == Verdict.ERROR.value or status in (
            RunStatus.FAILED.value,
            RunStatus.TIMEOUT.value,
            RunStatus.BUDGET.value,
        )
        if status == RunStatus.RUNNING.value:
            label, score, passed = "running", None, None
        elif failed:
            label, score, passed = "error", _d(0), False
        elif status == RunStatus.OK.value:
            label, score, passed = "ok", _d("1.0"), True
        else:
            label, score, passed = status, _d(0), False
        reason = f"{r.get('role')} r{r.get('round')}a{r.get('attempt')}: {status}"
        if r.get("verdict"):
            reason += f", verdict {r['verdict']}"
        if r.get("error"):
            reason += f" ({r['error']})"
        out.append(
            ScoreRow(
                "obj.run",
                label,
                score,
                passed,
                reason,
                r.get("id"),
                cost_usd=_d(r.get("cost_usd") or 0),
            )
        )
    return out


def _duration_s(job: Mapping[str, Any]) -> int | None:
    a, b = job.get("started_at"), job.get("finished_at")
    if a is None or b is None:
        return None
    return round((b - a).total_seconds())


def compute_signals(f: JobFacts) -> list[ScoreRow]:
    rid = _last_builder_run(f.runs)
    rows = _gate_rows(f.gates, rid)
    rounds_with_gate = len({int(g.get("round") or 0) for g in f.gates})
    fixes = len(f.gates) - rounds_with_gate
    rows.append(
        ScoreRow(
            "obj.fix_attempts",
            str(fixes),
            _d(fixes),
            None,
            "gate results beyond one per round",
            rid,
        )
    )
    rounds = int(f.job.get("round") or max((int(r.get("round") or 0) for r in f.runs), default=0))
    rows.append(ScoreRow("obj.rounds", str(rounds), _d(rounds), None, "review rounds", rid))
    rows += _verdict_rows(f.runs, rid)
    if f.decision_event is not None:
        dec = str(f.decision_event.get("decision"))
        ok = dec == Decision.APPROVE.value
        rows.append(
            ScoreRow("obj.decision", dec, _d("1.0" if ok else "0"), ok, f"approver: {dec}", rid)
        )
    rows.append(_human_row(f.human_actions, rid))
    rows.append(_delivery_row(f, rid))
    fb = sum(1 for r in f.runs if r.get("fallback_detected"))
    if fb:
        rows.append(
            ScoreRow(
                "obj.fallback",
                f"{fb} of {len(f.runs)} runs",
                _d(0),
                False,
                "silent model fallback detected",
                rid,
            )
        )
    else:
        rows.append(ScoreRow("obj.fallback", "none", _d("1.0"), True, "no fallback detected", rid))
    secs = _duration_s(f.job)
    if secs is not None:
        rows.append(
            ScoreRow("obj.duration_s", f"{secs // 60} min", _d(secs), None, f"{secs} seconds", rid)
        )
    cost = _d(f.job.get("cost_usd") or 0)
    rows.append(ScoreRow("obj.cost_usd", f"${cost:.2f}", cost, None, "job cost in USD", rid))
    rows += _run_rows(f.runs)
    return rows


@dataclass(frozen=True, slots=True)
class CommitMessage:
    sha: str
    message: str
    at: datetime


class CommitLog(Protocol):
    async def messages_since(self, app: AppDef, since: datetime) -> list[CommitMessage]: ...


class LocalCommitLog:
    """Commit messages of the default branch of the platform's bare repo."""

    def __init__(self, repos_dir: Path) -> None:
        self.repos_dir = Path(repos_dir)

    async def messages_since(self, app: AppDef, since: datetime) -> list[CommitMessage]:
        from swfactory.gitops.service import app_slug

        bare = self.repos_dir / f"{app_slug(app)}.git"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(bare),
            "log",
            app.default_branch,
            f"--since={since.astimezone(UTC).isoformat()}",
            "--format=%H%x00%cI%x00%B%x1e",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git log failed: {err.decode(errors='replace')[:200]}")
        msgs: list[CommitMessage] = []
        for rec in out.decode(errors="replace").split("\x1e"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            sha, at, body = rec.split("\x00", 2)
            msgs.append(CommitMessage(sha.strip(), body.strip(), datetime.fromisoformat(at)))
        return msgs


_GH_REPO = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


class GitHubCommitLog:
    """Commit messages of the default branch, read through the GitHub API."""

    def __init__(self, client_factory: Callable[[str], Any]) -> None:
        self._factory = client_factory

    async def messages_since(self, app: AppDef, since: datetime) -> list[CommitMessage]:
        m = _GH_REPO.search(app.repo_url)
        if m is None:
            return []
        client = self._factory(f"{m[1]}/{m[2]}")
        rows = await client.list_commits(app.default_branch, since)
        return [
            CommitMessage(
                str(r["sha"]),
                str(r["commit"]["message"]),
                datetime.fromisoformat(
                    str((r["commit"].get("committer") or r["commit"]["author"])["date"])
                ),
            )
            for r in rows
        ]


_REVERTS = re.compile(r"(?m)^This reverts commit ([0-9a-f]{7,40})")


def find_revert(
    msgs: Sequence[CommitMessage],
    *,
    merge_sha: str,
    branch_shas: Collection[str],
    title: str,
) -> CommitMessage | None:
    """The first commit that reverts the merge, one of its branch commits, or the title."""
    shas = [x for x in (merge_sha, *branch_shas) if x]
    subject = f'Revert "{title}"'
    for m in msgs:
        for ref in _REVERTS.findall(m.message):
            if any(full.startswith(ref) for full in shas):
                return m
        if m.message.split("\n", 1)[0].strip() == subject:
            return m
    return None


def due_daily(last: datetime | None, now: datetime, *, hour_utc: int = 2) -> bool:
    """True once per UTC day, from ``hour_utc`` on, when ``last`` is before today's slot."""
    slot = now.astimezone(UTC).replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return now >= slot and (last is None or last < slot)


def _app_def(kind: str, url: str, branch: str) -> AppDef:
    from swfactory.definition.models import AppDef

    return AppDef.model_validate({"repo_kind": kind, "repo_url": url, "default_branch": branch})


async def _refresh_stale(s: Any) -> set[UUID]:
    """Recompute the objective rows of jobs changed after their newest obj.* row."""
    from swfactory.db.repo import load_job_facts, replace_scores
    from swfactory.db.tables import Job, Score

    newest = (
        select(Score.job_id.label("jid"), func.max(Score.created_at).label("at"))
        .where(Score.scorer.like(OBJ_PREFIX + "%"))
        .group_by(Score.job_id)
        .subquery()
    )
    ids = (
        await s.scalars(
            select(Job.id).join(newest, newest.c.jid == Job.id).where(Job.updated_at > newest.c.at)
        )
    ).all()
    for jid in ids:
        rows = compute_signals(await load_job_facts(s, jid))
        old = (
            await s.scalars(
                select(Score.scorer)
                .where(Score.job_id == jid, Score.scorer.like(OBJ_PREFIX + "%"))
                .distinct()
            )
        ).all()
        names = (set(old) | OBJ_SCORERS | {r.scorer for r in rows}) - {OBJ_REVERTED}
        names.discard("obj.quality")
        await replace_scores(s, jid, rows, scorers=names)
    return set(ids)


async def _revert_pass(
    s: Any, logs: Mapping[str, CommitLog], now: datetime, changed: set[UUID]
) -> None:
    from swfactory.db.repo import replace_scores
    from swfactory.db.tables import App, Job, JobEvent, Score
    from swfactory.domain.types import JobKind, Stage

    since = now - timedelta(days=REVERT_WINDOW_DAYS)
    rows = (
        await s.execute(
            select(Job, App, func.max(JobEvent.at))
            .join(App, App.id == Job.app_id)
            .join(JobEvent, JobEvent.job_id == Job.id)
            .where(
                Job.kind == JobKind.NORMAL.value,
                Job.stage == Stage.MERGED.value,
                JobEvent.kind == "delivery.merged",
                JobEvent.at >= since,
            )
            .group_by(Job.id, App.id)
        )
    ).all()
    by_app: dict[str, list[CommitMessage]] = {}
    for job, app, merged_at in rows:
        log = logs.get(app.slug) or logs.get(app.repo_kind)
        if log is None:
            continue
        if app.slug not in by_app:
            by_app[app.slug] = await log.messages_since(
                _app_def(app.repo_kind, app.repo_url, app.default_branch), since
            )
        ev = await s.scalar(
            select(JobEvent.data)
            .where(JobEvent.job_id == job.id, JobEvent.kind == "delivery.merged")
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
        data = dict(ev or {})
        msgs = [m for m in by_app[app.slug] if m.at >= merged_at - timedelta(minutes=1)]
        found = find_revert(
            msgs,
            merge_sha=str(data.get("merge_sha") or ""),
            branch_shas={x for x in (job.head_sha, job.pushed_sha, data.get("head_sha")) if x},
            title=job.title,
        )
        reverted = found is not None
        label = "reverted" if reverted else "not_reverted"
        prev = await s.scalar(
            select(Score.label).where(Score.job_id == job.id, Score.scorer == OBJ_REVERTED)
        )
        if prev == label:
            continue
        row = ScoreRow(
            OBJ_REVERTED,
            label,
            Decimal(0 if reverted else 1),
            not reverted,
            f"reverted by {found.sha[:10]}" if found else "no revert found in 30 days",
        )
        await replace_scores(s, job.id, [row], scorers=[OBJ_REVERTED])
        changed.add(job.id)


async def nightly_scoring(
    sessions: async_sessionmaker[Any], logs: Mapping[str, CommitLog], *, now: datetime
) -> int:
    """Once a day: refresh stale objective rows and write ``obj.reverted``. Returns jobs changed."""
    from swfactory.db.tables import MaintenanceRun

    async with sessions() as s, s.begin():
        got = await s.scalar(
            text("select pg_try_advisory_xact_lock(hashtext(:n))"), {"n": NIGHTLY_NAME}
        )
        if not got:
            return 0
        last = await s.scalar(
            select(MaintenanceRun.last_run_at).where(MaintenanceRun.name == NIGHTLY_NAME)
        )
        if not due_daily(last, now):
            return 0
        changed = await _refresh_stale(s)
        await _revert_pass(s, logs, now, changed)
        await s.merge(MaintenanceRun(name=NIGHTLY_NAME, last_run_at=now))
        return len(changed)
