"""The job's ``feature.md`` and ``app.md`` inputs, rendered from the job row and its app."""

from __future__ import annotations

import re

from swfactory.definition.models import AppDef

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MAX_STACK_CHARS = 400


def render_feature_md(title: str, feature: str) -> str:
    return f"# {title}\n\n{feature}\n"


def stack_from_agents_md(text: str | None) -> str | None:
    """The body of the ``Stack`` section of an AGENTS.md as one line, or None."""
    if not text:
        return None
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if inside:
                break
            inside = m.group(1).strip().lower().startswith("stack")
            continue
        if inside and line.strip():
            body.append(line.strip().lstrip("-*").strip())
    joined = " ".join(b for b in body if b)
    return joined[:_MAX_STACK_CHARS] or None


def render_app_md(slug: str, app: AppDef, *, stack: str | None) -> str:
    smoke = getattr(app, "smoke", None)  # AppDef.smoke arrives with M4.7
    lines = [
        f"slug: {slug}",
        f"repo kind: {app.repo_kind}",
        f"default branch: {app.default_branch}",
        f"test command: {app.test_command or 'none'}",
        f"protected paths: {', '.join(app.protected_paths) or 'none'}",
        f"ui globs: {', '.join(app.ui_globs)}",
        f"smoke: {smoke or 'none'}",
        f"stack: {stack or 'unknown'}",
    ]
    return "\n".join(lines) + "\n"
