"""Fixed-vs-laya benchmark and the documented switch (M11.9). Laya is never switched on by code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.bench.report import BenchReport, ConfigSummary, build_report
from swfactory.bench.runner import Estimate, compute_estimate, start_bench_run
from swfactory.db.tables import LayaHead
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef
from swfactory.definition.store import load_active
from swfactory.laya.export import question_ids
from swfactory.laya.fit import RouterRefused, RouterUnreachable

EVAL_REPETITIONS = 2
EVAL_MIN_SAVING = Decimal("0.10")
ACTIVATE_PATH = "/v1/factory/heads/activate"
FIXED = "fixed"
LAYA = "laya"


class ActivationRefused(Exception):
    pass


class EvaluationRefused(Exception):
    """The evaluation was not started (nothing was spent)."""


@dataclass(frozen=True, slots=True)
class SwitchVerdict:
    switch: bool
    reasons: tuple[str, ...]
    fixed: ConfigSummary
    laya: ConfigSummary


def _rate(v: float | None) -> str:
    return "none" if v is None else f"{v:.2f}"


def _fell(label: str, fixed: float | None, laya: float | None) -> str | None:
    if fixed is None:
        return None
    if laya is None or laya < fixed:
        return f"{label} fell {_rate(fixed)} -> {_rate(laya)}"
    return None


def switch_verdict(
    report: BenchReport, *, question: str, head_version: str | None
) -> SwitchVerdict:
    """Quality holds (no lower pass rate, every task finished) and laya is at least 10% cheaper."""
    by_name = {c.name: c for c in report.configs}
    fixed, laya = by_name.get(FIXED), by_name.get(LAYA)
    if fixed is None or laya is None:
        raise ValueError("the report needs configurations named 'fixed' and 'laya'")
    reasons: list[str] = []
    if head_version is None:
        reasons.append(f"no fitted head for {question}")
    for name, c in ((LAYA, laya), (FIXED, fixed)):
        if c.completed != c.trials:
            reasons.append(f"{name} finished {c.completed} of {c.trials} tasks")
    for msg in (
        _fell("hidden-check pass rate", fixed.hidden_pass_rate, laya.hidden_pass_rate),
        _fell("judge pass rate", fixed.judge_pass_rate, laya.judge_pass_rate),
    ):
        if msg:
            reasons.append(msg)
    if fixed.mean_cost_usd is None or laya.mean_cost_usd is None:
        reasons.append("no cost data")
    elif laya.mean_cost_usd > fixed.mean_cost_usd * (1 - EVAL_MIN_SAVING):
        reasons.append(f"not 10% cheaper: ${laya.mean_cost_usd:.2f} vs ${fixed.mean_cost_usd:.2f}")
    return SwitchVerdict(not reasons, tuple(reasons), fixed, laya)


def evaluation_configs(
    profile: str, question: str, *, version: int
) -> tuple[BenchConfigDef, BenchConfigDef]:
    """The ``fixed`` and ``laya`` configurations; both pin the question so the definition's mode
    never leaks in. The laya one pins the evaluated head version."""
    if question.startswith("tier."):
        if question not in _all_tier_questions():
            raise ValueError(f"unknown question '{question}'")
        role = question.removeprefix("tier.")
        fixed: dict[str, Any] = {"roles": {role: {"mode": "fixed"}}}
        laya: dict[str, Any] = {"roles": {role: {"mode": "laya"}}}
    elif question in ("kind", "size", "risk", "touches_ui"):
        fixed = {"triage_modes": {question: "rules"}}
        laya = {"triage_modes": {question: "laya"}}
    else:
        raise ValueError(f"unknown question '{question}'")
    return (
        BenchConfigDef.model_validate({"name": FIXED, "profile": profile, **fixed}),
        BenchConfigDef.model_validate(
            {"name": LAYA, "profile": profile, **laya, "laya_heads": {question: version}}
        ),
    )


def _all_tier_questions() -> set[str]:
    from swfactory.definition.models import TIER_ROLES

    return {f"tier.{r.value}" for r in TIER_ROLES}


def holdout_overlap(
    suite_name: str, suite: BenchSuiteDef, train_refs: Collection[str]
) -> list[str]:
    """Suite tasks that are in a head's training rows. Only ``job:`` and ``bench:`` refs count."""
    refs = set(train_refs)
    out = [
        name
        for name, task in suite.tasks.items()
        if f"bench:{suite_name}/{name}" in refs
        or (task.source_job and f"job:{task.source_job}" in refs)
    ]
    return sorted(out)


async def activate_head(
    s: AsyncSession,
    router_url: str,
    api_key: str,
    question: str,
    version: int,
    *,
    by: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LayaHead:
    """Make one head the only active one for its question. Needs a passing evaluation."""
    head = await s.scalar(
        select(LayaHead).where(LayaHead.question == question, LayaHead.version == version)
    )
    if head is None:
        raise ActivationRefused(f"{question} v{version} does not exist")
    ev = head.evaluation if isinstance(head.evaluation, dict) else {}
    if ev.get("switch") is not True:
        raise ActivationRefused(f"{question} v{version} has no passing evaluation")
    if not router_url:
        raise RouterUnreachable("FACTORY_LAYA_URL is not set")
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as http:
            resp = await http.post(
                router_url.rstrip("/") + ACTIVATE_PATH,
                json={
                    "question_id": question,
                    "version": version,
                    "head_sha256": head.head_sha256,
                },
                headers={"authorization": f"Bearer {api_key}"},
            )
    except httpx.TransportError as e:
        raise RouterUnreachable(f"router unreachable: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise RouterRefused(f"router answered http {resp.status_code}: {resp.text[:200]}")
    await s.execute(update(LayaHead).where(LayaHead.question == question).values(active=False))
    head.active = True
    head.activated_by = by
    head.activated_at = datetime.now(UTC)
    await s.flush()
    return head


# --- the evaluation run -------------------------------------------------------------------


def report_table(rep: BenchReport) -> list[str]:
    lines = [
        f"{'configuration':<14}{'trials':<8}{'hidden pass':<13}{'judge pass':<12}"
        f"{'rounds':<8}cost/task"
    ]
    for c in rep.configs:
        rounds = "-" if c.mean_rounds is None else f"{c.mean_rounds:.1f}"
        cost = "-" if c.mean_cost_usd is None else f"${c.mean_cost_usd:.2f}"
        lines.append(
            f"{c.name:<14}{c.trials:<8}{_rate(c.hidden_pass_rate):<13}"
            f"{_rate(c.judge_pass_rate):<12}{rounds:<8}{cost}"
        )
    return lines


def verdict_line(v: SwitchVerdict) -> str:
    return "switch: yes" if v.switch else f"switch: no ({'; '.join(v.reasons)})"


def report_markdown(
    question: str, version: int, suite: str, reps: int, rep: BenchReport, v: SwitchVerdict
) -> str:
    table = "\n".join(report_table(rep))
    return (
        f"# Laya evaluation: {question} v{version}\n\n"
        f"- run: {rep.run_id}\n- suite: {suite}\n- repetitions: {reps}\n"
        f"- definition: {rep.definition_sha}\n\n```\n{table}\n```\n\n{verdict_line(v)}\n"
    )


async def run_evaluation(
    sessions: async_sessionmaker[AsyncSession],
    *,
    question: str,
    suite: str,
    version: int | None,
    repetitions: int,
    profile: str | None,
    requested_by: str,
    confirm: Callable[[Estimate, list[str]], Awaitable[bool]],
    out_dir: Path | None = None,
    poll_s: float = 5.0,
) -> tuple[SwitchVerdict, BenchReport, Path] | None:
    """Benchmark fixed against laya on ``suite``. None when the person declines the estimate."""
    from swfactory.api.routes.bench import load_run

    if repetitions < EVAL_REPETITIONS:
        raise EvaluationRefused(f"refused: at least {EVAL_REPETITIONS} repetitions are needed")
    async with sessions() as s:
        active = await load_active(s)
        if active is None:
            raise EvaluationRefused("refused: no active definition")
        sha, defn = active
        if question not in question_ids(defn):
            raise EvaluationRefused(f"refused: unknown question '{question}'")
        heads = list(
            await s.scalars(
                select(LayaHead).where(LayaHead.question == question).order_by(LayaHead.version)
            )
        )
        if version is None:
            head = heads[-1] if heads else None
        else:
            head = next((h for h in heads if h.version == version), None)
        if head is None:
            raise EvaluationRefused(f"refused: no fitted head for {question}")
        sdef = defn.benchmarks.get(suite)
        if sdef is None:
            raise EvaluationRefused(f"refused: no such benchmark suite '{suite}'")
        overlap = holdout_overlap(suite, sdef, head.train_refs or [])
        if overlap:
            raise EvaluationRefused(
                f"refused: {suite} tasks {', '.join(overlap)} are in {question} "
                f"v{head.version}'s training rows; evaluate on a holdout suite"
            )
        prof = profile or (
            sdef.configurations[0].profile if sdef.configurations else next(iter(defn.profiles))
        )
        if prof not in defn.profiles:
            raise EvaluationRefused(f"refused: unknown profile '{prof}'")
        try:
            configs = evaluation_configs(prof, question, version=head.version)
            _, _, est = await compute_estimate(
                s, defn, suite=suite, configs=list(configs), repetitions=repetitions
            )
        except ValueError as e:
            raise EvaluationRefused(f"refused: {e}") from None
        if not await confirm(est, [c.name for c in configs]):
            return None
        run_id = await start_bench_run(
            s,
            defn,
            suite=suite,
            configs=list(configs),
            repetitions=repetitions,
            budget_usd=None,
            definition_sha=sha,
            accept_estimate_usd=est.total_usd,
            requested_by=requested_by,
        )
        head_id, head_version, head_label = head.id, head.version, f"{question}@v{head.version}"
    rep = await _wait_report(sessions, run_id, poll_s, load_run)
    verdict = switch_verdict(rep, question=question, head_version=head_label)
    from swfactory.settings import get_settings

    out_dir = out_dir or get_settings().laya_export_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"eval-{question}-{str(run_id)[:8]}.md"
    path.write_text(
        report_markdown(question, head_version, suite, repetitions, rep, verdict), encoding="utf-8"
    )
    async with sessions() as s, s.begin():
        await s.execute(
            update(LayaHead)
            .where(LayaHead.id == head_id)
            .values(
                evaluation={
                    "run_id": str(run_id),
                    "suite": suite,
                    "repetitions": repetitions,
                    "switch": verdict.switch,
                    "reasons": list(verdict.reasons),
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
        )
    return verdict, rep, path


async def _wait_report(
    sessions: async_sessionmaker[AsyncSession], run_id: UUID, poll_s: float, load_run: Any
) -> BenchReport:
    while True:
        async with sessions() as s:
            found = await load_run(s, run_id)
        if found is None:
            raise EvaluationRefused(f"refused: bench run {run_id} vanished")
        run, results = found
        if run["status"] != "running":
            return build_report(run, results)
        await asyncio.sleep(poll_s)
