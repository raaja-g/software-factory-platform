"""OpenCode adapter: `opencode run --format json` through the gateway."""

import json
from dataclasses import dataclass, field
from typing import Any

from swfactory.domain.types import AgentResult, AgentTask, RunStatus, ToolPolicy, Usage
from swfactory.engines.base import (
    TableSupport,
    classify_failure,
    collect_output,
    commit_env,
    engine_version,
    error_class_of,
    json_lines,
    scrub,
)
from swfactory.models.profiles import model_family
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import HOME, OUT, REPO

OPENCODE_CACHE = "/opt/factory-engines/opencode-cache"
OPENCODE_HOME_CACHE = "/home/factory/.cache/opencode"
_GIT_READ_SUBCOMMANDS = ("diff", "log", "show", "status")
# Deny rules come after the allow rules: the last matching rule wins. git accepts any unambiguous
# abbreviation of a long option, so options that write a file or run a program are denied by
# prefix (--ou matches --output, --ext matches --ext-diff, --open matches --open-files-in-pager),
# as are shell chaining, redirection and control characters.
_GIT_READ_DENY = ("*--ou*", "*--ext*", "*--open*", "*;*", "*&*", "*|*", "*`*", "*$(*", "*>*", "*<*",
                  "*\n*", "*\r*")  # fmt: skip
_GIT_READ = {
    "*": "deny",
    **{p: "allow" for sub in _GIT_READ_SUBCOMMANDS for p in (f"git {sub}", f"git {sub} *")},
    **{p: "deny" for p in _GIT_READ_DENY},
}


@dataclass
class OpenCodeRun:
    text: str | None = None
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    models: tuple[str, ...] = ()
    error: str | None = None
    complete: bool = False


def opencode_permission(policy: ToolPolicy) -> dict[str, Any]:
    if policy is ToolPolicy.WRITE:
        # A write role puts its output file in OUT, which is outside the repo it works in, and
        # OpenCode auto-rejects a tool call outside the project ("permission requested:
        # external_directory ... auto-rejecting"): the builder committed its code and then could not
        # write build.md, so every build failed the output contract. Allow that one directory only.
        return {
            "edit": "allow",
            "bash": "allow",
            "webfetch": "deny",
            "external_directory": {f"{OUT}/*": "allow"},
        }
    if policy is ToolPolicy.READ_RUN:
        return {"edit": "deny", "webfetch": "deny", "bash": "allow"}
    if policy is ToolPolicy.READ_ONLY:
        return {"edit": "deny", "webfetch": "deny", "bash": dict(_GIT_READ)}
    return {"edit": "deny", "webfetch": "deny", "bash": "deny"}


def _runs_dir(task: AgentTask) -> str:
    return f"{HOME}/opencode/runs/{task.run_id}"


# The most output OpenCode may ask a model for in one reply. A model that declares no limit gets
# OpenCode's default (about 32k tokens), which Groq's qwen refuses ("max_tokens must be less than
# or equal to 16384"), and a refused reply fails the step in seconds. Context 0 keeps OpenCode's
# behaviour for unknown models (no context check); only the output ask is bounded.
OUTPUT_TOKEN_LIMIT = 16000


def model_entry(alias: str) -> dict[str, Any]:
    return {"name": alias, "limit": {"context": 0, "output": OUTPUT_TOKEN_LIMIT}}


def opencode_config(task: AgentTask) -> dict[str, Any]:
    anthropic = model_family(task.model.upstream_id) == "claude"
    key = "factory-anthropic" if anthropic else "factory"
    alias = task.model.alias
    provider: dict[str, Any] = {
        "npm": "@ai-sdk/anthropic" if anthropic else "@ai-sdk/openai-compatible",
        "name": "Factory gateway (Anthropic format)" if anthropic else "Factory gateway",
        "options": {
            "baseURL": f"{task.gateway_base_url}/v1",
            "apiKey": "{env:FACTORY_GATEWAY_KEY}",
        },
        "models": {alias: model_entry(alias)},
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "provider": {key: provider},
        "agent": {
            "factory": {"prompt": "{file:" + _runs_dir(task) + "/system.md}", "mode": "primary"}
        },
    }


def parse_events(stdout: str) -> OpenCodeRun:
    run = OpenCodeRun()
    models: list[str] = []
    inp = out = cr = cw = 0
    for ev in json_lines(stdout):
        kind = ev.get("type")
        raw_part = ev.get("part")
        part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
        if isinstance(ev.get("sessionID"), str):
            run.session_id = ev["sessionID"]
        model = part.get("modelID")
        if isinstance(model, str) and model not in models:
            models.append(model)
        if kind == "text":
            run.text = part.get("text", "")
            run.complete = True
        elif kind == "step_finish":
            run.complete = True
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            inp += int(tok.get("input") or 0)
            out += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
            cr += int(cache.get("read") or 0)
            cw += int(cache.get("write") or 0)
        elif kind == "error":
            err = ev.get("error")
            data = err.get("data") if isinstance(err, dict) else None
            msg = data.get("message") if isinstance(data, dict) else None
            if not msg and isinstance(err, dict):
                msg = err.get("message") or err.get("name")
            run.error = str(msg or err or "opencode error")
    run.usage = Usage(inp, out, cr, cw)
    run.models = tuple(models)
    return run


class OpenCodeEngine(TableSupport):
    name = "opencode"

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        assert box is not None
        version = await engine_version(box, "opencode")
        try:
            seed = await box.exec(
                ["sh", "-c", 'mkdir -p "$2" && cp -an "$1"/. "$2"/', "seed",
                 OPENCODE_CACHE, OPENCODE_HOME_CACHE],
                timeout_s=120,
            )  # fmt: skip
            if seed.exit_code != 0:
                import logging

                logging.getLogger(__name__).warning("opencode cache copy failed: %s", seed.stderr)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("opencode cache copy failed", exc_info=True)
        rd = _runs_dir(task)
        await box.write_text(f"{rd}/opencode.json", json.dumps(opencode_config(task)))
        await box.write_text(f"{rd}/system.md", task.system_prompt)
        provider = (
            "factory-anthropic" if model_family(task.model.upstream_id) == "claude" else "factory"
        )
        argv = ["opencode", "run", "--format", "json", "-m", f"{provider}/{task.model.alias}",
                "--agent", "factory"]  # fmt: skip
        if task.resume_session:
            argv += ["--session", task.resume_session]
        oc = f"{HOME}/opencode"
        env = {
            "OPENCODE_CONFIG": f"{rd}/opencode.json",
            "OPENCODE_PERMISSION": json.dumps(opencode_permission(task.tool_policy)),
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "FACTORY_GATEWAY_KEY": task.gateway_key,
            "HOME": HOME,
            "XDG_DATA_HOME": f"{oc}/data",
            "XDG_CONFIG_HOME": f"{oc}/config",
            "XDG_STATE_HOME": f"{oc}/state",
            "XDG_CACHE_HOME": f"{HOME}/.cache",
            **commit_env(task),
        }
        res = await box.exec(
            argv, cwd=task.workdir or REPO, env=env, timeout_s=task.timeout_s, stdin=task.prompt
        )
        parsed = parse_events(res.stdout)
        transcript = scrub(res.stdout, task)

        def result(status: RunStatus, output: str | None = None, error: str | None = None,
                   **meta: Any) -> AgentResult:  # fmt: skip
            return AgentResult(
                status=status, output=output, session_id=parsed.session_id, usage=parsed.usage,
                used_models=parsed.models, reported_cost_usd=None, transcript=transcript,
                error=scrub(error, task) if error else None, engine_version=version, meta=meta,
            )  # fmt: skip

        if res.timed_out:
            return result(RunStatus.TIMEOUT, error="timed out")
        if res.exit_code == 137:
            return result(RunStatus.FAILED, error="killed", reason="killed")
        if res.exit_code != 0 or parsed.error:
            text = parsed.error or res.stderr.strip() or f"opencode exited {res.exit_code}"
            status = classify_failure(text)
            meta: dict[str, Any] = {"error_class": error_class_of(text)}
            return result(status, error=text, **meta)
        if not parsed.complete:
            return result(RunStatus.FAILED, error="unparseable output", reason="unparseable output")
        output = await collect_output(box, task, parsed.text)
        return result(RunStatus.OK, output=output)
