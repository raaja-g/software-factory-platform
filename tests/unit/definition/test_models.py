import copy
import json
from pathlib import Path

import pytest
import yaml

from swfactory.definition.models import (
    DefinitionPolicy,
    definition_json_schema,
    validate_definition,
)

DEFAULT_HOSTS = frozenset({"generativelanguage.googleapis.com", "api.groq.com", "openrouter.ai"})


def defaults_data() -> dict:
    root = Path("defaults")
    load = lambda p: yaml.safe_load(p.read_text())  # noqa: E731
    return {
        "factory": load(root / "factory.yaml"),
        "profiles": {p.stem: load(p) for p in sorted((root / "profiles").glob("*.yaml"))},
        "runners": {p.stem: load(p) for p in sorted((root / "runners").glob("*.yaml"))},
        "automations": {},
        "webhooks": {},
        "scorers": {},
    }


FIRST_ALIAS = next(iter(defaults_data()["factory"]["models"]))
GOOD_SCORER = {
    "labels": [
        {"value": "bad", "score": 0.0, "description": "d"},
        {"value": "good", "score": 1.0, "description": "d"},
    ],
    "passing_score": 0.5,
    "judge": FIRST_ALIAS,
    "applies_to": ["builder"],
    "rubric": "r",
}


def set_in(data, path, value):
    out = copy.deepcopy(data)
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return out


def issues_without(role):
    data = defaults_data()
    del data["profiles"]["team-gemini"]["roles"][role]
    return validate_definition(data)[1]


def with_image(img):
    data = set_in(defaults_data(), ("runners", "standard", "image"), img)
    # the other default runner is pinned so only the image under test can fail
    return set_in(data, ("runners", "web", "image"), "swfactory/runner-web@sha256:" + "b" * 64)


def test_defaults_validate():
    d, issues = validate_definition(defaults_data())
    assert issues == () and d is not None
    assert set(d.profiles) == {"team-gemini", "claude-vertex", "mixed-economy", "local"}
    assert d.app("demo").runner == "standard"
    with pytest.raises(KeyError):
        d.app("nope")


@pytest.mark.parametrize(
    "path, value, loc, msg",
    [
        (
            ("factory", "apps", "demo", "profile"),
            "nope",
            ("factory", "apps", "demo", "profile"),
            "unknown profile 'nope'",
        ),
        (
            ("profiles", "team-gemini", "roles", "builder", "model"),
            "ghost",
            ("profiles", "team-gemini", "roles", "builder", "model"),
            "unknown model alias 'ghost'",
        ),
        (
            ("factory", "models", FIRST_ALIAS, "provider"),
            "nowhere",
            ("factory", "models", FIRST_ALIAS, "provider"),
            "unknown provider 'nowhere'",
        ),
        (
            ("factory", "apps", "demo", "runner"),
            "gpu",
            ("factory", "apps", "demo", "runner"),
            "unknown runner 'gpu'",
        ),
        (
            ("factory", "defaults", "profile"),
            "nope",
            ("factory", "defaults", "profile"),
            "unknown profile 'nope'",
        ),
        (
            ("factory", "apps", "demo", "registries"),
            ["nope"],
            ("factory", "apps", "demo", "registries", 0),
            "unknown registry 'nope'",
        ),
    ],
)
def test_cross_refs(path, value, loc, msg):
    _, issues = validate_definition(set_in(defaults_data(), path, value))
    assert [(i.type, i.loc) for i in issues] == [("definition_ref", loc)]
    assert msg in issues[0].msg


def test_sensitive_app_keeps_security():
    data = set_in(
        set_in(defaults_data(), ("factory", "apps", "demo", "sensitive"), True),
        ("factory", "apps", "demo", "reviewers"),
        ["ux", "code"],
    )
    issues = validate_definition(data)[1]
    assert [(i.loc, "security" in i.msg) for i in issues] == [
        (("factory", "apps", "demo", "reviewers"), True)
    ]


def test_local_path_must_be_inside_host_repos_root():
    data = set_in(
        set_in(defaults_data(), ("factory", "apps", "demo", "local_path"), "/Users/me/other/app"),
        ("factory", "apps", "demo", "repo_url"),
        "app",
    )
    assert validate_definition(data)[1] == ()
    pol = DefinitionPolicy(DEFAULT_HOSTS, ("swfactory/",), host_repos_root="/Users/me/work")
    issues = validate_definition(data, pol)[1]
    assert [(i.type, i.loc) for i in issues] == [
        ("definition_ref", ("factory", "apps", "demo", "local_path"))
    ]
    assert "not inside FACTORY_HOST_REPOS_ROOT" in issues[0].msg
    sibling = set_in(data, ("factory", "apps", "demo", "local_path"), "/Users/me/workshop/app")
    assert [i.loc for i in validate_definition(sibling, pol)[1]] == [
        ("factory", "apps", "demo", "local_path")
    ]
    for ok in ("/Users/me/work/app", "/Users/me/work"):
        data_ok = set_in(data, ("factory", "apps", "demo", "local_path"), ok)
        assert validate_definition(data_ok, pol)[1] == ()


def test_scorer_labels_straddle_pass():
    s = {
        "labels": [
            {"value": "ok", "score": 0.8, "description": "d"},
            {"value": "great", "score": 1.0, "description": "d"},
        ],
        "passing_score": 0.5,
        "judge": FIRST_ALIAS,
        "applies_to": ["builder"],
        "rubric": "r",
    }
    issues = validate_definition(set_in(defaults_data(), ("scorers", "s"), s))[1]
    assert [i.loc for i in issues] == [("scorers", "s", "labels")]
    assert "below passing_score" in issues[0].msg


def test_scorer_sampling_rate_is_a_fraction():
    d, _ = validate_definition(set_in(defaults_data(), ("scorers", "s"), GOOD_SCORER))
    assert d is not None and d.scorers["s"].sampling_rate == 0.10
    bad = {**GOOD_SCORER, "sampling_rate": 10}
    issues = validate_definition(set_in(defaults_data(), ("scorers", "s"), bad))[1]
    assert [(i.loc, i.type) for i in issues] == [
        (("scorers", "s", "sampling_rate"), "less_than_equal")
    ]


def test_profile_covers_loop_roles():
    assert [(i.loc, i.msg) for i in issues_without("builder")] == [
        (("profiles", "team-gemini", "roles"), "profile 'team-gemini' has no builder")
    ]
    for r in ("judge", "improver", "requirements"):
        assert issues_without(r) == ()


def test_profile_name_is_a_slug():
    data = set_in(
        defaults_data(), ("profiles", "Team Gemini"), defaults_data()["profiles"]["team-gemini"]
    )
    assert ("profiles", "Team Gemini") in [i.loc for i in validate_definition(data)[1]]


def test_keyed_provider_base_url_allowlist():
    data = set_in(
        defaults_data(), ("factory", "providers", "groq", "base_url"), "https://evil.example/v1"
    )
    issues = validate_definition(data)[1]
    assert [i.loc for i in issues] == [("factory", "providers", "groq", "base_url")]
    assert "'evil.example' is not in FACTORY_ALLOWED_PROVIDER_HOSTS" in issues[0].msg
    assert (
        validate_definition(data, DefinitionPolicy(frozenset({"evil.example"}), ("swfactory/",)))[1]
        == ()
    )
    d = validate_definition(defaults_data())[0]
    assert d is not None
    assert d.factory.providers["lm_studio"].base_url == "http://host.docker.internal:1234/v1"


@pytest.mark.parametrize(
    "provider, url, msg",
    [
        ("ollama", "http://169.254.169.254/computeMetadata/v1", "metadata or link-local address"),
        ("lm_studio", "http://metadata.google.internal/v1", "metadata or link-local address"),
        ("ollama", "http://127.0.0.1:11434", "loopback"),
        ("lm_studio", "http://docker-proxy:2375/v1", "factory service"),
        ("ollama", "http://10.0.0.5:11434", "not in FACTORY_ALLOWED_LOCAL_MODEL_HOSTS"),
    ],
)
def test_every_provider_base_url_checked(provider, url, msg):
    data = set_in(defaults_data(), ("factory", "providers", provider, "base_url"), url)
    issues = validate_definition(data)[1]
    assert [i.loc for i in issues] == [("factory", "providers", provider, "base_url")]
    assert msg in issues[0].msg


def test_listed_local_host_allowed():
    data = set_in(
        defaults_data(), ("factory", "providers", "ollama", "base_url"), "http://10.0.0.5:11434"
    )
    pol = DefinitionPolicy(
        frozenset(),
        ("swfactory/",),
        allowed_local_model_hosts=frozenset({"host.docker.internal", "10.0.0.5"}),
    )
    assert validate_definition(data, pol)[1] == ()


@pytest.mark.parametrize(
    "image, digest_required, msg",
    [
        ("docker.io/evil/img:latest", False, "not under an allowed image prefix"),
        ("swfactory/runner:0.1.0", True, "must be pinned by @sha256 digest"),
        ("swfactory/runner@sha256:" + "a" * 64, True, None),
    ],
)
def test_runner_image_policy(image, digest_required, msg):
    pol = DefinitionPolicy(DEFAULT_HOSTS, ("swfactory/",), digest_required)
    issues = validate_definition(with_image(image), pol)[1]
    if msg is None:
        assert issues == ()
    else:
        assert [i.loc for i in issues] == [("runners", "standard", "image")]
        assert msg in issues[0].msg


@pytest.mark.parametrize(
    "hosts, prefixes, local",
    [
        (frozenset({""}), ("swfactory/",), frozenset({"host.docker.internal"})),
        (DEFAULT_HOSTS, ("",), frozenset({"host.docker.internal"})),
        (DEFAULT_HOSTS, ("swfactory",), frozenset({"host.docker.internal"})),
        (DEFAULT_HOSTS, ("swfactory/",), frozenset({""})),
    ],
)
def test_policy_refuses_blank_or_open_entries(hosts, prefixes, local):
    with pytest.raises(ValueError):
        DefinitionPolicy(hosts, prefixes, allowed_local_model_hosts=local)


def test_policy_host_repos_root_must_be_absolute():
    with pytest.raises(ValueError):
        DefinitionPolicy(DEFAULT_HOSTS, ("swfactory/",), host_repos_root="work")


def test_webhook_secret_env_name():
    w = {
        "auth_mode": "signature",
        "signature_scheme": "github",
        "secret_env": "LITELLM_MASTER_KEY",
        "delivery_id_header": "X-GitHub-Delivery",
    }
    issues = validate_definition(set_in(defaults_data(), ("webhooks", "w"), w))[1]
    assert [(i.loc, i.type) for i in issues] == [
        (("webhooks", "w", "secret_env"), "string_pattern_mismatch")
    ]


def test_automation_cron_lives_in_filter():
    a = {
        "enabled": True,
        "trigger": {"provider": "schedule", "event": "cron_fired", "filter": {"cron": "0 3 * * 1"}},
        "app": "demo",
        "feature_template": "Weekly dependency bump",
    }
    d, issues = validate_definition(set_in(defaults_data(), ("automations", "weekly"), a))
    assert issues == () and d is not None
    assert d.automations["weekly"].trigger.filter["cron"] == "0 3 * * 1"
    bad = {**a, "trigger": {**a["trigger"], "schedule": "0 3 * * 1"}}
    issues = validate_definition(set_in(defaults_data(), ("automations", "weekly"), bad))[1]
    assert [(i.loc, i.type) for i in issues] == [
        (("automations", "weekly", "trigger", "schedule"), "extra_forbidden")
    ]


def test_schema_file_is_current():
    s = definition_json_schema()
    assert json.loads(Path("schema/definition.schema.json").read_text()) == s
    assert s["additionalProperties"] is False and "schemaVersion" in s["properties"]
