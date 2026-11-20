"""Rules-based triage and the hard reviewer rules."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from swfactory.db.repo import insert_model_decision
from swfactory.definition.models import AppDef, TriageConfig, TriageRule
from swfactory.domain.types import DEFAULT_REVIEWERS, Role, RunStatus, ToolPolicy, TriageMode
from swfactory.roles.contracts import ContractError, check_output, parse_triage
from swfactory.roles.render import estimate_tokens

if TYPE_CHECKING:
    from swfactory.artifacts import ArtifactStore
    from swfactory.db.tables import Job
    from swfactory.definition.store import DefinitionStore
    from swfactory.graph.steps import StepOutcome
    from swfactory.models.router_client import LayaAnswer, RouterClient

log = logging.getLogger(__name__)

_QUESTIONS = ("kind", "size", "risk", "touches_ui")
TRIAGE_QUESTIONS: Mapping[str, Mapping[str, Any]] = {
    "kind": {
        "type": "choice",
        "instructions": "What kind of change is this?",
        "criteria": {
            "feature": "new behaviour for users",
            "bugfix": "fixes wrong behaviour",
            "refactor": "restructures code without changing behaviour",
            "docs": "documentation only",
            "infra": "build, CI, deployment or dependencies",
        },
    },
    "size": {
        "type": "choice",
        "instructions": "How large is this change?",
        "criteria": {
            "S": "a few lines in one file",
            "M": "a few files in one module, with tests",
            "L": "many files or modules, or a new subsystem",
        },
    },
    "risk": {
        "type": "choice",
        "instructions": "How risky is this change?",
        "criteria": {
            "low": "isolated and easily reverted",
            "medium": "touches shared code paths or behaviour other features rely on",
            "high": "touches auth, network input parsing, secrets or data deletion",
        },
    },
    "touches_ui": {
        "type": "choice",
        "instructions": "Does this change touch the browser user interface?",
        "criteria": {
            "A": "changes browser-rendered UI or browser behaviour",
            "B": "backend or CLI only",
        },
    },
}
TOUCHES_UI_LABELS = {"A": True, "B": False}
_DEFAULTS: Mapping[str, Any] = {"kind": "feature", "size": "M", "risk": "low", "touches_ui": False}


def triage_state(job: Any, app: Any, *, max_tokens: int = 512) -> dict[str, Any]:
    """What Laya sees: the job's title and feature plus the app, trimmed to ``max_tokens``."""
    tri = job.triage if isinstance(getattr(job, "triage", None), dict) else {}
    repo = str(getattr(app, "repo_url", "") or "").rstrip("/").removesuffix(".git")
    state: dict[str, Any] = {
        "title": str(job.title),
        "feature": str(job.feature),
        "app": repo.rsplit("/", 1)[-1] or "unknown",
        "stack": str(tri.get("stack") or "unknown"),
        "labels": [str(x) for x in tri.get("labels", [])],
        "ui_globs": [str(g) for g in getattr(app, "ui_globs", ())],
    }
    feature = state["feature"]
    while estimate_tokens(json.dumps(state)) >= max_tokens and feature:
        feature = feature[: max(0, len(feature) // 2)] if len(feature) > 40 else ""
        state["feature"] = feature.rstrip() + " [omitted]"
    return state


@dataclass(frozen=True, slots=True)
class TriageResult:
    kind: str
    size: str
    risk: str
    touches_ui: bool
    source: Mapping[str, str]
    profile_name: str | None
    budget_usd: Decimal | None
    reviewers: tuple[str, ...]
    require_human_approval: bool


@dataclass(frozen=True, slots=True)
class ReviewerPlan:
    run: tuple[str, ...]
    skipped: Mapping[str, str]


def matches_glob(path: str, glob: str) -> bool:
    return fnmatchcase(path, glob) or (glob.startswith("**/") and fnmatchcase(path, glob[3:]))


_TRAIL = ".,;:!?)]}>\"'"
_LEAD = "([{<\"'"
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def mentioned_paths(text: str) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[\s`]+", text):
        tok = tok.lstrip(_LEAD).rstrip(_TRAIL)
        if not tok or "://" in tok:
            continue
        if ("/" in tok or _EXT.search(tok)) and tok not in out:
            out.append(tok)
    return out


def _label(rule: TriageRule, idx: int) -> str:
    return f'rule "{rule.name}"' if rule.name else f"rule #{idx}"


def triage_by_rules(
    config: TriageConfig, app: AppDef, *, text: str, labels: Sequence[str]
) -> tuple[TriageResult, list[str]]:
    result, reasons, _ = _triage_by_rules(config, app, text=text, labels=labels)
    return result, reasons


def _triage_by_rules(
    config: TriageConfig, app: AppDef, *, text: str, labels: Sequence[str]
) -> tuple[TriageResult, list[str], set[str]]:
    set_keys: set[str] = set()
    kind, size, risk, ui = "feature", "M", "low", False
    profile: str | None = None
    budget: Decimal | None = None
    reviewers: tuple[str, ...] = app.reviewers
    approval = False
    reasons: list[str] = []
    paths = mentioned_paths(text)
    for idx, rule in enumerate(config.rules, 1):
        who = _label(rule, idx)
        m = rule.match
        hits: list[str] = []
        for kw in m.keywords:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                hits.append(f'- {who} matched keyword "{kw}"')
        for lb in m.labels:
            if lb in labels:
                hits.append(f'- {who} matched label "{lb}"')
        for g in m.globs:
            for p in paths:
                if matches_glob(p, g):
                    hits.append(f'- {who} matched glob "{g}" on {p}')
        if not (m.keywords or m.labels or m.globs):
            hits.append(f"- {who} matched (no conditions)")
        if not hits:
            continue
        reasons.extend(hits)
        for k, v in rule.set.items():
            set_keys.add(k)
            if k == "kind":
                kind = str(v)
            elif k == "size":
                size = str(v)
            elif k == "risk":
                risk = str(v)
            else:
                ui = bool(v)
        if rule.profile is not None:
            if rule.profile in app.allowed_profiles:
                profile = rule.profile
            else:
                reasons.append(
                    f'- {who} profile "{rule.profile}" is not allowed for this app; ignored'
                )
        if rule.budget_usd is not None:
            budget = rule.budget_usd
        if rule.reviewers is not None:
            reviewers = rule.reviewers
        if rule.require_human_approval:
            approval = True
    if budget is not None and app.budget_usd is not None and budget > app.budget_usd:
        budget = app.budget_usd
    result = TriageResult(
        kind=kind,
        size=size,
        risk=risk,
        touches_ui=ui,
        source=dict.fromkeys(_QUESTIONS, "rules"),
        profile_name=profile,
        budget_usd=budget,
        reviewers=reviewers,
        require_human_approval=approval,
    )
    return result, reasons, set_keys


def merge_triage(
    rule_values: Mapping[str, Any],
    defaults: Mapping[str, Any],
    answers: Mapping[str, Any],
    modes: Mapping[str, TriageMode],
    *,
    llm_values: Mapping[str, Any] = {},  # noqa: B006
    llm_error: str | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Values, source and reason per question. A value a rule set always wins (rules last).

    ``answers`` are LayaAnswer-like objects (``choice``, ``route``, ``error``).
    """
    values: dict[str, Any] = {}
    source: dict[str, str] = {}
    reason: dict[str, str] = {}
    for q in _QUESTIONS:
        mode = modes.get(q, TriageMode.RULES)
        values[q], source[q] = defaults[q], "rules"
        if q in rule_values:
            values[q] = rule_values[q]
            reason[q] = (
                "rule override"
                if mode in (TriageMode.LAYA, TriageMode.LLM)
                else "shadow: rules decided"
                if mode == TriageMode.SHADOW
                else "rules"
            )
        elif mode == TriageMode.SHADOW:
            reason[q] = "shadow: rules decided"
        elif mode == TriageMode.LAYA:
            a: Any = answers.get(q)
            err = a.error if a is not None else "missing answer"
            if a is None or err is not None:
                reason[q] = f"laya fallback: router error ({err})"
            elif a.route != "auto" or a.choice is None:
                reason[q] = "route review: rules decided"
            else:
                choice = a.choice
                values[q] = TOUCHES_UI_LABELS[choice] if q == "touches_ui" else choice
                source[q], reason[q] = "laya", "laya: answer used"
        elif mode == TriageMode.LLM:
            if q in llm_values:
                values[q], source[q], reason[q] = llm_values[q], "llm", "llm: answer used"
            else:
                reason[q] = f"llm fallback: {llm_error or 'no answer'}"
        else:
            reason[q] = "rules"
    return values, source, reason


def render_triage_md(result: TriageResult, reasons: Sequence[str]) -> str:
    lines = [
        "# Triage",
        "",
        f"kind: {result.kind}",
        f"size: {result.size}",
        f"risk: {result.risk}",
        f"touches_ui: {str(result.touches_ui).lower()}",
        f"profile: {result.profile_name or '-'}",
        f"budget_usd: {result.budget_usd if result.budget_usd is not None else '-'}",
        f"reviewers: {', '.join(result.reviewers)}",
        f"require_human_approval: {str(result.require_human_approval).lower()}",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(reasons or ["- no rule matched; defaults apply"])
    lines += ["", "## Source", "", "| question | value | source |", "| --- | --- | --- |"]
    for q in _QUESTIONS:
        v = getattr(result, q)
        shown = str(v).lower() if isinstance(v, bool) else v
        lines.append(f"| {q} | {shown} | {result.source.get(q, 'rules')} |")
    return "\n".join(lines) + "\n"


def triage_to_json(r: TriageResult) -> dict[str, Any]:
    return {
        "kind": r.kind,
        "size": r.size,
        "risk": r.risk,
        "touches_ui": r.touches_ui,
        "source": dict(r.source),
        "profile_name": r.profile_name,
        "budget_usd": str(r.budget_usd) if r.budget_usd is not None else None,
        "reviewers": list(r.reviewers),
        "require_human_approval": r.require_human_approval,
    }


def triage_from_json(d: Mapping[str, Any] | str) -> TriageResult:
    if isinstance(d, str):
        d = json.loads(d)
    assert not isinstance(d, str)
    b = d.get("budget_usd")
    return TriageResult(
        kind=d["kind"],
        size=d["size"],
        risk=d["risk"],
        touches_ui=bool(d["touches_ui"]),
        source=dict(d.get("source", {})),
        profile_name=d.get("profile_name"),
        budget_usd=Decimal(str(b)) if b is not None else None,
        reviewers=tuple(d.get("reviewers", DEFAULT_REVIEWERS)),
        require_human_approval=bool(d.get("require_human_approval", False)),
    )


def reviewers_for(app: AppDef, triage: TriageResult, diff_paths: Sequence[str]) -> ReviewerPlan:
    chosen = set(triage.reviewers or app.reviewers)
    chosen.add("security")
    ui_changed = any(matches_glob(p, g) for p in diff_paths for g in app.ui_globs)
    if ui_changed:
        chosen.add("ui")
    skipped: dict[str, str] = {}
    if app.reviewer_relevance and not ui_changed and not triage.touches_ui:
        for k in ("ux", "ui"):
            if k in chosen:
                chosen.discard(k)
                skipped[k] = "no UI files changed"
    run = tuple(k for k in DEFAULT_REVIEWERS if k in chosen)
    return ReviewerPlan(run=run, skipped=skipped)


LlmStep = Callable[..., Awaitable["StepOutcome"]]


class TriageService:
    def __init__(
        self,
        definitions: DefinitionStore,
        artifacts: ArtifactStore,
        router: RouterClient | None = None,
        llm_step: LlmStep | None = None,
    ) -> None:
        self.definitions = definitions
        self.artifacts = artifacts
        self.router = router
        self.llm_step = llm_step

    async def _run_llm(
        self, job: Job, questions: Sequence[str]
    ) -> tuple[dict[str, Any], str | None, str | None, int]:
        """(values, error, run_id, latency_ms) of one triage step; never raises."""
        started = time.monotonic()
        run_id: str | None = None
        values: dict[str, Any] = {}
        error: str | None = None
        if self.llm_step is None:
            error = "no triage model"
        else:
            try:
                out = await self.llm_step(
                    job_id=job.id,
                    role=Role.TRIAGE,
                    round=1,
                    inputs=["feature.md", "app.md"],
                    output_name="triage-llm.md",
                    workdir=None,
                    tool_policy=ToolPolicy.NONE,
                )
                run_id = str(out.run_id)
                if out.status == RunStatus.OK:
                    parsed = parse_triage(out.text or "")
                    values = {q: parsed[q] for q in questions}
                else:
                    error = await self._run_error(out)
            except ContractError as e:
                error = f"contract: {e}"
            except Exception as e:  # budget, no model, engine: never fails the job
                error = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        return values, error, run_id, int((time.monotonic() - started) * 1000)

    async def _run_error(self, out: StepOutcome) -> str:
        from swfactory.db.tables import AgentRun

        try:
            async with self.definitions.sessions() as s:
                row = await s.get(AgentRun, out.run_id)
            if row is not None and row.error:
                return row.error.strip().splitlines()[0][:200]
        except Exception as e:
            log.info("triage llm error lookup failed: %s", e)
        problem = check_output("triage", out.text)
        return f"contract: {problem}" if out.text is not None and problem else "step failed"

    async def triage(
        self,
        job: Job,
        app: AppDef,
        *,
        modes: Mapping[str, TriageMode] | None = None,
        pins: Mapping[str, int] | None = None,
    ) -> TriageResult:
        config = TriageConfig()
        if job.definition_sha:
            defn = await self.definitions.get(job.definition_sha)
            if defn is not None:
                config = defn.factory.triage
        eff: dict[str, TriageMode] = {
            q: {**config.modes, **(modes or {})}.get(q, config.mode) for q in _QUESTIONS
        }
        tri = job.triage if isinstance(job.triage, dict) else {}
        labels = [str(x) for x in tri.get("labels", [])]
        result, reasons, set_keys = _triage_by_rules(
            config, app, text=f"{job.title}\n{job.feature}", labels=labels
        )
        laya_qs = [q for q in _QUESTIONS if eff[q] in (TriageMode.SHADOW, TriageMode.LAYA)]
        answers: dict[str, LayaAnswer] = {}
        state: dict[str, Any] = {}
        if laya_qs and self.router is not None:
            state = triage_state(job, app)
            answers = await self.router.decide(
                state, {q: TRIAGE_QUESTIONS[q] for q in laya_qs}, pins=pins
            )
        elif laya_qs:
            from swfactory.models.router_client import LayaAnswer as _LA

            answers = {q: _LA(None, {}, None, 0, "unreachable") for q in laya_qs}
        llm_qs = [q for q in _QUESTIONS if eff[q] == TriageMode.LLM]
        llm_values: dict[str, Any] = {}
        llm_error: str | None = None
        run_id: str | None = None
        latency = 0
        if llm_qs:
            llm_values, llm_error, run_id, latency = await self._run_llm(job, llm_qs)
        rule_values = {q: getattr(result, q) for q in _QUESTIONS if q in set_keys}
        values, source, why = merge_triage(
            rule_values, _DEFAULTS, answers, eff, llm_values=llm_values, llm_error=llm_error
        )
        # a question no rule set takes the merged value; a rule-set one already holds it
        merged = replace(
            result,
            source=source,
            kind=values["kind"],
            size=values["size"],
            risk=values["risk"],
            touches_ui=bool(values["touches_ui"]),
        )
        if llm_qs:
            async with self.definitions.sessions() as s, s.begin():
                for q in llm_qs:
                    crit = TRIAGE_QUESTIONS[q]["criteria"]
                    value = values[q]
                    chosen = (
                        next((k for k, v in TOUCHES_UI_LABELS.items() if v is value), None)
                        if q == "touches_ui"
                        else str(value)
                    )
                    await insert_model_decision(
                        s,
                        job_id=job.id,
                        role="triage",
                        round=None,
                        mode="llm",
                        options=[{"label": k, "description": d} for k, d in crit.items()],
                        chosen=chosen,
                        reason=why[q],
                        latency_ms=latency,
                        purpose="triage",
                        answer={
                            "question": q,
                            "value": values[q] if source[q] == "llm" else None,
                            "run_id": run_id,
                        },
                    )
        if answers and self.router is not None:
            async with self.definitions.sessions() as s, s.begin():
                for q in laya_qs:
                    a = answers[q]
                    crit = TRIAGE_QUESTIONS[q]["criteria"]
                    value = values[q]
                    await insert_model_decision(
                        s,
                        job_id=job.id,
                        role="triage",
                        round=None,
                        mode=eff[q].value,
                        options=[{"label": k, "description": d} for k, d in crit.items()],
                        chosen=(
                            next((k for k, v in TOUCHES_UI_LABELS.items() if v is value), None)
                            if q == "touches_ui"
                            else str(value)
                        ),
                        reason=why[q],
                        latency_ms=a.latency_ms,
                        purpose="triage",
                        answer={
                            "question": q,
                            "state": state,
                            "choice": a.choice,
                            "probabilities": dict(a.probabilities),
                            "route": a.route,
                            "error": a.error,
                        },
                        router_version=a.router_version,
                        head_version=a.head_version,
                    )
        await self.artifacts.put(job.id, "triage.md", render_triage_md(merged, reasons))
        return merged
