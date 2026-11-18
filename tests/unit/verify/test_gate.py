import json

from tests.fakes.gate_case import BASE, HEAD, git_subcommands, run_gate

from swfactory.domain.types import VerifyStatus
from swfactory.verify.gate import protected_hits, resolve_test_command, verify_json_name


def test_app_command_wins():
    assert resolve_test_command("make test", "Test command: `pytest`") == "make test"


def test_agents_md_line():
    assert (
        resolve_test_command(None, "## Stack\n- **Test command:** `uv run pytest -q`\n")
        == "uv run pytest -q"
    )


def test_none_means_skipped():
    assert (
        resolve_test_command(None, "# no stack") is None
        and resolve_test_command(None, None) is None
    )


def test_protected_globs():
    assert protected_hits(
        [".github/workflows/ci.yml", "src/a.py", "tests/hidden/t.py"],
        [".github/", "tests/hidden/**"],
    ) == (".github/workflows/ci.yml", "tests/hidden/t.py")


async def test_rebase_conflict_is_error():
    c = await run_gate(rebase=False)
    assert (c.r.status, c.r.reason) == (VerifyStatus.ERROR, "rebase conflict")


async def test_head_not_on_job_branch_is_error():
    c = await run_gate(symref="refs/heads/fix/greeting")
    assert (c.r.status, c.r.reason) == (VerifyStatus.ERROR, "HEAD is not on the job branch")
    assert c.provider.verify_calls == []


async def test_command_from_default_branch_not_head():
    c = await run_gate(base_agents="Test command: `pytest -q`", head_agents="Test command: `true`")
    assert (c.r.status, c.r.reason) == (VerifyStatus.ERROR, "test command changed")
    assert c.r.test_command == "pytest -q"
    assert ["bash", "-lc", "pytest -q"] in c.iso.exec_calls


async def test_git_reads_happen_in_the_verify_box():
    c = await run_gate()
    assert {"diff", "show", "merge-base"} <= git_subcommands(c.iso)
    assert not {"diff", "show"} & git_subcommands(c.job_box)
    assert (
        c.git.diff_stat_boxes == [c.iso]
        and c.artifacts["base/AGENTS.md"] == "Test command: `pytest -q`"
    )


async def test_branch_without_base_is_error():
    c = await run_gate(on_base=1)
    assert (c.r.status, c.r.reason) == (
        VerifyStatus.ERROR,
        "branch does not contain the default branch",
    )


async def test_setup_runs_in_the_verify_box_before_tests():
    c = await run_gate(setup=("npm ci",))
    assert c.iso.exec_calls.index(["bash", "-lc", "npm ci"]) < c.iso.exec_calls.index(
        ["bash", "-lc", "pytest -q"]
    )


async def test_setup_failure_fails_the_gate():
    c = await run_gate(setup=("npm ci",), setup_exit=1)
    assert (c.r.status, c.r.reason) == (VerifyStatus.FAILED, "setup failed: npm ci (exit 1)")
    assert ["bash", "-lc", "pytest -q"] not in c.iso.exec_calls


async def test_tests_run_in_the_verify_box_at_head():
    c = await run_gate()
    assert c.provider.verify_calls == [("gate-r1-a1", HEAD)] and c.provider.isolated_names == []
    assert not any(a[:2] == ["bash", "-lc"] for a in c.job_box.exec_calls)
    assert c.r.status == VerifyStatus.PASSED and BASE != HEAD


async def test_stores_diff_patch():
    c = await run_gate()
    assert c.artifacts["diff.patch"].startswith("diff --git") and c.kinds["diff.patch"] == "log"


async def test_stores_whole_diff_patch():
    big = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        + "+x\n" * 150_000
        + "diff --git a/src/auth.py b/src/auth.py\n+y\n"
    )
    assert (await run_gate(patch=big)).artifacts["diff.patch"] == big


async def test_stores_verify_json():
    c = await run_gate(attempt=2, test_exit=1)
    assert verify_json_name(1, 2) == "verify/r1-a2.json" and c.kinds["verify/r1-a2.json"] == "json"
    assert json.loads(c.artifacts["verify/r1-a2.json"]) == {
        "status": "failed",
        "reason": "tests failed (exit 1)",
        "exit_code": 1,
        "protected": [],
        "screenshots": [],
        "test_command": "pytest -q",
        "head_sha": HEAD,
    }


async def test_failing_tests_fail_with_log():
    c = await run_gate(test_exit=1, test_output="Traceback (most recent call last):\n")
    assert c.r.status == VerifyStatus.FAILED and c.r.reason == "tests failed (exit 1)"
    assert "Traceback" in c.artifacts["verify.md"]
    assert "Traceback" in c.artifacts["logs/verify-r1-a1.log"]


async def test_timeout_comes_from_the_caller():
    c = await run_gate(timed_out=True, test_timeout_s=2700)
    assert c.iso.exec_timeouts[-1] == 2700 and c.r.reason == "tests timed out after 45 min"
    assert (await run_gate(timed_out=True, test_timeout_s=900)).iso.exec_timeouts[-1] == 900


async def test_protected_path_fails():
    c = await run_gate(diff_paths=(".github/workflows/ci.yml",), protected=(".github/",))
    assert c.r.status == VerifyStatus.FAILED and c.r.protected == (".github/workflows/ci.yml",)
    assert c.r.paths == (".github/workflows/ci.yml",)


async def test_no_command_is_skipped():
    c = await run_gate(base_agents=None, head_agents=None)
    assert c.r.status == VerifyStatus.SKIPPED and "No test command" in c.r.report


async def test_all_checks_reported_together():
    c = await run_gate(
        test_exit=1, diff_paths=(".github/workflows/ci.yml",), protected=(".github/",)
    )
    assert "| Tests |" in c.r.report and "| Protected paths |" in c.r.report
