"""Judge scorers: a sampled, cross-family LLM judge with provenance stripped from its inputs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select

from swfactory.costs.budgets import BudgetExceeded
from swfactory.db.repo import insert_model_decision
from swfactory.db.tables import AgentRun
from swfactory.definition.models import Definition, JudgeEntry, ScorerDef
from swfactory.domain.types import ModelRef, Role, RunStatus, SelectionMode, ToolPolicy
from swfactory.graph.deps import Deps
from swfactory.graph.steps import StepOutcome, pinned_definition, run_agent_step
from swfactory.models.catalog import ModelInfo
from swfactory.models.profiles import model_family
from swfactory.models.selection import ModelChoice
from swfactory.roles.contracts import ContractError
from swfactory.scoring.objective import ScoreRow

JUDGE_INPUT_VERSION = 1
_ENGINE_WORDS = ("opencode", "claude_code", "gemini_cli")
_MODEL_WORDS = (
    "claude",
    "sonnet",
    "opus",
    "haiku",
    "gemini",
    "gpt",
    "grok",
    "llama",
    "qwen",
    "claude code",
    "gemini cli",
)
_PROVENANCE_LINE = re.compile(r"^[ \t]*(Factory-Model|Co-Authored-By):.*(?:\n|$)", re.I | re.M)
_REVIEW_RE = re.compile(r"^round-(\d+)/review-.*\.md$")
_REDACTED = "[redacted]"


def sampled(job_id: UUID, scorer: str, rate: float, *, benchmark: bool = False) -> bool:
    if benchmark:
        return True
    digest = hashlib.sha256(f"{job_id}:{scorer}".encode()).hexdigest()
    return int(digest[:8], 16) / 2**32 < rate


def rubric_sha(scorer: ScorerDef) -> str:
    body = {
        "rubric": scorer.rubric,
        "labels": [lb.model_dump(mode="json") for lb in scorer.labels],
        "passing_score": scorer.passing_score,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def judge_sha(upstream_id: str, judge_prompt: str) -> str:
    body = [upstream_id, judge_prompt, JUDGE_INPUT_VERSION]
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


def provenance_names(defn: Definition) -> frozenset[str]:
    names: set[str] = set(_ENGINE_WORDS) | set(_MODEL_WORDS)
    for alias, model in defn.factory.models.items():
        names.add(alias)
        names.add(model.upstream_id)
    return frozenset(n.lower() for n in names if n)


def strip_provenance(text: str, names: Iterable[str]) -> str:
    text = _PROVENANCE_LINE.sub("", text)
    ordered = sorted({n for n in names if n}, key=len, reverse=True)
    if not ordered:
        return text
    pattern = "(?<!\\w)(?:" + "|".join(re.escape(n) for n in ordered) + ")(?!\\w)"
    return re.sub(pattern, _REDACTED, text, flags=re.I)


@dataclass(frozen=True, slots=True)
class JudgePick:
    alias: str | None
    reason: str
    options: tuple[Mapping[str, Any], ...]


def pick_judge(
    entry: JudgeEntry,
    infos: Mapping[str, ModelInfo | None],
    *,
    builder_families: Collection[str],
) -> JudgePick:
    families = sorted(set(builder_families))
    shown = ", ".join(families) if families else "none"
    tried = [entry.model, *entry.fallback]
    options: list[Mapping[str, Any]] = []
    chosen: str | None = None
    for alias in tried:
        info = infos.get(alias)
        family = model_family(info.upstream_id) if info is not None else None
        usable = info is not None and info.available
        outside = family is not None and family not in families
        options.append({"alias": alias, "family": family, "available": usable})
        if chosen is None and usable and outside:
            chosen = alias
    if chosen is None:
        reason = (
            f"skipped: no available judge outside the builder's family ({shown}); "
            f"tried {', '.join(tried)}"
        )
        return JudgePick(None, reason, tuple(options))
    return JudgePick(chosen, f"judge: outside builder family ({shown})", tuple(options))


@dataclass(frozen=True, slots=True)
class Judgement:
    label: str
    score: Decimal
    passed: bool
    reason: str


_LABEL_RE = re.compile(r"^[\s#*_]*label[\s*_]*:[\s*_]*(.*?)[\s*_]*$", re.I)  # no `>`: a quote
_REASON_RE = re.compile(r"^[\s#>*_]*reason[\s*_]*:[\s*_]*(.*?)[\s*_]*$", re.I)
_VALUE_STRIP = " \t`'\"*_"


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")


def _unquoted_lines(text: str) -> list[str]:
    """The non-empty lines of the judge's own answer: not code, not a quote.

    The judge reads untrusted text (a diff can contain a line "LABEL: A") and may quote it back.
    Quoted or code lines are the input talking, never the verdict, so they are dropped before
    anything is parsed: lines inside a fenced block, indented code, and ">" quotes. Fences follow
    CommonMark, so this parser and a markdown renderer agree on what is code: a fence closes only
    on a line of the same character, at least as long as the opening one, with nothing after it.
    A fence that never closes is refused, not guessed at."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for ln in text.splitlines():
        if fence is not None:
            m = _FENCE_CLOSE.match(ln)
            if m is not None and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        m = _FENCE_OPEN.match(ln)
        if m is not None and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if ln.startswith(("    ", "\t")):  # indented code
            continue
        t = ln.strip()
        if t and not t.startswith(">"):
            out.append(ln)
    if fence is not None:
        raise ContractError("unclosed code fence in the answer")
    return out


def parse_judgement(text: str, scorer: ScorerDef) -> Judgement:
    # One `LABEL: <value>` line anywhere: models sometimes open with a sentence, and the answer is
    # still clear. Two or more is ambiguous (a judge quoting a line from the diff, then its own),
    # so that is refused rather than guessed at.
    lines = _unquoted_lines(text)
    hits: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _LABEL_RE.match(ln)
        if m is not None and m.group(1).strip(_VALUE_STRIP):
            hits.append((i, m.group(1).strip(_VALUE_STRIP)))
    if not hits:
        raise ContractError("no 'LABEL: <value>' line in the answer")
    if len(hits) > 1:
        raise ContractError("more than one 'LABEL: <value>' line in the answer")
    at, given = hits[0]
    by_lower = {lb.value.lower(): lb for lb in scorer.labels}
    found = by_lower.get(given.lower())
    if found is None:
        raise ContractError(f"unknown label '{given}'")
    reason = ""
    for ln in lines[at + 1 :]:
        rm = _REASON_RE.match(ln)
        if rm is not None:
            reason = rm.group(1).strip()
            break
    score = Decimal(str(found.score))
    return Judgement(found.value, score, score >= Decimal(str(scorer.passing_score)), reason)


def judge_instructions(scorer: ScorerDef) -> str:
    rows = ["| label | score | meaning |", "| --- | --- | --- |"]
    rows += [f"| {lb.value} | {lb.score} | {lb.description} |" for lb in scorer.labels]
    parts = []
    if scorer.rubric.strip():
        parts.append(f"## Rubric\n\n{scorer.rubric.strip()}")
    parts.append("## Labels\n\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _minutes(r: Mapping[str, Any]) -> str:
    start, end = r.get("started_at"), r.get("finished_at")
    if start is None or end is None:
        return ""
    return f"{(end - start).total_seconds() / 60:.1f}"


def judge_runs_table(runs: Sequence[Mapping[str, Any]]) -> str:
    out = [
        "| role | round | attempt | status | verdict | minutes | tokens |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in runs:
        tokens = int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
        out.append(
            f"| {r.get('role')} | {r.get('round')} | {r.get('attempt')} | {r.get('status')} "
            f"| {r.get('verdict') or ''} | {_minutes(r)} | {tokens} |"
        )
    return "\n".join(out) + "\n"


def _run_dict(r: AgentRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "role": r.role,
        "round": r.round,
        "attempt": r.attempt,
        "status": r.status,
        "verdict": r.verdict,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "model_alias": r.model_alias,
    }


async def run_judge(
    deps: Deps,
    job_id: UUID,
    name: str,
    scorer: ScorerDef,
    *,
    round: int,
    occurrence: int,
    extra_sections: Mapping[str, str] = {},  # noqa: B006
) -> ScoreRow:
    job, defn, _app = await pinned_definition(deps, job_id)
    judge_prompt = deps.roles.get(Role.JUDGE, definition_sha=job.definition_sha).body
    r_sha = rubric_sha(scorer)

    async with deps.sessions() as s:
        runs = [
            _run_dict(r)
            for r in await s.scalars(
                select(AgentRun)
                .where(AgentRun.job_id == job_id)
                .order_by(AgentRun.started_at, AgentRun.attempt)
            )
        ]
    role0 = scorer.applies_to[0] if scorer.applies_to else None
    ok_of_role = [r for r in runs if r["role"] == role0 and r["status"] == RunStatus.OK.value]
    subject_run: UUID | None = (
        max(ok_of_role, key=lambda r: (r["round"], r["attempt"]))["id"] if ok_of_role else None
    )

    # 1. the builder families
    builder_runs = [r for r in runs if r["role"] == Role.BUILDER.value]
    families: set[str] = set()
    for alias in {r["model_alias"] for r in builder_runs if r["model_alias"]}:
        info = await deps.catalog.get(alias)
        if info is not None:
            families.add(model_family(info.upstream_id))

    # 2. the decision row, even for a skip
    entry = scorer.judge
    infos: dict[str, ModelInfo | None] = {}
    for alias in [entry.model, *entry.fallback]:
        infos[alias] = await deps.catalog.get(alias)
    pick = pick_judge(entry, infos, builder_families=families)
    first = infos.get(pick.alias) if pick.alias else next((i for i in infos.values() if i), None)
    j_sha = judge_sha(first.upstream_id if first is not None else entry.model, judge_prompt)
    async with deps.sessions() as s, s.begin():
        decision_id = await insert_model_decision(
            s,
            job_id=job_id,
            role=Role.JUDGE.value,
            round=round,
            mode=SelectionMode.FIXED.value,
            options=pick.options,
            chosen=pick.alias,
            reason=pick.reason,
            latency_ms=None,
        )

    def row(
        label: str, score: Decimal | None, passed: bool | None, reason: str, **kw: Any
    ) -> ScoreRow:
        return ScoreRow(
            name, label, score, passed, reason, subject_run, rubric_sha=r_sha, judge_sha=j_sha, **kw
        )

    if pick.alias is None:
        return row("skipped", None, None, pick.reason)
    info = infos[pick.alias]
    assert info is not None

    # 3. the judge's inputs
    names = provenance_names(defn)
    base = f"scoring/{name}"
    put = deps.artifacts.put
    await put(
        job_id,
        f"{base}/runs.md",
        strip_provenance(
            judge_runs_table([r for r in runs if r["role"] != Role.JUDGE.value]), names
        ),
    )
    extras = [f"{base}/{key}.md" for key in extra_sections]
    for key, text in extra_sections.items():
        await put(job_id, f"{base}/{key}.md", text)
    transcripts: list[str] = []
    metas = await deps.artifacts.list(job_id)
    by_run = {r["id"]: r for r in builder_runs}
    for meta in sorted(metas, key=lambda m: m.path):
        if name == "process-compliance" and meta.kind == "transcript" and meta.run_id in by_run:
            text = await deps.artifacts.get_text(job_id, meta.path)
            if text is None:
                continue
            br = by_run[meta.run_id]
            path = f"{base}/transcript-r{br['round']}-a{br['attempt']}.md"
            await put(job_id, path, strip_provenance(text, names))
            transcripts.append(path)
    known = {m.path for m in metas}
    rounds = [int(m.group(1)) for p in known if (m := _REVIEW_RE.match(p))]
    reviews = (
        sorted(p for p in known if (m := _REVIEW_RE.match(p)) and int(m.group(1)) == max(rounds))
        if rounds
        else []
    )
    inputs = [
        p
        for p in ("feature.md", "spec.md", "diff.patch", "verify.md", *reviews, "decision.md")
        if p in known
    ]
    inputs += [f"{base}/runs.md", *extras, *transcripts]

    # 4. the judge run
    choice = ModelChoice(
        engine=entry.engine,
        model=ModelRef(info.alias, info.provider, info.upstream_id),
        mode=SelectionMode.FIXED,
        tier=None,
        decision_id=decision_id,
        reason=pick.reason,
    )
    outcome: StepOutcome
    try:
        outcome = await run_agent_step(
            deps,
            job_id=job_id,
            role=Role.JUDGE,
            round=round,
            inputs=inputs,
            output_name=f"{base}/r{occurrence}.md",
            workdir=None,
            tool_policy=ToolPolicy.NONE,
            occurrence=occurrence,
            choice=choice,
            instructions=judge_instructions(scorer),
        )
    except BudgetExceeded:
        return row("skipped", None, None, "skipped: budget", judge_model=pick.alias)

    async with deps.sessions() as s:
        run = await s.get(AgentRun, outcome.run_id)
    cost = run.cost_usd if run is not None and run.cost_usd is not None else Decimal(0)
    extra: dict[str, Any] = {"judge_model": pick.alias, "cost_usd": cost}
    if outcome.status == RunStatus.BUDGET:
        return row("skipped", None, None, "skipped: budget", **extra)
    if outcome.status != RunStatus.OK or outcome.text is None:
        return row("invalid", None, None, f"judge run {outcome.status.value}", **extra)
    try:
        j = parse_judgement(outcome.text, scorer)
    except ContractError as exc:
        return row("invalid", None, None, str(exc), **extra)
    return row(j.label, j.score, j.passed, j.reason, **extra)
