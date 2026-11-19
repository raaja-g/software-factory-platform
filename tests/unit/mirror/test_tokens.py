import pytest
from tests.unit.mirror.test_config import NPM_AR, NPM_GH, PY_TOK

from swfactory.logging import redact
from swfactory.mirror.tokens import (
    TokenUnavailable,
    adc_access_token,
    read_env_file,
    registry_token,
)


async def test_artifact_registry_uses_the_adc_callable():
    assert await registry_token(NPM_AR, env={}, adc=lambda: "ya29.x", github=None) == "ya29.x"


async def test_artifact_registry_without_adc_is_unavailable():
    with pytest.raises(TokenUnavailable, match="no registry ADC"):
        await registry_token(NPM_AR, env={}, adc=None, github=None)


async def test_token_upstream_reads_its_env_var_and_registers_it():
    env = {"REGISTRY_ACME_PY_TOKEN": "tok-123456789"}
    assert await registry_token(PY_TOK, env=env, adc=None, github=None) == "tok-123456789"
    assert "tok-123456789" not in redact("header tok-123456789")
    with pytest.raises(TokenUnavailable, match="REGISTRY_ACME_PY_TOKEN is not set"):
        await registry_token(PY_TOK, env={}, adc=None, github=None)


async def test_github_packages_needs_the_broker():
    with pytest.raises(TokenUnavailable, match="GitHub App"):
        await registry_token(NPM_GH, env={}, adc=None, github=None)

    async def broker():
        return "ghs_x"

    assert await registry_token(NPM_GH, env={}, adc=None, github=broker) == "ghs_x"


def test_read_env_file_returns_only_named_variables(tmp_path):
    p = tmp_path / ".env"
    p.write_text("LITELLM_MASTER_KEY=sk-1\nREGISTRY_A_TOKEN='abc'\n# c\nGEMINI_API_KEY=k\n")
    assert read_env_file(p, ["REGISTRY_A_TOKEN", "NOPE"]) == {"REGISTRY_A_TOKEN": "abc"}


def test_empty_adc_file_is_unavailable(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{}")
    with pytest.raises(TokenUnavailable, match="no registry ADC"):
        adc_access_token(p)
    p.write_text("")
    with pytest.raises(TokenUnavailable, match="no registry ADC"):
        adc_access_token(p)
