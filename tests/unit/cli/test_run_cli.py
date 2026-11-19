import os

from tests.unit.cli.conftest import run


def test_run_prints_the_started_line(mock_api):
    r = run("run", "--repo", "~/work/todo-app", "--req-text", "A todo list")
    assert r.exit_code == 0
    assert r.output == "backlog todo-app:b001 started; requirements job todo-app:001\n"
    assert mock_api.bodies[0] == {
        "repo": os.path.expanduser("~/work/todo-app"),
        "requirement": "A todo list",
        "ci_safe": False,
    }


def test_run_reads_multiline_text_from_stdin(mock_api):
    run("run", "--repo", "acme/web", "--req", "-", input="line one\nline two\n")
    assert mock_api.bodies[0]["requirement"] == "line one\nline two\n"


def test_run_reads_a_markdown_file(mock_api, tmp_path):
    f = tmp_path / "req.md"
    f.write_text("# Todo\n- add\n")
    run("run", "--repo", "acme/web", "--req", str(f))
    assert mock_api.bodies[0]["requirement"] == "# Todo\n- add\n"


def test_run_needs_exactly_one_requirement_source(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("x")
    assert run("run", "--repo", "acme/web").exit_code == 2
    assert run("run", "--repo", "acme/web", "--req", str(f), "--req-text", "y").exit_code == 2
    assert run("run", "--repo", "acme/web", "--req", str(tmp_path / "missing.md")).exit_code == 2


def test_run_rejects_a_file_that_is_not_utf8(tmp_path):
    f = tmp_path / "r.md"
    f.write_bytes(b"\xff\xfe\x00bad")
    assert run("run", "--repo", "acme/web", "--req", str(f)).exit_code == 2


def test_run_resolves_dot_paths_but_not_owner_name(mock_api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run("run", "--repo", ".", "--req-text", "x")
    run("run", "--repo", "acme/web", "--req-text", "x")
    assert [b["repo"] for b in mock_api.bodies] == [str(tmp_path.resolve()), "acme/web"]


def test_run_sends_the_flags(mock_api):
    run("run", "--repo", "acme/web", "--req-text", "x", "--profile", "mixed-economy",
        "--ci-safe", "--budget", "5")  # fmt: skip
    assert mock_api.bodies[0] == {
        "repo": "acme/web", "requirement": "x", "profile": "mixed-economy",
        "ci_safe": True, "budget_usd": "5",
    }  # fmt: skip


def test_run_failure_exits_1_with_the_detail(mock_api_failed):
    r = run("run", "--repo", "/x", "--req-text", "x")
    assert r.exit_code == 1 and "is not a git repository" in r.output


def test_run_waiting_for_a_definition_pr_prints_it_once(mock_api_waiting_then_started):
    r = run("run", "--repo", "acme/web", "--req-text", "x")
    msg = "waiting for the definition PR https://github.com/org/def/pull/3 to merge"
    assert r.output.count(msg) == 1 and "started; requirements job" in r.output
