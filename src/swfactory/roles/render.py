"""Render a role's system prompt and job prompt, trimming inputs to a token budget."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from swfactory.roles.fence import fence_untrusted
from swfactory.roles.library import PLACEHOLDERS, RoleSpec
from swfactory.sandbox.paths import IN, REPO

DEFAULT_CONTEXT_TOKENS = 32_000
PROMPT_SHARE = 0.75
DIFF_HEAD_LINES = 40

_ROUND_RE = re.compile(r"^round-(\d+)/")
_END_MARKER = "<<<END UNTRUSTED DATA"
_CUT_MARKER = "\n[... cut ...]\n"


@dataclass(frozen=True, slots=True)
class PromptContext:
    job_id: UUID
    app_slug: str
    round: int
    inputs: Mapping[
        str, str
    ]  # artifact path -> RAW text; render_prompt trims first, then fences each input
    output_path: str | None
    workdir: str | None
    context_tokens: int
    instructions: str = ""  # trusted text appended to the system prompt, never fenced or trimmed


def estimate_tokens(text: str) -> int:
    """Deliberately overestimates for code: one token per three bytes, plus one."""
    return len(text.encode()) // 3 + 1


@dataclass(slots=True)
class _FileDiff:
    path: str
    lines: list[str]
    added: int = 0
    removed: int = 0


def _parse_diff(diff: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = _FileDiff(m.group(2) if m else line[len("diff --git ") :], [line])
            files.append(cur)
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            new = lines[i + 1][4:].split("\t")[0]
            old = line[4:].split("\t")[0]
            path = old if new == "/dev/null" else new
            path = path[2:] if path.startswith(("a/", "b/")) else path
            if cur is None or len(cur.lines) > 1 and not _in_header(cur):
                cur = _FileDiff(path, [])
                files.append(cur)
            else:
                cur.path = path
            cur.lines += [line, lines[i + 1]]
            i += 2
            continue
        elif cur is not None:
            cur.lines.append(line)
            if line.startswith("+"):
                cur.added += 1
            elif line.startswith("-"):
                cur.removed += 1
        i += 1
    return files


def _in_header(f: _FileDiff) -> bool:
    """True while only header lines (diff --git, index, mode, ---/+++) have been seen."""
    return all(not ln.startswith("@@") for ln in f.lines)


def diffstat(diff: str) -> str:
    files = _parse_diff(diff)
    out = [f"{f.path} | {f.added + f.removed}" for f in files]
    ins = sum(f.added for f in files)
    dele = sum(f.removed for f in files)
    out.append(f"{len(files)} files changed, {ins} insertions(+), {dele} deletions(-)")
    return "\n".join(out)


def _cut_diff(files: list[_FileDiff], stat: str, k: int) -> str:
    parts = [stat]
    for f in files[:k]:
        parts.append("\n".join(f.lines[:DIFF_HEAD_LINES]))
    return "\n\n".join(parts)


def _system_prompt(spec: RoleSpec, ctx: PromptContext) -> str:
    out = (
        f"Write your output to {ctx.output_path} and nothing else."
        if ctx.output_path
        else "Reply with the output document only, no preamble."
    )
    values = {
        "output_instruction": out,
        "in_dir": IN,
        "workdir": ctx.workdir or REPO,
        "round": str(ctx.round),
    }
    text = spec.body
    for name in PLACEHOLDERS:
        text = text.replace("{{" + name + "}}", values[name])
    if ctx.instructions:
        text = f"{text}\n\n# Step instructions\n\n{ctx.instructions}"
    return text


def _assemble(ctx: PromptContext, inputs: Mapping[str, str], notes: list[str]) -> str:
    head = [
        "# Job",
        f"job: {ctx.job_id} · app: {ctx.app_slug} · round: {ctx.round}",
        f"output file: {ctx.output_path or '(reply inline)'}",
        f"working directory: {ctx.workdir or '(none)'}",
        "",
        "# Inputs",
    ]
    if not inputs:
        head.append("\n(none)")
    for path, text in inputs.items():
        head.append(f"\n## {path}\n{fence_untrusted(path, text)}")
    out = "\n".join(head) + "\n"
    if notes:
        out += "\n" + "\n".join(notes) + "\n"
    return out


def render_prompt(spec: RoleSpec, ctx: PromptContext) -> tuple[str, str]:
    system = _system_prompt(spec, ctx)
    budget = int(ctx.context_tokens * PROMPT_SHARE) - estimate_tokens(system)
    inputs: dict[str, str] = dict(ctx.inputs)
    dropped: list[str] = []
    diff_note: str | None = None
    trunc: dict[str, tuple[int, int]] = {}  # path -> (kept chars, original chars)
    cut_paths: list[str] = []
    texts_original: dict[str, str] = {}

    def notes() -> list[str]:
        out: list[str] = []
        if dropped:
            out.append(f"omitted: {', '.join(dropped)} (older review rounds)")
        if diff_note:
            out.append(diff_note)
        for p, (kept, orig) in trunc.items():
            out.append(f"omitted: {p} (truncated to {kept:,} of {orig:,} characters)")
        if out and ctx.workdir:
            out += [f"Full files: {IN}/{p}" for p in cut_paths]
        return out

    def size() -> int:
        return estimate_tokens(_assemble(ctx, inputs, notes()))

    def mark(path: str) -> None:
        if path not in cut_paths:
            cut_paths.append(path)

    # 1. drop the oldest review rounds first
    def groups() -> set[int]:
        return {int(m.group(1)) for p in inputs if (m := _ROUND_RE.match(p))}

    while size() > budget and len(groups()) > 1:
        oldest = min(groups())
        for p in [p for p in inputs if (m := _ROUND_RE.match(p)) and int(m.group(1)) == oldest]:
            del inputs[p]
            dropped.append(p)
            mark(p)

    # 2. cut diff.patch to a diffstat plus the head of each file
    if size() > budget and "diff.patch" in inputs:
        original = inputs["diff.patch"]
        files = _parse_diff(original)
        stat = diffstat(original)

        def attempt(k: int) -> tuple[str, str]:
            return (
                _cut_diff(files, stat, k),
                "omitted: diff.patch cut to a diffstat plus the first "
                f"{DIFF_HEAD_LINES} lines of {k} of {len(files)} files",
            )

        if len(attempt(0)[0]) < len(original):
            lo, hi = 0, len(files)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                inputs["diff.patch"], diff_note = attempt(mid)
                if size() <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            inputs["diff.patch"], diff_note = attempt(lo)
            mark("diff.patch")

    # 3. cut the largest remaining input to its head and tail
    for _ in range(200):
        over = size() - budget
        if over <= 0:
            break
        path = max(inputs, key=lambda p: len(inputs[p]))
        if not inputs[path]:
            break
        if path not in trunc:
            trunc[path] = (len(inputs[path]), len(inputs[path]))
            texts_original[path] = inputs[path]
        kept, orig = trunc[path]
        kept = max(0, kept - (over * 3 + 64))
        trunc[path] = (kept, orig)
        src = texts_original[path]
        half = kept // 2
        inputs[path] = src[:half] + _CUT_MARKER + src[len(src) - (kept - half) :] if kept else ""
        mark(path)
    return system, _assemble(ctx, inputs, notes())


def diff_was_cut(prompt: str) -> bool:
    idx = prompt.rfind(_END_MARKER)
    if idx < 0:
        return False
    tail = prompt[idx:].split("\n")[1:]
    return any(ln.startswith("omitted: diff.patch") for ln in tail)
