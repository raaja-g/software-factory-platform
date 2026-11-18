"""Render the LiteLLM proxy config from the definition."""

import re
from collections.abc import Mapping
from typing import Any

from swfactory.definition.models import Definition

LITELLM_PREFIX: Mapping[str, str] = {
    "vertex_ai": "vertex_ai",
    "gemini": "gemini",
    "groq": "groq",
    "openrouter": "openrouter",
    "lm_studio": "lm_studio",
    "ollama": "ollama_chat",
    "openai_compatible": "openai",
    "anthropic_compatible": "anthropic",
}
LOCAL_KINDS = frozenset({"lm_studio", "ollama"})
_BASE_URL_KINDS = frozenset({"lm_studio", "ollama", "openai_compatible", "anthropic_compatible"})
VERTEX_PROJECT_PARAM = "vertex_project"
VERTEX_LOCATION_PARAM = "vertex_location"
DEFAULT_PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"
LITELLM_SETTINGS: Mapping[str, bool] = {
    "turn_off_message_logging": True,
    "redact_user_api_key_info": True,
}
SPEND_BATCH_S = 5
LITELLM_DB_POOL_LIMIT = 10


def _per_token(per_mtok: Any) -> float:
    return float(per_mtok) / 1_000_000


# The only environment variables a rendered config may reference. Anything else that starts with
# "os.environ/" would let a definition read the gateway's other secrets.
_ENV_REF = re.compile(
    r"os\.environ/((GEMINI|GROQ|OPENROUTER|BYO_[A-Z0-9_]+)_API_KEY|GOOGLE_CLOUD_PROJECT"
    r"|[A-Z][A-Z0-9_]*_PROJECT|LITELLM_MASTER_KEY)"
)


def _check_env_refs(params: dict[str, Any]) -> None:
    for key, value in params.items():
        if (
            isinstance(value, str)
            and value.startswith("os.environ/")
            and not _ENV_REF.fullmatch(value)
        ):
            raise ValueError(
                f"refusing to render {key}={value!r}: not an allowed environment reference"
            )


def render_litellm_config(defn: Definition) -> dict[str, Any]:
    providers = defn.factory.providers
    entries: list[dict[str, Any]] = []
    limited = False
    for alias, m in defn.factory.models.items():
        prov = providers[m.provider]
        params: dict[str, Any] = {"model": f"{LITELLM_PREFIX[prov.kind]}/{m.upstream_id}"}
        if prov.api_key_env:
            params["api_key"] = f"os.environ/{prov.api_key_env}"
        if prov.kind in _BASE_URL_KINDS and prov.base_url:
            params["api_base"] = prov.base_url
        if prov.kind == "vertex_ai":
            params[VERTEX_PROJECT_PARAM] = prov.project or (
                f"os.environ/{prov.project_env or DEFAULT_PROJECT_ENV}"
            )
            params[VERTEX_LOCATION_PARAM] = prov.location or "global"
        if m.rpm is not None:
            params["rpm"] = m.rpm
            limited = True
        if m.tpm is not None:
            params["tpm"] = m.tpm
            limited = True
        _check_env_refs(params)
        entry: dict[str, Any] = {"model_name": alias, "litellm_params": params}
        info: dict[str, Any] = {}
        if prov.kind in LOCAL_KINDS:
            info = {"input_cost_per_token": 0, "output_cost_per_token": 0}
        if m.price_in_per_mtok is not None:
            info["input_cost_per_token"] = _per_token(m.price_in_per_mtok)
        if m.price_out_per_mtok is not None:
            info["output_cost_per_token"] = _per_token(m.price_out_per_mtok)
        if info:
            entry["model_info"] = info
        entries.append(entry)
    cfg: dict[str, Any] = {
        "model_list": entries,
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "proxy_batch_write_at": SPEND_BATCH_S,
            "database_connection_pool_limit": LITELLM_DB_POOL_LIMIT,
        },
        "litellm_settings": dict(LITELLM_SETTINGS),
    }
    if limited:
        cfg["router_settings"] = {"optional_pre_call_checks": ["enforce_model_rate_limits"]}
    return cfg
