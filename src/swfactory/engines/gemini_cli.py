"""Gemini CLI engine adapter (optional): runs `gemini` headless behind a localhost forwarder."""

import json
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from swfactory.domain.types import AgentResult, AgentTask, RunStatus, ToolPolicy, Usage
from swfactory.engines.base import (
    TableSupport,
    classify_failure,
    collect_output,
    commit_env,
    engine_version,
    error_class_of,
    scrub,
)
from swfactory.sandbox.base import Sandbox

GEMINI_HOME = "/home/factory/gemini"
CONFIG_DIR = f"{GEMINI_HOME}/.gemini"
READ_TOOLS = ["read_file", "read_many_files", "list_directory", "glob", "grep_search"]

FORWARD_SCRIPT = r"""gw_host=$1 gw_port=$2 port=$3; shift 3
socat TCP-LISTEN:$port,bind=127.0.0.1,reuseaddr,fork TCP:$gw_host:$gw_port >/dev/null & fwd=$!
trap 'kill $fwd 2>/dev/null' EXIT
up=0; for _ in $(seq 25); do (exec 3<>/dev/tcp/127.0.0.1/$port) 2>/dev/null && { up=1; break; }; sleep 0.2; done
[ "$up" = 1 ] || { echo "factory: localhost forwarder did not start" >&2; exit 97; }
gemini "$@"
"""  # noqa: E501

_CLEAN = [
    "sh",
    "-c",
    'd="$1"; rm -f "$(dirname "$d")/.env"; [ -d "$d" ] || exit 0; '
    'find "$d" -mindepth 1 -maxdepth 1 ! -name tmp -exec rm -rf {} +',
    "gemini-clean",
    CONFIG_DIR,
]


def forward_port(run_id: UUID) -> int:
    return 20000 + run_id.int % 10000


def gemini_settings(task: AgentTask) -> dict[str, Any]:
    s: dict[str, Any] = {
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True},
        "context": {"fileName": ["AGENTS.md"]},
        "security": {"auth": {"selectedType": "gateway"}, "folderTrust": {"enabled": True}},
        "model": {"maxSessionTurns": task.max_turns},
        "telemetry": {"enabled": False},
        "experimental": {"enableAgents": False},
    }
    if task.tool_policy is ToolPolicy.READ_RUN:
        s["tools"] = {"core": [*READ_TOOLS, "run_shell_command"]}
    elif task.tool_policy is not ToolPolicy.WRITE:
        s["tools"] = {"core": list(READ_TOOLS)}
    return s


def _policy(tools: list[str]) -> str:
    return "".join(
        f'[[rule]]\ntoolName = "{t}"\ndecision = "deny"\npriority = 999\n\n' for t in tools
    )


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


class GeminiCliEngine(TableSupport):
    name = "gemini_cli"

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    async def _prepare(self, task: AgentTask, box: Sandbox) -> bool:
        res = await box.exec(_CLEAN, timeout_s=60)
        if res.exit_code != 0:
            return False
        await box.write_text(
            f"{CONFIG_DIR}/settings.json", json.dumps(gemini_settings(task), indent=2)
        )
        await box.write_text(f"{CONFIG_DIR}/trustedFolders.json", "{}")
        if task.tool_policy is ToolPolicy.READ_RUN:
            await box.write_text(
                f"{CONFIG_DIR}/policies/factory-read-run.toml",
                _policy(["write_file", "replace"]),
            )
        elif task.tool_policy is not ToolPolicy.WRITE:
            await box.write_text(
                f"{CONFIG_DIR}/policies/factory-read-only.toml",
                _policy(["write_file", "replace", "run_shell_command"]),
            )
        return True

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        assert box is not None, "gemini_cli needs a sandbox"
        version = await engine_version(box, "gemini_cli")

        def fail(
            status: RunStatus,
            error: str | None,
            meta: dict[str, Any],
            *,
            transcript: str | None = None,
            session: str | None = None,
            usage: Usage | None = None,
            models: tuple[str, ...] = (),
        ) -> AgentResult:
            return AgentResult(
                status, None, session, usage or Usage(), models, None, transcript, error,
                version, meta,
            )  # fmt: skip

        if not await self._prepare(task, box):
            return fail(
                RunStatus.FAILED, "could not reset the Gemini CLI config", {"reason": "config"}
            )

        gw = urlparse(task.gateway_base_url)
        gw_port = gw.port or (443 if gw.scheme == "https" else 80)
        port = forward_port(task.run_id)
        mode = "yolo" if task.tool_policy in (ToolPolicy.WRITE, ToolPolicy.READ_RUN) else "plan"
        argv = [
            "bash", "-c", FORWARD_SCRIPT, "gemini-run", gw.hostname or "", str(gw_port), str(port),
            "-p", "Follow the instructions above.",
            "-m", task.model.alias,
            "-o", "json",
            "--approval-mode", mode,
            "--skip-trust",
            "--allowed-mcp-server-names", "",
        ]  # fmt: skip
        if task.resume_session:
            argv += ["-r", task.resume_session]
        env = {
            **commit_env(task),
            "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
            "GEMINI_API_KEY": task.gateway_key,
            "GEMINI_CLI_HOME": GEMINI_HOME,
        }
        res = await box.exec(
            argv,
            cwd=task.workdir or "/work/repo",
            env=env,
            timeout_s=task.timeout_s,
            stdin=task.system_prompt + "\n\n" + task.prompt,
        )
        transcript = scrub(res.stdout, task)
        if res.timed_out:
            return fail(RunStatus.TIMEOUT, "timed out", {}, transcript=transcript)

        doc: dict[str, Any] | None
        try:
            parsed = json.loads(res.stdout)
            doc = parsed if isinstance(parsed, dict) else None
        except ValueError:
            doc = None
        err = (doc or {}).get("error")
        err_msg = err.get("message", "") if isinstance(err, dict) else ""
        code = res.exit_code

        if code != 0:
            text = scrub(f"{res.stderr}\n{err_msg}".strip(), task)
            meta: dict[str, Any] = {}
            status = RunStatus.FAILED
            if code == 97:
                meta = {"reason": "forwarder", "error_class": "gateway"}
            elif code == 137:
                meta = {"reason": "killed"}
            elif code == 1:
                status = classify_failure(text)
                cls = error_class_of(text)
                if cls:
                    meta["error_class"] = cls
            elif code == 42:
                text = text or "input error"
            elif code == 53:
                meta = {"reason": "turn_limit"}
            else:
                meta = {"reason": f"exit {code}"}
            return fail(
                status, text or None, meta, transcript=transcript,
                session=(doc or {}).get("session_id"),
            )  # fmt: skip

        if doc is None:
            return fail(
                RunStatus.FAILED, "unparseable output", {"reason": "unparseable output"},
                transcript=transcript,
            )  # fmt: skip

        models = (doc.get("stats") or {}).get("models") or {}
        used: list[str] = list(models)
        init_model = (doc.get("init") or {}).get("model")
        if init_model and init_model not in used:
            used.append(init_model)
        tok = [m.get("tokens") or {} for m in models.values() if isinstance(m, dict)]
        usage = Usage(
            input_tokens=sum(_int(t.get("prompt")) for t in tok),
            output_tokens=sum(_int(t.get("candidates")) for t in tok),
            cache_read_tokens=sum(_int(t.get("cached")) for t in tok),
        )
        response = doc.get("response")
        output = await collect_output(box, task, response if isinstance(response, str) else None)
        return AgentResult(
            RunStatus.OK, output, doc.get("session_id"), usage, tuple(used),
            None, transcript, None, version, {},
        )  # fmt: skip
