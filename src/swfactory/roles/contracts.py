"""Strict output contracts for role outputs: verdicts, decisions, specs and requirements."""

import re
from typing import Any

import yaml

from swfactory.backlogs import RequirementItem
from swfactory.domain.types import Decision, Verdict

_STRIP = " \t#>*_"
_VERDICT_RE = re.compile(r"VERDICT:[ \t]*(PASS|CHANGES)", re.I)
_DECISION_RE = re.compile(r"(APPROVE|ESCALATE)", re.I)
_YAML_BLOCK_RE = re.compile(r"^```(?:yaml|yml)[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_REQUIRED_HEADINGS = ("## Summary", "## Requirements", "## Out of scope", "## Backlog")


class ContractError(ValueError):
    """The output does not follow its contract."""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip(_STRIP)
    return ""


def parse_verdict(text: str) -> Verdict:
    m = _VERDICT_RE.fullmatch(_first_line(text))
    if m is None:
        raise ContractError(
            "the first non-empty line must be 'VERDICT: PASS' or 'VERDICT: CHANGES'"
        )
    return Verdict(m.group(1).upper())


def parse_decision(text: str) -> Decision:
    m = _DECISION_RE.fullmatch(_first_line(text))
    if m is None:
        raise ContractError("the first non-empty line must be 'APPROVE' or 'ESCALATE'")
    return Decision(m.group(1).upper())


def _is_heading(line: str, heading: str) -> bool:
    return line.strip().lower() == heading.lower()


def _section(text: str, heading: str) -> str | None:
    """The text after the heading line up to the next '## ' heading, or None if absent."""
    out: list[str] | None = None
    for line in text.splitlines():
        if out is None:
            if _is_heading(line, heading):
                out = []
        elif line.strip().startswith("## "):
            break
        else:
            out.append(line)
    return None if out is None else "\n".join(out)


def check_spec(text: str) -> list[str]:
    section = _section(text, "## Acceptance criteria")
    if section is None:
        return ["missing '## Acceptance criteria' section"]
    if not any(line.lstrip().startswith("- [ ]") for line in section.splitlines()):
        return ["no '- [ ]' item under '## Acceptance criteria'"]
    return []


def _text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _load_requirements(text: str, max_items: int) -> tuple[list[str], list[RequirementItem]]:
    problems = [f"missing '{h}' section" for h in _REQUIRED_HEADINGS if _section(text, h) is None]
    backlog = _section(text, "## Backlog")
    if backlog is None:
        return problems, []
    blocks = _YAML_BLOCK_RE.findall(backlog)
    if len(blocks) != 1:
        return [*problems, "'## Backlog' must hold exactly one yaml block"], []
    try:
        data = yaml.safe_load(blocks[0])
    except yaml.YAMLError:
        return [*problems, "the backlog block is not valid yaml"], []
    if not isinstance(data, list):
        return [*problems, "the backlog must be a yaml list of items"], []
    if not data:
        return [*problems, "the backlog has no items"], []
    if len(data) > max_items:
        return [
            *problems,
            f"the backlog has {len(data)} items; at most {max_items} are allowed",
        ], []

    items: list[RequirementItem] = []
    ids: list[str] = []
    for n, raw in enumerate(data, 1):
        if not isinstance(raw, dict):
            problems.append(f"item {n} is not a mapping")
            continue
        iid = raw.get("id")
        if not _text(iid):
            problems.append(f"item {n} is missing 'id'")
            continue
        iid = str(iid).strip()
        ok = True
        for key in ("title", "feature"):
            if key not in raw:
                problems.append(f"item '{iid}' is missing '{key}'")
                ok = False
            elif not _text(raw[key]):
                problems.append(f"item '{iid}' has an invalid '{key}'")
                ok = False
        for key in ("depends_on", "acceptance"):
            if key not in raw:
                problems.append(f"item '{iid}' is missing '{key}'")
                ok = False
            elif not isinstance(raw[key], list) or not all(_text(x) for x in raw[key]):
                problems.append(f"item '{iid}' has an invalid '{key}'")
                ok = False
        if iid in ids:
            problems.append(f"duplicate id '{iid}'")
            ok = False
        if ok:
            deps = tuple(str(x).strip() for x in raw["depends_on"])
            bad = [d for d in deps if d not in ids]
            for d in bad:
                problems.append(f"item '{iid}' depends on '{d}', which is not an earlier item")
            if not raw["acceptance"]:
                problems.append(f"item '{iid}' has no acceptance criterion")
            items.append(
                RequirementItem(
                    id=iid,
                    title=str(raw["title"]).strip(),
                    feature=str(raw["feature"]).strip(),
                    depends_on=deps,
                    acceptance=tuple(str(x).strip() for x in raw["acceptance"]),
                )
            )
        ids.append(iid)
    return problems, items


def check_requirements(text: str, *, max_items: int = 30) -> list[str]:
    return _load_requirements(text, max_items)[0]


def parse_requirements(text: str, *, max_items: int = 30) -> list[RequirementItem]:
    problems, items = _load_requirements(text, max_items)
    if problems:
        raise ContractError(problems[0])
    return items


def check_output(contract: str, text: str | None, *, max_items: int = 30) -> str | None:
    """A problem sentence for the output, or None when it follows the contract."""
    if text is None:
        return "no output file was written"
    if not text.strip():
        return "the output is empty"
    try:
        if contract == "verdict":
            parse_verdict(text)
        elif contract == "decision":
            parse_decision(text)
        elif contract == "spec":
            problems = check_spec(text)
            return "; ".join(problems) or None
        elif contract == "requirements":
            problems = check_requirements(text, max_items=max_items)
            return "; ".join(problems) or None
    except ContractError as e:
        return str(e)
    return None
