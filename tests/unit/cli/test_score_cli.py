from tests.unit.cli.conftest import SCORE_ROW, SCORE_SUMMARY, run

PENDING_POST = {
    "request_id": "q1",
    "summary": {**SCORE_SUMMARY, "judges_pending": ["merge-readiness"], "judges_skipped": []},
    "scores": [],
}
GATE = {
    **SCORE_ROW, "scorer": "obj.gate", "label": "passed", "judge_model": None, "cost_usd": "0.0",
}  # fmt: skip


def test_score_cli_prints_rows(mock_api):
    out = run("score", "demo:001", "--force").output
    assert (
        "merge-readiness  B  passed  judge gemini-lite@vertex  $0.004100  "
        "uncalibrated (0 of 30 labels)" in out
    )
    assert "scope-creep: not sampled" in out
    assert mock_api.bodies[-1] == {"force": True, "scorers": None} or mock_api.bodies
    assert "/api/jobs/demo:1/score" in mock_api.paths


def test_score_cli_scorer_option_repeats(mock_api):
    run("score", "demo:001", "--scorer", "a", "--scorer", "b")
    assert {"force": False, "scorers": ["a", "b"]} in mock_api.bodies


def test_score_cli_polls_until_done_then_prints_rows(mock_api):
    mock_api.score_post = PENDING_POST
    pending = {"status": "pending", "summary": None, "detail": None}
    running = {"status": "running", "summary": None, "detail": None}
    done = {"status": "done", "summary": SCORE_SUMMARY, "detail": None}
    mock_api.score_states = [pending, running, running, done]
    mock_api.scores = [GATE, SCORE_ROW]
    r = run("score", "demo:001", "--force")
    assert r.exit_code == 0, r.output
    assert mock_api.paths.count("/api/score-requests/q1") == 4
    assert r.output.splitlines()[0] == (
        "merge-readiness  B  passed  judge gemini-lite@vertex  $0.004100  "
        "uncalibrated (0 of 30 labels)"
    )
    assert "obj.gate" not in r.output  # one line per judge scorer
    assert mock_api.paths.index("/api/jobs/demo:1/scores") > mock_api.paths.index(
        "/api/score-requests/q1"
    )


def test_score_cli_no_wait_returns_at_once(mock_api):
    mock_api.score_post = PENDING_POST
    r = run("score", "demo:001", "--force", "--no-wait")
    assert r.exit_code == 0, r.output
    assert "q1" in r.output and "merge-readiness" in r.output
    assert not any(p.startswith("/api/score-requests/") for p in mock_api.paths)


def test_score_cli_failed_request_exits_1(mock_api):
    mock_api.score_post = PENDING_POST
    mock_api.score_states = [{"status": "failed", "summary": None, "detail": "boom"}]
    r = run("score", "demo:001")
    assert r.exit_code == 1 and "boom" in r.output


def test_score_cli_times_out(mock_api, monkeypatch):
    from swfactory.cli import api_client

    mock_api.score_post = PENDING_POST
    mock_api.score_states = [{"status": "running", "summary": None, "detail": None}]
    monkeypatch.setattr(api_client, "SCORE_WAIT_S", 6.0)
    r = run("score", "demo:001")
    assert r.exit_code == 1 and "still running" in r.output
    assert mock_api.paths.count("/api/score-requests/q1") == 4  # 0, 2, 4, 6 s


def test_score_label_prints_plan_wording(mock_api):
    r = run("score", "label", "demo:001", "merge-readiness", "A")
    assert r.exit_code == 0, r.output
    assert r.output.splitlines()[0] == "labelled merge-readiness on demo:001: A (1 of 30 labels)"
    assert "gemini-lite@vertex: B" in r.output


def test_jobs_show_has_scores_block(mock_api):
    mock_api.scores = [
        {**GATE, "label": "passed"},
        {**GATE, "scorer": "obj.fix_attempts", "label": "0"},
        {**GATE, "scorer": "obj.rounds", "label": "1"},
        {**GATE, "scorer": "obj.decision", "label": "APPROVE"},
        {**GATE, "scorer": "obj.human", "label": "none"},
        {**GATE, "scorer": "obj.delivery", "label": "merged_unchanged"},
        {**GATE, "scorer": "obj.fallback", "label": "none"},
        {**GATE, "scorer": "obj.duration_s", "label": "34 min"},
        {**GATE, "scorer": "obj.cost_usd", "label": "$2.10"},
        {**GATE, "scorer": "obj.run", "label": "ok"},
        {**GATE, "scorer": "obj.quality", "label": "good"},
        SCORE_ROW,
    ]
    out = run("jobs", "show", "todo:1").output
    assert "Scores:" in out
    assert (
        "gate passed · fix attempts 0 · rounds 1 · decision APPROVE · human none · "
        "delivery merged_unchanged · fallback none · 34 min · $2.10 · quality good" in out
    )
    assert (
        "merge-readiness  B  passed  judge gemini-lite@vertex  $0.004100  "
        "uncalibrated (0 of 30 labels)" in out
    )
