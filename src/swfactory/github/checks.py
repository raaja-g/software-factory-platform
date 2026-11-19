"""Pure rendering of the pull request body and the ``factory/review`` check."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from swfactory.domain.types import Decision, Verdict

CHECK_NAME = "factory/review"
OMITTED_NOTE = "Some sections were omitted; see the dashboard."


@dataclass(frozen=True, slots=True)
class PrBodyContext:
    job_id: UUID
    feature: str
    spec_summary: str
    criteria: tuple[str, ...]
    verify: str
    screenshots: tuple[str, ...]
    verdicts: Mapping[int, Mapping[str, str]]
    decision: str | None
    decision_reason: str
    runs: tuple[tuple[str, str, str, Decimal], ...]  # (role, engine, model, cost_usd)
    dashboard_url: str
    approved_by: str | None = None


def _quote(text: str) -> str:
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _kinds(verdicts: Mapping[int, Mapping[str, str]], rounds: list[int]) -> list[str]:
    kinds: list[str] = []
    for r in rounds:
        for k in verdicts[r]:
            if k not in kinds:
                kinds.append(k)
    return kinds


def _verdict_table(verdicts: Mapping[int, Mapping[str, str]], rounds: list[int]) -> str:
    if not rounds:
        return "No reviews yet."
    kinds = _kinds(verdicts, rounds)
    out = ["| Round | " + " | ".join(_cell(k) for k in kinds) + " |"]
    out.append("|---|" + "---|" * len(kinds))
    for r in rounds:
        row = verdicts[r]
        out.append(f"| {r} | " + " | ".join(_cell(row.get(k, "-")) for k in kinds) + " |")
    return "\n".join(out)


def _decision_text(ctx: PrBodyContext) -> str:
    if ctx.decision is None:
        return "No decision yet."
    text = f"**{ctx.decision}**"
    if ctx.decision_reason:
        text += f": {ctx.decision_reason}"
    if ctx.approved_by:
        text += f"\n\nApproved by {ctx.approved_by}."
    return text


def _cost_table(ctx: PrBodyContext) -> str:
    if not ctx.runs:
        return "No model runs recorded."
    out = ["| Role | Engine | Model | Cost (USD) |", "|---|---|---|---|"]
    total = Decimal(0)
    for role, engine, model, cost in ctx.runs:
        total += cost
        out.append(f"| {_cell(role)} | {_cell(engine)} | {_cell(model)} | {cost:.4f} |")
    out.append(f"| **Total** | | | {total:.4f} |")
    return "\n".join(out)


def _build(ctx: PrBodyContext, *, with_verify: bool, rounds: list[int]) -> str:
    criteria = "\n".join(f"- [ ] {c}" for c in ctx.criteria) or "None listed."
    verify = ""
    if with_verify:
        verify = ctx.verify.strip() or "No verify log."
        if ctx.screenshots:
            verify += "\n\n" + "\n".join(f"![screenshot]({u})" for u in ctx.screenshots)
    else:
        verify = "Verify log omitted."
    parts = [
        f"<!-- factory:job={ctx.job_id} -->",
        "## Feature\n\n"
        + _quote(ctx.feature)
        + ("\n\n" + ctx.spec_summary.strip() if ctx.spec_summary.strip() else ""),
        "## Acceptance criteria\n\n" + criteria,
        "## Verify\n\n" + verify,
        "## Reviews\n\n" + _verdict_table(ctx.verdicts, rounds),
        "## Decision\n\n" + _decision_text(ctx),
        "## Models and cost\n\n" + _cost_table(ctx),
        f"[Open in the dashboard]({ctx.dashboard_url})",
    ]
    return "\n\n".join(parts) + "\n"


def render_pr_body(ctx: PrBodyContext, *, limit: int = 60_000) -> str:
    rounds = sorted(ctx.verdicts)
    body = _build(ctx, with_verify=True, rounds=rounds)
    if len(body) <= limit:
        return body
    omitted = False
    body = _build(ctx, with_verify=False, rounds=rounds)
    omitted = True
    while omitted and len(body) + len(OMITTED_NOTE) + 2 > limit and len(rounds) > 1:
        rounds = rounds[1:]
        body = _build(ctx, with_verify=False, rounds=rounds)
    note = "\n" + OMITTED_NOTE + "\n"
    if len(body) + len(note) > limit:
        room = max(limit - len(note), 0)
        body = body[:room]
    return body + note if len(body) + len(note) <= limit else (body + note)[:limit]


def _all_verdicts(ctx: PrBodyContext) -> list[str]:
    if not ctx.verdicts:
        return []
    return [str(v) for v in ctx.verdicts[max(ctx.verdicts)].values()]


def review_check(ctx: PrBodyContext) -> tuple[str, str, str]:
    """Return (conclusion, title, summary) for the ``factory/review`` check run."""
    verdicts = _all_verdicts(ctx)
    table = _verdict_table(ctx.verdicts, sorted(ctx.verdicts))
    decision_line = f"Decision: {ctx.decision or 'none'}"
    if ctx.decision_reason:
        decision_line += f" ({ctx.decision_reason})"
    if ctx.approved_by:
        conclusion = "success"
        title = f"Approved by {ctx.approved_by}"
        decision_line += f". Approved by {ctx.approved_by} at the human gate."
    elif ctx.decision == Decision.APPROVE and all(
        v in (Verdict.PASS, Verdict.SKIPPED) for v in verdicts
    ):
        conclusion, title = "success", "Reviews passed"
    elif any(v == Verdict.ERROR for v in verdicts):
        conclusion, title = "failure", "A reviewer failed"
    else:
        conclusion, title = "action_required", "Needs a person's review"
    return conclusion, title, f"{table}\n\n{decision_line}"
