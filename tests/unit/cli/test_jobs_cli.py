from tests.unit.cli.conftest import SHOWN_HEAD, run


def test_jobs_create_sends_cli_source_and_token(mock_api):
    run("jobs", "create", "--app", "todo", "Add x")
    assert mock_api.headers["authorization"] == "Bearer sfp_t"
    assert mock_api.bodies[0]["source"] == "cli"


def test_token_read_from_file(mock_api, tmp_token_file):
    run("jobs", "list")
    assert mock_api.headers["authorization"] == f"Bearer {tmp_token_file.read_text().strip()}"


def test_jobs_create_feature_option_alias(mock_api):
    run("jobs", "create", "--app", "todo", "--feature", "Add x")
    assert mock_api.bodies[0]["feature"] == "Add x"


def test_jobs_create_prints_colon_ref(mock_api):
    r = run("jobs", "create", "--app", "todo", "Add x")
    assert r.output.startswith("queued todo:001 ") and "/#/job/" in r.output


def test_jobs_create_feature_twice_is_usage_error():
    assert run("jobs", "create", "--app", "todo", "--feature", "a", "b").exit_code == 2


def test_jobs_list_and_show(mock_api):
    assert "todo:001 needs_human Add x" in run("jobs", "list").output
    out = run("jobs", "show", "todo/1").output
    assert "head: " + SHOWN_HEAD in out and "/#/job/" in out
    assert mock_api.paths[-2] == "/api/jobs/todo:1"


def test_approve_403_exits_1_with_detail(mock_api_403):
    r = run("jobs", "approve", "todo:1")
    assert r.exit_code == 1 and "approver role required" in r.output


def test_approve_sends_head_sha_from_show(mock_api):
    run("jobs", "approve", "todo:1")
    assert mock_api.bodies[-1] == {"action": "approve", "note": None, "head_sha": SHOWN_HEAD}


def test_rework_note_option_and_positional(mock_api):
    run("jobs", "rework", "todo:1", "--note", "a")
    run("jobs", "rework", "todo/1", "b")
    assert [b["note"] for b in mock_api.bodies[-2:]] == ["a", "b"]


def test_rework_requires_note():
    assert run("jobs", "rework", "todo:1").exit_code == 2


def test_worker_keeps_fake_script():
    assert "--fake-script" in run("worker", "--help").output


def test_models_refresh_goes_through_the_api(mock_api):
    r = run("models", "refresh", "--alias", "flash@vertex")
    assert mock_api.bodies[0] == {"aliases": ["flash@vertex"]}
    assert mock_api.paths.count("/api/models/refresh/7") == 2 and "flash@vertex" in r.output


def test_models_list_and_apps_and_login_url(mock_api):
    assert "flash@vertex" in run("models", "list", "--available").output
    assert "todo  local" in run("apps", "list").output
    assert run("login-url").output.strip().endswith("/login?code=abc")


def test_serve_refuses_public_host_personal(monkeypatch):
    monkeypatch.setenv("FACTORY_MODE", "personal")
    from swfactory.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("swfactory.api.auth.gce_metadata_reachable", lambda *a, **k: False)
    assert run("serve", "--host", "0.0.0.0").exit_code == 2


def test_serve_refuses_unset_mode(env_no_mode):
    assert run("serve").exit_code == 2


def test_add_budget_posts_amount(mock_api):
    run("jobs", "add-budget", "todo:1", "5")
    assert mock_api.bodies[-1] == {"action": "add_budget", "note": None, "amount_usd": "5"}


def test_add_budget_rejects_bad_amount():
    assert run("jobs", "add-budget", "todo:1", "-2").exit_code == 2
