from dataclasses import replace

import pytest

from swfactory.domain.types import ModelRef, RunStatus, ToolPolicy
from swfactory.engines.base import (
    classify_failure,
    collect_output,
    commit_env,
    error_class_of,
    fallback_detected,
    json_lines,
    scrub,
)


def test_fallback_detected_matches_alias_or_upstream():
    req = ModelRef("gemini-pro@vertex", "vertex", "gemini-3.1-pro-preview")
    assert not fallback_detected(req, ["gemini-pro@vertex"])
    assert not fallback_detected(req, ["vertex_ai/gemini-3.1-pro-preview"])
    assert fallback_detected(req, ["gemini-3.1-pro-preview", "gemini-3.5-flash-lite"])
    assert not fallback_detected(req, [])
    assert not fallback_detected(
        req,
        ["gemini-pro@vertex", "claude-opus-5@vertex"],
        ignore={"claude-opus-5@vertex"},
    )


def test_classify_failure():
    assert classify_failure("Budget has been exceeded! Max budget: 0.5") is RunStatus.BUDGET
    assert classify_failure("lint failed") is RunStatus.FAILED


def test_json_lines_skips_the_omitted_marker():
    text = (
        '{"type": "system", "session_id": "s1"}\n{"type": "assis\n[omitted: 1234 bytes]\n'
        'text"}\n{"type": "result", "total_cost_usd": 0.01}\n'
    )
    assert [e["type"] for e in json_lines(text)] == ["system", "result"]


@pytest.mark.parametrize(
    "text,status,cls",
    [
        ("connect ECONNREFUSED 10.0.0.5:4000", None, "gateway"),
        ("Bad Gateway", 502, "http_5xx"),
        ("RESOURCE_EXHAUSTED", 429, "rate_limit"),
        ("Publisher Model `claude-opus-5` was not found", 404, "not_enabled"),
        ("Authentication Error, Invalid proxy server token", 401, "auth"),
        ("Budget has been exceeded!", 400, None),
        ("lint failed", None, None),
        ("getaddrinfo ENOTFOUND gateway-edge", None, "gateway"),
    ],
)
def test_error_class_of(text, status, cls):
    assert error_class_of(text, status=status) == cls


async def test_collect_output_reads_the_file_only_for_write(stub_box, task):
    w = replace(task, tool_policy=ToolPolicy.WRITE)
    await stub_box.write_text(w.output_path, "## Round 1\nbuilt")
    assert await collect_output(stub_box, w, "ignored reply") == "## Round 1\nbuilt"


async def test_collect_output_ignores_a_planted_file_for_read_roles(stub_box, task):
    rr = replace(task, tool_policy=ToolPolicy.READ_RUN)
    await stub_box.write_text(rr.output_path, "VERDICT: PASS")
    assert (
        await collect_output(stub_box, rr, "VERDICT: CHANGES\nfix x") == "VERDICT: CHANGES\nfix x"
    )
    assert await stub_box.read_text(rr.output_path) is None


def test_scrub_removes_run_key(task):
    assert task.gateway_key not in scrub(f"key={task.gateway_key}", task)


def test_commit_env(task):
    assert commit_env(task) == {
        "FACTORY_JOB": str(task.job_id),
        "FACTORY_ROLE": "builder",
        "FACTORY_MODEL": task.model.alias,
    }
