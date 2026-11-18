import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.gateway.render import (
    LITELLM_DB_POOL_LIMIT,
    SPEND_BATCH_S,
    VERTEX_LOCATION_PARAM,
    VERTEX_PROJECT_PARAM,
    render_litellm_config,
)
from swfactory.settings import get_settings

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "gateway"
GOLDEN = FIXTURE / "litellm.golden.yaml"


@pytest.fixture(autouse=True)
def _hosts(monkeypatch):
    monkeypatch.setenv("FACTORY_ALLOWED_LOCAL_MODEL_HOSTS", "host.docker.internal,some-proxy")
    get_settings.cache_clear()


def load_fixture():
    r = load_definition(str(FIXTURE))
    assert not r.errors, r.errors
    assert r.definition is not None
    return r.definition


def load_defaults():
    r = load_definition(str(defaults_dir()))
    assert not r.errors, r.errors
    assert r.definition is not None
    return r.definition


def params_of(cfg, alias):
    return next(e for e in cfg["model_list"] if e["model_name"] == alias)["litellm_params"]


def params(alias):
    return params_of(render_litellm_config(load_fixture()), alias)


def info(alias):
    cfg = render_litellm_config(load_fixture())
    return next(e for e in cfg["model_list"] if e["model_name"] == alias)["model_info"]


def with_limits(defn, alias, **limits):
    models = dict(defn.factory.models)
    models[alias] = models[alias].model_copy(update=limits)
    return defn.model_copy(update={"factory": defn.factory.model_copy(update={"models": models})})


def test_matches_golden():
    cfg = render_litellm_config(load_fixture())
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert cfg == yaml.safe_load(GOLDEN.read_text())


def test_prefixes():
    assert params("gemini-pro@vertex")["model"] == "vertex_ai/gemini-4-argon"
    assert params("flash@gemini")["model"].startswith("gemini/")
    assert params("coder@groq")["model"].startswith("groq/")
    assert params("coder@openrouter")["model"].startswith("openrouter/")
    assert params("qwen@lmstudio")["model"].startswith("lm_studio/")
    assert params("qwen@ollama")["model"].startswith("ollama_chat/")
    assert params("byo@openai")["model"].startswith("openai/")
    assert params("byo@anthropic")["model"].startswith("anthropic/")
    assert params("byo@openai")["api_base"] == "http://some-proxy:8080/v1"


def test_vertex_global():
    p = params("claude-sonnet@vertex")
    assert p[VERTEX_PROJECT_PARAM] == "os.environ/GOOGLE_CLOUD_PROJECT"
    assert p[VERTEX_LOCATION_PARAM] == "global"


def test_vertex_literal_project():
    assert params("flash@vertex-literal")[VERTEX_PROJECT_PARAM] == "my-gcp-project"


def test_shipped_defaults_use_google_cloud_project():
    entries = render_litellm_config(load_defaults())["model_list"]
    assert all(
        e["litellm_params"].get(VERTEX_PROJECT_PARAM, "os.environ/GOOGLE_CLOUD_PROJECT")
        == "os.environ/GOOGLE_CLOUD_PROJECT"
        for e in entries
    )


def test_logging_settings():
    cfg = render_litellm_config(load_fixture())
    assert cfg["litellm_settings"] == {
        "turn_off_message_logging": True,
        "redact_user_api_key_info": True,
    }
    assert set(cfg) == {"model_list", "general_settings", "litellm_settings"}
    assert cfg["general_settings"]["proxy_batch_write_at"] == SPEND_BATCH_S == 5
    assert cfg["general_settings"]["database_connection_pool_limit"] == LITELLM_DB_POOL_LIMIT == 10
    assert yaml.safe_load(GOLDEN.read_text())["litellm_settings"] == cfg["litellm_settings"]


def test_local_models_zero_priced():
    assert info("qwen@lmstudio") == {"input_cost_per_token": 0, "output_cost_per_token": 0}


def test_custom_price_override():
    assert info("flash@gemini")["input_cost_per_token"] == pytest.approx(0.30e-6)


def test_no_secret_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-secret")
    rendered = render_litellm_config(load_fixture())
    assert "sk-test-secret" not in yaml.safe_dump(rendered)
    assert all(
        e["litellm_params"].get("api_key", "os.environ/").startswith("os.environ/")
        for e in rendered["model_list"]
    )


def test_no_gateway_fallbacks():
    assert "fallbacks" not in render_litellm_config(load_fixture()).get("router_settings", {})


def test_rate_limits_rendered():
    cfg = render_litellm_config(with_limits(load_fixture(), "flash@gemini", rpm=60, tpm=200_000))
    assert (params_of(cfg, "flash@gemini")["rpm"], params_of(cfg, "flash@gemini")["tpm"]) == (
        60,
        200_000,
    )
    assert cfg["router_settings"] == {"optional_pre_call_checks": ["enforce_model_rate_limits"]}
    assert "rpm" not in params_of(cfg, "qwen@ollama")


def test_cli_writes_file(tmp_path):
    out = tmp_path / "litellm.yaml"
    result = CliRunner().invoke(
        app, ["gateway", "render", "--def", str(FIXTURE), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("# generated by factory gateway render")
    assert result.output.startswith(f"wrote {out}: ")


def test_provider_project_cannot_read_gateway_env():
    import pytest
    from pydantic import ValidationError

    from swfactory.definition.models import ProviderDef

    for bad in ("os.environ/GEMINI_API_KEY", "a/b", "UPPER-case", "x"):
        with pytest.raises(ValidationError):
            ProviderDef(kind="vertex_ai", project=bad)
    with pytest.raises(ValidationError):
        ProviderDef(kind="vertex_ai", location="os.environ/LITELLM_MASTER_KEY")
    assert (
        ProviderDef(kind="vertex_ai", project="my-proj-123", location="global").project
        == "my-proj-123"
    )


def test_renderer_refuses_unlisted_env_references():
    import pytest

    from swfactory.gateway.render import _check_env_refs

    _check_env_refs(
        {"api_key": "os.environ/GROQ_API_KEY", "vertex_project": "os.environ/GOOGLE_CLOUD_PROJECT"}
    )
    with pytest.raises(ValueError):
        _check_env_refs({"vertex_project": "os.environ/DATABASE_URL"})
