from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from swfactory.definition.models import (
    AppDef,
    Defaults,
    FactoryConfig,
    ProviderDef,
    RegistryDef,
)
from swfactory.domain.types import MergePolicy


def defaults_factory() -> dict:
    return yaml.safe_load(Path("defaults/factory.yaml").read_text())


def test_factory_defaults_validate():
    f = FactoryConfig.model_validate(defaults_factory())
    assert (
        f.schema_version == "factory/v1"
        and f.budgets.job_usd == Decimal("10")
        and f.concurrency.global_ == 2
    )
    assert (
        f.apps["demo"].merge_policy == MergePolicy.LOCAL_MERGE and f.defaults.judge_sampling == 0.10
    )
    assert set(f.providers) == {"vertex", "gemini", "groq", "openrouter", "lm_studio", "ollama"}
    assert (f.providers["vertex"].kind, f.providers["vertex"].project_env) == (
        "vertex_ai",
        "GOOGLE_CLOUD_PROJECT",
    )


@pytest.mark.parametrize(
    "value, ok", [(0.0, True), (0.10, True), (1.0, True), (10, False), (-0.1, False)]
)
def test_judge_sampling_is_a_fraction(value, ok):
    if ok:
        assert Defaults(judge_sampling=value).judge_sampling == value
    else:
        with pytest.raises(ValidationError) as ei:
            Defaults(judge_sampling=value)
        assert ei.value.errors()[0]["loc"] == ("judge_sampling",)


def test_unknown_key_rejected():
    data = defaults_factory()
    data["apps"]["demo"]["merge_polcy"] = "x"
    with pytest.raises(ValidationError) as ei:
        FactoryConfig.model_validate(data)
    e = ei.value.errors()[0]
    assert e["type"] == "extra_forbidden" and e["loc"] == ("apps", "demo", "merge_polcy")


def test_local_app_rejects_pr_policy():
    with pytest.raises(ValidationError, match="local apps cannot use pr_manual"):
        AppDef.model_validate(
            {"repo_kind": "local", "repo_url": "demo", "merge_policy": "pr_manual"}
        )
    assert (
        AppDef.model_validate(
            {"repo_kind": "github", "repo_url": "https://github.com/o/r"}
        ).merge_policy
        == MergePolicy.PR_MANUAL
    )


def test_local_path_only_on_local_apps():
    ok = AppDef.model_validate(
        {"repo_kind": "local", "repo_url": "app", "local_path": "/Users/me/work/app"}
    )
    assert ok.local_path == "/Users/me/work/app" and ok.merge_policy == MergePolicy.LOCAL_MERGE
    assert AppDef.model_validate({"repo_kind": "local", "repo_url": "demo"}).local_path is None
    with pytest.raises(ValidationError, match="local_path needs repo_kind local"):
        AppDef.model_validate(
            {
                "repo_kind": "github",
                "repo_url": "https://github.com/o/r",
                "local_path": "/Users/me/x",
            }
        )


@pytest.mark.parametrize(
    "path",
    ["work/app", "~/work/app", "/Users/me/../root", "/Users//me/app", "/Users/me/app\n", ""],
)
def test_local_path_must_be_absolute_and_clean(path):
    with pytest.raises(ValidationError, match="local_path"):
        AppDef.model_validate({"repo_kind": "local", "repo_url": "app", "local_path": path})


@pytest.mark.parametrize(
    "env, ok",
    [
        ("GEMINI_API_KEY", True),
        ("BYO_ACME_API_KEY", True),
        ("LITELLM_MASTER_KEY", False),
        ("FACTORY_DATABASE_PASSWORD", False),
        ("GOOGLE_APPLICATION_CREDENTIALS", False),
        ("DATABASE_URL", False),
        ("OPENAI_API_KEY", False),
    ],
)
def test_api_key_env_names(env, ok):
    def make():
        return ProviderDef(
            kind="openai_compatible", api_key_env=env, base_url="https://api.groq.com/v1"
        )

    if ok:
        assert make().api_key_env == env
    else:
        with pytest.raises(ValidationError) as ei:
            make()
        assert ei.value.errors()[0]["loc"] == ("api_key_env",)
        assert "API_KEY" in ei.value.errors()[0]["msg"]


@pytest.mark.parametrize(
    "env, ok", [("GOOGLE_CLOUD_PROJECT", True), ("ACME_PROJECT", True), ("PROJECT_NUMBER", False)]
)
def test_project_env_names(env, ok):
    def make():
        return ProviderDef(kind="vertex_ai", project_env=env, location="global")

    if ok:
        assert make().project_env == env
    else:
        with pytest.raises(ValidationError) as ei:
            make()
        assert ei.value.errors()[0]["loc"] == ("project_env",)


def test_bad_slug_rejected():
    data = defaults_factory()
    data["apps"]["Demo_App"] = data["apps"].pop("demo")
    with pytest.raises(ValidationError) as ei:
        FactoryConfig.model_validate(data)
    assert ei.value.errors()[0]["loc"][:2] == ("apps", "Demo_App")


GOOD_REG = {
    "kind": "npm",
    "upstream": "artifact_registry",
    "url": "https://us-npm.pkg.dev/acme/js/",
    "scopes": ["@acme"],
}


def test_registry_def_valid():
    r = RegistryDef.model_validate(GOOD_REG)
    assert (r.kind, r.upstream, r.scopes, r.credential) == (
        "npm",
        "artifact_registry",
        ("@acme",),
        None,
    )
    t = RegistryDef.model_validate(
        {
            "kind": "pypi",
            "upstream": "token",
            "url": "https://pypi.example.com/simple/",
            "scopes": ["acme-"],
            "credential": "REGISTRY_ACME_TOKEN",
        }
    )
    assert t.credential == "REGISTRY_ACME_TOKEN"


@pytest.mark.parametrize(
    "change, msg",
    [
        (
            {"kind": "pypi", "upstream": "github_packages", "scopes": ["acme-"]},
            "GitHub Packages supports npm only",
        ),
        ({"upstream": "token"}, "credential is required"),
        ({"credential": "REGISTRY_A_TOKEN"}, "credential must be unset"),
        ({"upstream": "token", "credential": "LITELLM_MASTER_KEY"}, "must match"),
        ({"scopes": []}, "at least one scope"),
        ({"scopes": ["acme"]}, "npm scopes start with @"),
        ({"url": "http://us-npm.pkg.dev/acme/js/"}, "https"),
        ({"url": "https://user:pw@us-npm.pkg.dev/acme/js/"}, "credentials in the url"),
    ],
)
def test_registry_def_rejects(change, msg):
    with pytest.raises(ValidationError, match=msg):
        RegistryDef.model_validate({**GOOD_REG, **change})


def test_scope_belongs_to_one_registry():
    data = defaults_factory()
    data["registries"] = {
        "a": GOOD_REG,
        "b": {**GOOD_REG, "upstream": "github_packages", "url": "https://npm.pkg.github.com/"},
    }
    with pytest.raises(ValidationError, match=r"scope @acme is used by registries 'a' and 'b'"):
        FactoryConfig.model_validate(data)
