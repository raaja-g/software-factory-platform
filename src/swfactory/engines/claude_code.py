"""Claude Code adapter (gateway mode): `claude --bare -p` with stream-json output."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from swfactory.domain.types import AgentResult, AgentTask, RunStatus, ToolPolicy, Usage
from swfactory.engines.base import (
    TableSupport,
    collect_output,
    commit_env,
    engine_version,
    error_class_of,
    json_lines,
    scrub,
)
from swfactory.models.profiles import model_family
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import HOME
from swfactory.settings import Settings

CONFIG_DIR = f"{HOME}/claude"
TOTALS_DIR = f"{CONFIG_DIR}/factory-totals"
READ_ONLY_TOOLS = ["Read", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
                   "Bash(git status *)"]  # fmt: skip
NO_EDIT = ["Edit", "Write", "NotebookEdit"]
# git accepts abbreviated long options, so these are prefixes: --ou (--output), --ext (--ext-diff),
# --open (--open-files-in-pager). They write a file or run a program, so they are denied even
# inside the read-only git allowlist
GIT_UNSAFE = ["Bash(git * --ou*)", "Bash(git * --ext*)", "Bash(git * --open*)"]


@dataclass(frozen=True, slots=True)
class ClaudeRun:
    status: RunStatus | None  # None: no parseable result line
    text: str | None
    session_id: str | None
    usage: Usage
    used_models: tuple[str, ...]
    cost: Decimal | None
    error: str | None = None
    subtype: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def parse_stream(stdout: str) -> ClaudeRun:
    result: dict[str, Any] | None = None
    for line in json_lines(stdout):
        if line.get("type") == "result":
            result = line
    if result is None:
        return ClaudeRun(None, None, None, Usage(), (), None)
    mu = result.get("modelUsage")
    mu = mu if isinstance(mu, dict) else {}
    usage = Usage(
        input_tokens=sum(_int(m.get("inputTokens")) for m in mu.values()),
        output_tokens=sum(_int(m.get("outputTokens")) for m in mu.values()),
        cache_read_tokens=sum(_int(m.get("cacheReadInputTokens")) for m in mu.values()),
        cache_write_tokens=sum(_int(m.get("cacheCreationInputTokens")) for m in mu.values()),
    )
    subtype = result.get("subtype")
    if subtype == "success" and not result.get("is_error"):
        status = RunStatus.OK
    elif subtype == "error_max_budget_usd":
        status = RunStatus.BUDGET
    else:
        status = RunStatus.FAILED
    cost = result.get("total_cost_usd")
    text = result.get("result")
    sid = result.get("session_id")
    return ClaudeRun(
        status=status,
        text=text if isinstance(text, str) else None,
        session_id=sid if isinstance(sid, str) else None,
        usage=usage,
        used_models=tuple(mu),
        cost=Decimal(str(cost)) if isinstance(cost, int | float) else None,
        subtype=subtype if isinstance(subtype, str) else None,
        raw=result,
    )


def claude_argv(task: AgentTask) -> list[str]:
    a = ["claude", "--bare", "-p", "--output-format", "stream-json", "--verbose",
         "--model", task.model.alias, "--permission-mode", "dontAsk"]  # fmt: skip
    match task.tool_policy:
        case ToolPolicy.WRITE:
            a += ["--allowedTools", "Read", "Edit", "Bash"]
        case ToolPolicy.READ_ONLY:
            a += ["--allowedTools", *READ_ONLY_TOOLS, "--disallowedTools", *NO_EDIT, *GIT_UNSAFE]
        case ToolPolicy.READ_RUN:
            a += ["--allowedTools", "Read", "Grep", "Glob", "Bash", "--disallowedTools", *NO_EDIT]
        case ToolPolicy.NONE:
            a += ["--tools", ""]
    a += ["--max-turns", str(task.max_turns)]
    if task.max_budget_usd is not None:
        a += ["--max-budget-usd", f"{task.max_budget_usd:f}"]
    a += ["--setting-sources", "user", "--strict-mcp-config",
          "--append-system-prompt-file", _system_path(task),
          "--settings", '{"cleanupPeriodDays": 3650}']  # fmt: skip
    if task.resume_session:
        a += ["--resume", task.resume_session]
    else:
        a += ["--session-id", str(task.run_id)]
    return a


def _system_path(task: AgentTask) -> str:
    return f"{CONFIG_DIR}/runs/{task.run_id}/system.md"


def claude_env(task: AgentTask) -> dict[str, str]:
    alias = task.model.alias
    env = {
        "ANTHROPIC_BASE_URL": task.gateway_base_url,
        "ANTHROPIC_AUTH_TOKEN": task.gateway_key,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": alias,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": alias,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias,
        "CLAUDE_CODE_SUBAGENT_MODEL": alias,
        "CLAUDE_CONFIG_DIR": CONFIG_DIR,
        "CLAUDE_CODE_PROJECT_DIR_NAME": str(task.job_id),
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    if model_family(task.model.upstream_id) != "claude":
        env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
    return {**env, **commit_env(task)}


def _totals_path(session_id: str) -> str:
    return f"{TOTALS_DIR}/{session_id}.json"


async def _load_totals(box: Sandbox, session_id: str) -> tuple[Decimal, Usage] | None:
    raw = await box.read_text(_totals_path(session_id))
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return Decimal(str(d["cost"])), Usage(
            _int(d["input"]), _int(d["output"]), _int(d["cache_read"]), _int(d["cache_write"])
        )
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None


async def _store_totals(box: Sandbox, session_id: str, cost: Decimal, u: Usage) -> None:
    await box.write_text(
        _totals_path(session_id),
        json.dumps({"cost": str(cost), "input": u.input_tokens, "output": u.output_tokens,
                    "cache_read": u.cache_read_tokens, "cache_write": u.cache_write_tokens}),
    )  # fmt: skip


class ClaudeCodeEngine(TableSupport):
    name = "claude_code"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        assert box is not None, "claude_code needs a sandbox"
        version = await engine_version(box, self.name)
        await box.write_text(_system_path(task), task.system_prompt)
        res = await box.exec(
            claude_argv(task),
            cwd=task.workdir or "/work/repo",
            env=claude_env(task),
            timeout_s=task.timeout_s,
            stdin=task.prompt,
        )
        transcript = scrub(res.stdout, task)
        stderr = scrub(res.stderr, task)

        def fail(
            status: RunStatus, error: str | None, reason: str | None, run: ClaudeRun | None = None
        ):
            meta: dict[str, Any] = {}
            if reason:
                meta["reason"] = reason
            if error:
                cls = error_class_of(error)
                if cls:
                    meta["error_class"] = cls
            return AgentResult(
                status=status, output=None, session_id=run.session_id if run else None,
                usage=run.usage if run else Usage(), used_models=run.used_models if run else (),
                reported_cost_usd=run.cost if run else None, transcript=transcript,
                error=error, engine_version=version, meta=meta,
            )  # fmt: skip

        run = parse_stream(res.stdout)
        if res.timed_out:
            return fail(RunStatus.TIMEOUT, "timed out", None, run if run.status else None)
        if res.exit_code == 137:
            return fail(RunStatus.FAILED, stderr or "killed", "killed")
        if run.status is None:
            return fail(RunStatus.FAILED, stderr or "unparseable output", "unparseable output")

        cost, usage = run.cost, run.usage
        sid = run.session_id
        if sid and cost is not None:
            if task.resume_session:
                prev = await _load_totals(box, sid)
                if prev is not None:
                    pc, pu = prev
                    cost = max(cost - pc, Decimal(0))
                    usage = Usage(
                        max(run.usage.input_tokens - pu.input_tokens, 0),
                        max(run.usage.output_tokens - pu.output_tokens, 0),
                        max(run.usage.cache_read_tokens - pu.cache_read_tokens, 0),
                        max(run.usage.cache_write_tokens - pu.cache_write_tokens, 0),
                    )
            await _store_totals(box, sid, run.cost or Decimal(0), run.usage)

        error = None
        output = None
        meta: dict[str, Any] = {}
        if run.status is RunStatus.OK:
            output = await collect_output(box, task, run.text)
        else:
            error = scrub(run.text, task) if run.text else (stderr or run.subtype)
            if error:
                cls = error_class_of(error)
                if cls:
                    meta["error_class"] = cls
        return AgentResult(
            status=run.status, output=output, session_id=sid, usage=usage,
            used_models=run.used_models, reported_cost_usd=cost, transcript=transcript,
            error=error, engine_version=version, meta=meta,
        )  # fmt: skip
