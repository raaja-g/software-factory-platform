from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.domain.types import AgentTask, ModelRef, Role, RunStatus, ToolPolicy
from swfactory.engines.base import fallback_detected
from swfactory.engines.claude_code import ClaudeCodeEngine, claude_argv, claude_env, parse_stream
from swfactory.gateway.client import model_error_class
from swfactory.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
CC = FIXTURES / "claude_code"
server_settings = Settings()


def usage_of(path: Path):
    return parse_stream(path.read_text()).usage


@pytest.fixture
def task():
    run_id = uuid4()
    return AgentTask(
        job_id=uuid4(), run_id=run_id, role=Role.BUILDER, round=1, attempt=1,
        engine="claude_code",
        model=ModelRef("claude-sonnet@vertex", "vertex", "claude-sonnet-4-6"),
        system_prompt="sys", prompt="do it", workdir="/work/repo",
        output_path=f"/work/.factory/out/{run_id}.md", tool_policy=ToolPolicy.WRITE,
        attachments=(), max_turns=80, timeout_s=60, resume_session=None,
        gateway_base_url="http://gateway-edge:4000", gateway_key="sk-run-test", tags={},
        max_budget_usd=Decimal("2.50"),
    )  # fmt: skip


@pytest.fixture
def ro_task(task):
    return replace(task, tool_policy=ToolPolicy.READ_ONLY)


@pytest.fixture
def rr_task(task):
    return replace(task, tool_policy=ToolPolicy.READ_RUN)


@pytest.fixture
def resume_task(task):
    return replace(task, resume_session=str(task.run_id))


class SeqSandbox(StubSandbox):
    """Replays success.jsonl, then resume.jsonl."""

    def __init__(self, root):
        super().__init__(root, CC / "success.jsonl")
        self.seq = [CC / "success.jsonl", CC / "resume.jsonl"]

    async def exec(self, argv, **kw):
        self.fixture = self.seq[min(len(self.calls), len(self.seq) - 1)]
        return await super().exec(argv, **kw)


def box_for(tmp_path, name):
    return StubSandbox(tmp_path / "box", CC / name)


async def test_gateway_invocation(tmp_path, task):
    box = box_for(tmp_path, "success.jsonl")
    r = await ClaudeCodeEngine(server_settings).run(task, box)
    c = box.calls[0]
    a = c["argv"]
    assert a[:3] == ["claude", "--bare", "-p"]
    for flag, val in [("--output-format", "stream-json"), ("--permission-mode", "dontAsk"),
                      ("--model", "claude-sonnet@vertex"), ("--max-turns", "80"),
                      ("--max-budget-usd", "2.50"), ("--session-id", str(task.run_id)),
                      ("--setting-sources", "user")]:  # fmt: skip
        assert a[a.index(flag) + 1] == val
    assert "--verbose" in a and "--strict-mcp-config" in a
    assert c["stdin"] == "do it"
    e = c["env"]
    assert e["ANTHROPIC_BASE_URL"] == "http://gateway-edge:4000"
    assert e["ANTHROPIC_AUTH_TOKEN"] == "sk-run-test"
    assert e["CLAUDE_CONFIG_DIR"] == "/home/factory/claude"
    assert e["CLAUDE_CODE_PROJECT_DIR_NAME"] == str(task.job_id)
    assert (
        e["DISABLE_AUTOUPDATER"]
        == e["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]
        == e["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"]
        == "1"
    )
    assert e["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet@vertex"
    assert e["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet@vertex"
    assert "CLAUDE_CODE_ATTRIBUTION_HEADER" not in e
    assert e["FACTORY_ROLE"] == "builder"
    assert r.status is RunStatus.OK and r.reported_cost_usd == Decimal("0.0412")
    assert r.used_models == ("claude-sonnet@vertex",)


def test_non_claude_family_disables_attribution_header(task):
    t = replace(task, model=ModelRef("gemini-pro@vertex", "vertex", "gemini-2.5-pro"))
    assert claude_env(t)["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"


def test_read_only_allowed_tools(ro_task):
    a = claude_argv(ro_task)
    i, j = a.index("--allowedTools"), a.index("--disallowedTools")
    assert a[i + 1 : i + 6] == ["Read", "Bash(git diff *)", "Bash(git log *)",
                                "Bash(git show *)", "Bash(git status *)"]  # fmt: skip
    assert a[j + 1 : j + 4] == ["Edit", "Write", "NotebookEdit"]
    assert {"Bash(git * --ou*)", "Bash(git * --ext*)", "Bash(git * --open*)"} <= set(a[j + 1 :])


def test_read_run_tools(rr_task):
    a = claude_argv(rr_task)
    i, j = a.index("--allowedTools"), a.index("--disallowedTools")
    assert a[i + 1 : i + 5] == ["Read", "Grep", "Glob", "Bash"]
    assert a[j + 1 : j + 4] == ["Edit", "Write", "NotebookEdit"]


def test_none_policy_and_write_tools(task):
    a = claude_argv(replace(task, tool_policy=ToolPolicy.NONE))
    assert a[a.index("--tools") + 1] == ""
    w = claude_argv(task)
    i = w.index("--allowedTools")
    assert w[i + 1 : i + 4] == ["Read", "Edit", "Bash"]


async def test_resume_stores_delta(tmp_path, task, resume_task):
    box = SeqSandbox(tmp_path / "box")
    eng = ClaudeCodeEngine(server_settings)
    r1 = await eng.run(task, box)
    r2 = await eng.run(resume_task, box)
    argv2 = box.calls[-1]["argv"]
    assert r2.reported_cost_usd == Decimal("0.0288")
    assert argv2[argv2.index("--resume") + 1] == resume_task.resume_session
    assert "--session-id" not in argv2
    cum = usage_of(CC / "resume.jsonl")
    assert (r2.usage.input_tokens, r2.usage.output_tokens) == (
        cum.input_tokens - r1.usage.input_tokens,
        cum.output_tokens - r1.usage.output_tokens,
    )
    assert r2.usage.cache_read_tokens == cum.cache_read_tokens - r1.usage.cache_read_tokens


async def test_resume_without_totals_reports_cumulative(tmp_path, resume_task):
    box = box_for(tmp_path, "resume.jsonl")
    r = await ClaudeCodeEngine(server_settings).run(resume_task, box)
    assert r.reported_cost_usd == Decimal("0.07")


@pytest.mark.parametrize(
    "fixture,status", [("max_turns.jsonl", RunStatus.FAILED), ("budget.jsonl", RunStatus.BUDGET)]
)
async def test_result_subtypes(tmp_path, task, fixture, status):
    r = await ClaudeCodeEngine(server_settings).run(task, box_for(tmp_path, fixture))
    assert r.status is status


@pytest.mark.parametrize(
    "stdout,code,timed_out,status,reason",
    [
        ("", 124, True, RunStatus.TIMEOUT, None),
        ("", 137, False, RunStatus.FAILED, "killed"),
        ('{"type": "system", "subtype": "init"}\n{"type": "assist', 0, False,
         RunStatus.FAILED, "unparseable output"),
    ],
)  # fmt: skip
async def test_timeout_kill_and_bad_output(tmp_path, task, stdout, code, timed_out, status, reason):
    (fx := tmp_path / "stdout.jsonl").write_text(stdout)
    box = StubSandbox(tmp_path / "box", fx, exit_code=code, timed_out=timed_out)
    r = await ClaudeCodeEngine(server_settings).run(task, box)
    assert (r.status, r.meta.get("reason")) == (status, reason)


async def test_swapped_model_sets_used_models(tmp_path, task):
    r = await ClaudeCodeEngine(server_settings).run(task, box_for(tmp_path, "swapped_model.jsonl"))
    assert fallback_detected(task.model, r.used_models) is True


async def test_classifier_fallback_reported_in_used_models(tmp_path, task):
    r = await ClaudeCodeEngine(server_settings).run(
        task, box_for(tmp_path, "classifier_fallback.jsonl")
    )
    assert r.used_models == ("claude-sonnet@vertex", "claude-opus-5@vertex")


async def test_not_enabled_error_text_and_class(tmp_path, task):
    r = await ClaudeCodeEngine(server_settings).run(task, box_for(tmp_path, "not_enabled.jsonl"))
    assert (r.status, r.meta["error_class"]) == (RunStatus.FAILED, "not_enabled")
    assert r.error and "Publisher Model" in r.error
    assert model_error_class(r.error) == "not_enabled"
