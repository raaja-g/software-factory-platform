import json
from dataclasses import replace
from pathlib import Path

import pytest
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.domain.types import ModelRef, RunStatus, ToolPolicy
from swfactory.engines.base import fallback_detected
from swfactory.engines.opencode import (
    OPENCODE_CACHE,
    OPENCODE_HOME_CACHE,
    OUTPUT_TOKEN_LIMIT,
    OpenCodeEngine,
    opencode_config,
    opencode_permission,
    parse_events,
)
from swfactory.gateway.client import model_error_class

FX = Path(__file__).parent / "fixtures" / "opencode"


@pytest.fixture
def task(task):
    return replace(
        task,
        engine="opencode",
        model=ModelRef("gemini-flash@vertex", "vertex", "gemini-4-flash"),
        gateway_base_url="http://gateway-edge:4000",
    )


@pytest.fixture
def claude_task(task):
    return replace(task, model=ModelRef("sonnet@vertex", "vertex", "claude-sonnet-4-6"))


@pytest.fixture
def resume_task(task):
    return replace(task, resume_session="ses_abc")


@pytest.fixture
def stub_box_opencode(tmp_path):
    return StubSandbox(tmp_path / "box", FX / "success.jsonl")


@pytest.fixture
def stub_box_opencode_swapped(tmp_path):
    return StubSandbox(tmp_path / "box", FX / "swapped_model.jsonl")


@pytest.fixture
def stub_box_opencode_error(tmp_path):
    return StubSandbox(tmp_path / "box", FX / "error.jsonl", exit_code=1)


async def test_invocation(stub_box_opencode, task):
    r = await OpenCodeEngine().run(task, stub_box_opencode)
    seed, call = stub_box_opencode.calls[0], stub_box_opencode.calls[-1]
    assert call["argv"][:4] == ["opencode", "run", "--format", "json"] and "-m" in call["argv"]
    assert call["argv"][call["argv"].index("-m") + 1] == "factory/gemini-flash@vertex"
    assert call["env"]["OPENCODE_DISABLE_CLAUDE_CODE"] == "1" and call["cwd"] == task.workdir
    assert call["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert call["env"]["XDG_CACHE_HOME"] == "/home/factory/.cache"
    assert call["env"]["FACTORY_JOB"] == str(task.job_id)
    assert seed["argv"][:2] == ["sh", "-c"]
    assert seed["argv"][-2:] == [OPENCODE_CACHE, OPENCODE_HOME_CACHE]
    cfg = json.loads(await stub_box_opencode.read_text(call["env"]["OPENCODE_CONFIG"]))
    assert cfg["provider"]["factory"]["options"] == {
        "baseURL": "http://gateway-edge:4000/v1",
        "apiKey": "{env:FACTORY_GATEWAY_KEY}",
    }
    assert task.gateway_key not in json.dumps(cfg)
    assert r.status is RunStatus.OK and r.session_id and r.usage.output_tokens > 0
    assert r.output == "Done."


def test_every_model_declares_an_output_limit(claude_task, task):
    """Without one OpenCode asks for its default (about 32k tokens), which Groq's qwen refuses."""
    for t in (task, claude_task):
        (provider,) = opencode_config(t)["provider"].values()
        entry = provider["models"][t.model.alias]
        assert entry["limit"] == {"context": 0, "output": OUTPUT_TOKEN_LIMIT}
    assert OUTPUT_TOKEN_LIMIT <= 16384  # the smallest cap of the free providers in use


def test_claude_uses_anthropic_provider(claude_task):
    p = opencode_config(claude_task)["provider"]
    assert list(p) == ["factory-anthropic"]
    assert p["factory-anthropic"]["npm"] == "@ai-sdk/anthropic"
    assert p["factory-anthropic"]["options"] == {
        "baseURL": "http://gateway-edge:4000/v1",
        "apiKey": "{env:FACTORY_GATEWAY_KEY}",
    }


def test_read_only_permission():
    p = opencode_permission(ToolPolicy.READ_ONLY)
    assert p["edit"] == "deny" and p["webfetch"] == "deny"
    bash = p["bash"]
    keys = list(bash)
    assert keys[0] == "*" and bash["*"] == "deny"
    assert [k for k, v in bash.items() if v == "allow"] == [
        f"git {s}{x}" for s in ("diff", "log", "show", "status") for x in ("", " *")
    ]
    assert bash["git diff"] == bash["git diff *"] == "allow" and "git difftool" not in bash
    first_deny = min(keys.index(k) for k, v in bash.items() if v == "deny" and k != "*")
    assert first_deny > max(
        keys.index(k) for k, v in bash.items() if v == "allow"
    )  # last match wins
    for risky in ("*--ou*", "*--ext*", "*--open*", "*;*", "*&*", "*|*", "*>*", "*\n*", "*\r*"):
        assert bash[risky] == "deny"


def test_read_run_permission():
    assert opencode_permission(ToolPolicy.READ_RUN) == {
        "edit": "deny", "webfetch": "deny", "bash": "allow",
    }  # fmt: skip


def test_write_and_none_permission():
    assert opencode_permission(ToolPolicy.WRITE) == {
        "edit": "allow", "bash": "allow", "webfetch": "deny",
        "external_directory": {"/work/.factory/out/*": "allow"},
    }  # fmt: skip
    assert opencode_permission(ToolPolicy.NONE) == {
        "edit": "deny", "webfetch": "deny", "bash": "deny",
    }  # fmt: skip


def test_parse_events_sums_tokens():
    run = parse_events((FX / "success.jsonl").read_text())
    assert run.text == "Done." and run.session_id == "ses_abc123"
    assert (run.usage.input_tokens, run.usage.output_tokens) == (220, 14)
    assert run.models == ("gemini-flash@vertex",)


async def test_swapped_model_sets_used_models(stub_box_opencode_swapped, task):
    r = await OpenCodeEngine().run(task, stub_box_opencode_swapped)
    assert fallback_detected(task.model, r.used_models) is True


async def test_resume_passes_session(stub_box_opencode, resume_task):
    await OpenCodeEngine().run(resume_task, stub_box_opencode)
    a = stub_box_opencode.calls[-1]["argv"]
    assert a[a.index("--session") + 1] == "ses_abc"


@pytest.mark.parametrize(
    "stdout,code,timed_out,status,reason",
    [
        ("", 124, True, RunStatus.TIMEOUT, None),
        ("", 137, False, RunStatus.FAILED, "killed"),
        ('{"type": "step_start", "sessionID": "ses_1"}\n{"type": "te', 0, False,
         RunStatus.FAILED, "unparseable output"),
    ],
)  # fmt: skip
async def test_timeout_kill_and_bad_output(tmp_path, task, stdout, code, timed_out, status, reason):
    (fx := tmp_path / "stdout.jsonl").write_text(stdout)
    r = await OpenCodeEngine().run(
        task, StubSandbox(tmp_path / "box", fx, exit_code=code, timed_out=timed_out)
    )
    assert (r.status, r.meta.get("reason")) == (status, reason)


async def test_nonzero_exit_failed_with_error_class(stub_box_opencode_error, task):
    r = await OpenCodeEngine().run(task, stub_box_opencode_error)
    assert (r.status, r.meta["error_class"]) == (RunStatus.FAILED, "gateway")
    assert r.error is not None and "ECONNREFUSED" in r.error
    assert model_error_class(r.error) is None


def test_only_write_roles_may_touch_the_output_directory():
    # a write role's output file lives in /work/.factory/out, outside the repo; OpenCode
    # auto-rejects a write outside the project unless that path is allowed, so a builder that did
    # its work could not write build.md. Nothing broader than that one directory is allowed, and
    # roles that reply in their final message get no external access at all.
    write = opencode_permission(ToolPolicy.WRITE)["external_directory"]
    assert write == {"/work/.factory/out/*": "allow"}
    for policy in (ToolPolicy.READ_ONLY, ToolPolicy.READ_RUN, ToolPolicy.NONE):
        assert "external_directory" not in opencode_permission(policy)
