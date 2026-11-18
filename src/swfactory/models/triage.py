"""Rules-based triage and the hard reviewer rules."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from swfactory.definition.models import AppDef, TriageConfig, TriageRule
from swfactory.domain.types import DEFAULT_REVIEWERS, TriageMode

if TYPE_CHECKING:
    from swfactory.artifacts import ArtifactStore
    from swfactory.db.tables import Job
    from swfactory.definition.store import DefinitionStore

log = logging.getLogger(__name__)

_QUESTIONS = ("kind", "size", "risk", "touches_ui")


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
    return result, reasons


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


class TriageService:
    def __init__(self, definitions: DefinitionStore, artifacts: ArtifactStore) -> None:
        self.definitions = definitions
        self.artifacts = artifacts

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
        wanted = {config.mode, *(modes or {}).values()} - {TriageMode.RULES}
        for m in sorted(wanted, key=str):
            log.info("triage mode %s not available before M11", m)
        tri = job.triage if isinstance(job.triage, dict) else {}
        labels = [str(x) for x in tri.get("labels", [])]
        result, reasons = triage_by_rules(
            config, app, text=f"{job.title}\n{job.feature}", labels=labels
        )
        await self.artifacts.put(job.id, "triage.md", render_triage_md(result, reasons))
        return result
