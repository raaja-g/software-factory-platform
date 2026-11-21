from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from swfactory.definition.models import AppDef, Definition, FactoryConfig, Profile
from swfactory.domain.types import EngineSupport, Role, SelectionMode
from swfactory.models.catalog import ModelInfo
from swfactory.models.profiles import (
    ResolvedProfile,
    engine_support,
    model_family,
    profile_warnings,
    resolve_profile,
    validate_profile,
)


def info(alias: str, upstream: str, **kw) -> ModelInfo:
    base = dict(
        alias=alias, provider="p", upstream_id=upstream, strength="mid",
        price_in_per_mtok=Decimal(1), price_out_per_mtok=Decimal(2), context_window=100000,
        supports_tools=True, messages_tools_ok=True, available=True, error_class=None,
        latency_ms=10, last_error=None,
    )  # fmt: skip
    base.update(kw)
    return ModelInfo(**base)  # type: ignore[arg-type]


SONNET = info("sonnet@vertex", "claude-sonnet-4-6")
FLASH = info("flash@vertex", "gemini-4-flash")
PRO = info("pro@vertex", "gemini-4-pro")
QWEN = info("qwen@ollama", "qwen/qwen3-coder")
QWEN_NO_TOOLS = replace(QWEN, supports_tools=False)
GONE_FREE = {a.alias: a for a in (SONNET, FLASH, PRO, QWEN)}
INFOS = {**GONE_FREE}

SHIPPED_PROFILES = {
    p.stem: Profile.model_validate(yaml.safe_load(p.read_text()))
    for p in sorted(Path("defaults/profiles").glob("*.yaml"))
}


def starter_infos() -> dict[str, ModelInfo]:
    out = {}
    for p in SHIPPED_PROFILES.values():
        for rm in p.roles.values():
            for a in (rm.model, *rm.fallback, *(t.model for t in rm.tiers)):
                fam = a.split("-")[0]
                out[a] = info(a, f"{fam}-x-{a}")
    return out


def profile(**roles) -> Profile:
    spec = {}
    for key, (engine, model, exp) in roles.items():
        role = {"code": "reviewer-code", "security": "reviewer-security"}.get(key, key)
        spec[role] = {"engine": engine, "model": model, "allow_experimental": exp}
    return Profile.model_validate({"roles": spec})


def starter(name: str) -> Profile:
    return SHIPPED_PROFILES[name]


def make_def() -> tuple[Definition, AppDef, AppDef]:
    f = yaml.safe_load(Path("defaults/factory.yaml").read_text())
    fc = FactoryConfig.model_validate(f)
    d = Definition(factory=fc, profiles=SHIPPED_PROFILES)
    app = fc.apps["demo"].model_copy(
        update={"profile": "team-gemini", "allowed_profiles": ("mixed-economy", "local")}
    )
    no_prof = fc.apps["demo"].model_copy(update={"profile": None, "allowed_profiles": ()})
    return d, app, no_prof


def test_resolution_order():
    d, app, no_prof = make_def()
    assert (
        resolve_profile(d, app, override="local", triage_profile="mixed-economy")[1] == "override"
    )
    assert resolve_profile(d, app, override=None, triage_profile="mixed-economy")[1] == "triage"
    assert resolve_profile(d, app, override=None, triage_profile="claude-vertex")[1] == "app"
    p, src = resolve_profile(d, no_prof, override=None, triage_profile=None)
    assert src == "global" and p.name == d.factory.defaults.profile


def test_unknown_override_raises():
    d, app, _ = make_def()
    with pytest.raises(KeyError):
        resolve_profile(d, app, override="nope", triage_profile=None)


PDEF = SHIPPED_PROFILES["team-gemini"]
PDEF_NO_REQ = PDEF.model_copy(
    update={"roles": {r: v for r, v in PDEF.roles.items() if r is not Role.REQUIREMENTS}}
)


def test_sticky_defaults():
    p = ResolvedProfile.from_def("team-gemini", PDEF)
    assert p.for_role(Role.BUILDER).sticky and not p.for_role(Role.APPROVER).sticky


def test_requirements_role_falls_back_to_the_builder_entry():
    p = ResolvedProfile.from_def("no-req", PDEF_NO_REQ)
    b, r = p.for_role(Role.BUILDER), p.for_role(Role.REQUIREMENTS)
    assert (r.engine, r.model, r.tiers, r.sticky, r.mode) == (
        b.engine, b.model, (), False, SelectionMode.FIXED,
    )  # fmt: skip


def test_shipped_profiles_name_a_requirements_model():
    assert all(
        Role.REQUIREMENTS in ResolvedProfile.from_def(n, d).roles
        for n, d in SHIPPED_PROFILES.items()
        if n in ("team-gemini", "claude-vertex")
    )


@pytest.mark.parametrize(
    "uid,fam",
    [
        ("claude-sonnet-4-6", "claude"),
        ("gemini-4-argon", "gemini"),
        ("gpt-5.2", "openai"),
        ("o3-mini", "openai"),
        ("qwen/qwen3-coder", "qwen"),
        ("llama-4-scout", "llama"),
        # provider prefixes, case and whitespace never change the family (D17 compares these)
        ("anthropic/claude-sonnet-5", "claude"),
        ("  claude-x", "claude"),
        ("Claude-Sonnet-5-5\n", "claude"),
        ("google/gemini-4", "gemini"),
        ("vertex_ai/gemini-4", "gemini"),
        ("models/gemini-4-argon", "gemini"),
        ("openai/gpt-5.2", "openai"),
        ("openrouter/anthropic/claude-opus-5", "claude"),
        ("openrouter/openai/o3-mini", "openai"),
        ("meta-llama/Llama-4-Scout", "llama"),
        ("mistralai/Mistral-Large", "mistral"),
        ("deepseek-ai/DeepSeek-V3", "deepseek"),
        ("qwen/qwen3-coder", "qwen"),
        ("Qwen2.5-Coder:7b", "qwen"),
        ("ollama:qwen2.5-coder:7b", "qwen"),
        ("anthropic:claude-haiku-5-5", "claude"),
        ("us.anthropic.claude-sonnet-5-v1:0", "claude"),
        ("bedrock/anthropic.claude-3-sonnet", "claude"),
        ("<model-id>", "<model"),
        ("<model>", "<model>"),
    ],
)
def test_model_family(uid, fam):
    assert model_family(uid) == fam


@pytest.mark.parametrize(
    "engine,inf,want",
    [
        ("claude_code", SONNET, "ok"),
        ("claude_code", FLASH, "experimental"),
        ("claude_code", replace(SONNET, messages_tools_ok=False), "no"),
        ("opencode", FLASH, "ok"),
        ("opencode", replace(FLASH, supports_tools=False), "no"),
        ("opencode", replace(FLASH, supports_tools=None), "experimental"),
        ("gemini_cli", FLASH, "ok"),
        ("gemini_cli", SONNET, "experimental"),
        ("direct", QWEN_NO_TOOLS, "ok"),
        ("nope", FLASH, "no"),
    ],
)
def test_engine_support(engine, inf, want):
    assert engine_support(engine, inf) == EngineSupport(want)


def test_validate_rejects_no_and_unallowed_experimental():
    assert validate_profile(profile(builder=("claude_code", "flash@vertex", False)), INFOS) == [
        "builder: claude_code with flash@vertex is experimental and allow_experimental is false"
    ]
    assert validate_profile(profile(builder=("claude_code", "flash@vertex", True)), INFOS) == []
    infos = {**INFOS, "qwen@ollama": QWEN_NO_TOOLS}
    assert "reviewer-code: opencode with qwen@ollama is not supported" in validate_profile(
        profile(code=("opencode", "qwen@ollama", False)), infos
    )
    assert "approver: unknown model alias gone@x" in validate_profile(
        profile(approver=("direct", "gone@x", False)), INFOS
    )


def test_d17_starter_profiles_independent():
    infos = starter_infos()
    for name in ("team-gemini", "claude-vertex"):
        assert profile_warnings(starter(name), infos) == []


def test_d17_same_family_flagged():
    assert profile_warnings(
        profile(
            builder=("opencode", "pro@vertex", False), security=("opencode", "flash@vertex", False)
        ),
        INFOS,
    ) == [
        "reviewer-security: flash@vertex is the builder's family (gemini); "
        "D17 prefers another family"
    ]
