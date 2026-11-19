"""factory/v1 models for ``factory.yaml``."""

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import models_json_schema

from swfactory.costs.pricing import STEP_MAX_DEFAULT_USD, STEP_MIN_USD
from swfactory.domain.types import (
    DEFAULT_REVIEWERS,
    FactoryMode,
    MergePolicy,
    Role,
    SelectionMode,
    TriageMode,
)
from swfactory.sandbox.base import RunnerSpec
from swfactory.settings import Settings

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]

_API_KEY_ENV = re.compile(r"^(GEMINI|GROQ|OPENROUTER|BYO_[A-Z0-9_]+)_API_KEY$")
_PROJECT_ENV = re.compile(r"^(GOOGLE_CLOUD_PROJECT|[A-Z][A-Z0-9_]*_PROJECT)$")
_CREDENTIAL = re.compile(r"^REGISTRY_[A-Z0-9_]+_TOKEN$")
_NPM_SCOPE = re.compile(r"^@[a-z0-9][a-z0-9._-]*$")
_PYPI_SCOPE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Defaults(StrictModel):
    profile: str = "team-gemini"
    max_rounds: int = 2
    builder_continuity: Literal["fresh", "resume"] = "fresh"
    max_fix_attempts: int = 1
    step_timeouts_min: dict[str, int] = Field(
        default_factory=lambda: {
            "triage": 1,
            "spec": 15,
            "build": 60,
            "verify": 15,
            "review": 20,
            "approve": 10,
            "judge": 5,
            "requirements": 20,
            "improve": 60,
        }
    )
    max_backlog_items: int = Field(default=30, ge=1, le=100)
    engine_retries: int = 2
    engine_retry_backoff_s: tuple[int, int] = (10, 60)
    contract_repair_attempts: int = 1
    max_job_crashes: int = Field(default=3, ge=1)
    lease_s: int = 90
    lease_renew_s: int = 30
    definition_poll_s: int = 300
    router_timeout_s: int = 2
    judge_sampling: float = Field(default=0.10, ge=0, le=1)
    waiting_expiry_days: int = Field(default=14, ge=0, le=365)  # 0 disables
    max_turns: dict[str, int] = Field(default_factory=lambda: {"builder": 80, "default": 40})
    # Claude Code's safety-classifier fallbacks (section 9.3): model aliases every claude_code
    # run key also allows
    claude_classifier_fallbacks: tuple[str, ...] = ()

    @field_validator("max_turns")
    @classmethod
    def _max_turns(cls, v: dict[str, int]) -> dict[str, int]:
        if "default" not in v:
            raise ValueError("max_turns needs a 'default' entry")
        if any(n < 1 for n in v.values()):
            raise ValueError("max_turns values must be at least 1")
        return v


def max_turns_for(defaults: Defaults, role: str) -> int:
    """Turn limit of a role: its own entry, else the ``default`` entry."""
    return defaults.max_turns.get(str(role), defaults.max_turns["default"])


class Budgets(StrictModel):
    job_usd: Decimal = Decimal("10")
    job_max_usd: Decimal = Decimal("25")
    daily_usd: Decimal = Decimal("150")
    app_monthly_usd: dict[str, Decimal] = {}
    step_max_usd: dict[str, Decimal] = Field(
        default_factory=lambda: {r.value: v for r, v in STEP_MAX_DEFAULT_USD.items()}
    )

    @model_validator(mode="after")
    def _job_max(self) -> "Budgets":
        if self.job_max_usd < self.job_usd:
            raise ValueError("job_max_usd must be at least job_usd")
        return self

    @field_validator("step_max_usd", mode="before")
    @classmethod
    def _step_max(cls, v: Any) -> Any:
        if not isinstance(v, Mapping):
            return v
        out = {r.value: d for r, d in STEP_MAX_DEFAULT_USD.items()}
        for key, raw in v.items():
            try:
                role = Role(key)
            except ValueError:
                raise ValueError(f"step_max_usd: unknown role {key}") from None
            val = Decimal(str(raw))
            if val < STEP_MIN_USD[role]:
                raise ValueError(
                    f"step_max_usd: {key} {val} is below its step minimum {STEP_MIN_USD[role]}"
                )
            out[key] = val
        return out


class Concurrency(StrictModel):
    global_: int = Field(default=2, alias="global")
    per_app: int = 1


class ProviderDef(StrictModel):
    kind: Literal[
        "vertex_ai",
        "gemini",
        "groq",
        "openrouter",
        "lm_studio",
        "ollama",
        "openai_compatible",
        "anthropic_compatible",
    ]
    api_key_env: str | None = None
    base_url: str | None = None
    project: str | None = None
    project_env: str | None = None
    location: str | None = None
    subscription_backed: bool = False

    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env(cls, v: str | None) -> str | None:
        if v is not None and not _API_KEY_ENV.match(v):
            raise ValueError(
                "api_key_env must match ^(GEMINI|GROQ|OPENROUTER|BYO_[A-Z0-9_]+)_API_KEY$"
            )
        return v

    @field_validator("project_env")
    @classmethod
    def _check_project_env(cls, v: str | None) -> str | None:
        if v is not None and not _PROJECT_ENV.match(v):
            raise ValueError("project_env must be GOOGLE_CLOUD_PROJECT or end in _PROJECT")
        return v

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str | None) -> str | None:
        if v is not None and urlsplit(v).scheme not in ("http", "https"):
            raise ValueError("base_url must be an http or https url")
        return v

    @field_validator("project")
    @classmethod
    def _check_project(cls, v: str | None) -> str | None:
        # A literal project id only. "os.environ/NAME" would make the gateway read any of its own
        # environment variables (its secrets) and send the value to the provider.
        if v is not None and not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", v):
            raise ValueError(
                "project must be a GCP project id (lowercase letters, digits, hyphens)"
            )
        return v

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[a-z0-9-]+", v):
            raise ValueError("location must be a region name such as global or us-central1")
        return v


class ModelAliasDef(StrictModel):
    provider: str
    upstream_id: str
    strength: Literal["strong", "mid", "cheap", "local"]
    price_in_per_mtok: Decimal | None = None
    price_out_per_mtok: Decimal | None = None
    context_window: int | None = None
    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)


class RegistryDef(StrictModel):
    kind: Literal["npm", "pypi"]
    upstream: Literal["artifact_registry", "github_packages", "token"]
    url: str
    scopes: tuple[str, ...] = ()
    credential: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("url must be https")
        if parts.username or parts.password:
            raise ValueError("credentials in the url are not allowed")
        return v

    @model_validator(mode="after")
    def _check(self) -> "RegistryDef":
        if self.upstream == "github_packages" and self.kind == "pypi":
            raise ValueError("GitHub Packages supports npm only")
        if not self.scopes:
            raise ValueError("at least one scope is required")
        for s in self.scopes:
            if self.kind == "npm":
                if not s.startswith("@"):
                    raise ValueError(f"npm scopes start with @: {s!r}")
                if not _NPM_SCOPE.match(s):
                    raise ValueError(f"scope {s!r} must match {_NPM_SCOPE.pattern}")
            elif not _PYPI_SCOPE.match(s):
                raise ValueError(f"scope {s!r} must match {_PYPI_SCOPE.pattern}")
        if self.upstream == "token":
            if self.credential is None:
                raise ValueError("credential is required for upstream token")
            if not _CREDENTIAL.match(self.credential):
                raise ValueError(f"credential must match {_CREDENTIAL.pattern}")
        elif self.credential is not None:
            raise ValueError(f"credential must be unset for upstream {self.upstream}")
        return self


def _check_local_path(v: str) -> str:
    if (
        not v.startswith("/")
        or len(v) > 4096
        or "//" in v
        or ".." in v.split("/")
        or any(ord(c) < 32 or ord(c) == 127 for c in v)
    ):
        raise ValueError(
            "local_path must be an absolute path with no '..' segment, no '//' "
            "and no control characters, at most 4096 characters"
        )
    return v


_SHOT_NAME = re.compile(r"^[a-z0-9-]+$")


class SmokeConfig(StrictModel):
    """Browser smoke test: the app is always reached at http://127.0.0.1:<port> in the gate box."""

    start_command: str
    port: int = Field(ge=1, le=65535)
    paths: list[str]
    screenshots: list[str]
    ready_timeout_s: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "SmokeConfig":
        if not self.paths or len(self.paths) != len(self.screenshots):
            raise ValueError("paths and screenshots must be non-empty and the same length")
        if len(set(self.screenshots)) != len(self.screenshots):
            raise ValueError("screenshot names must be unique")
        for n in self.screenshots:
            if not _SHOT_NAME.fullmatch(n):
                raise ValueError(f"screenshot name {n!r} must match ^[a-z0-9-]+$")
        for p in self.paths:
            if not p.startswith("/"):
                raise ValueError(f"smoke path {p!r} must start with '/'")
        return self


class AppAccess(StrictModel):
    """Per-app access lists (emails); an email in several lists gets the highest."""

    viewers: tuple[str, ...] = ()
    submitters: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()

    @field_validator("viewers", "submitters", "approvers", mode="before")
    @classmethod
    def _emails(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return tuple(x.strip().lower() if isinstance(x, str) else x for x in v)
        return v


class AppDef(StrictModel):
    repo_kind: Literal["github", "local"]
    repo_url: str
    local_path: str | None = None
    default_branch: str = "main"
    merge_policy: MergePolicy | None = None
    profile: str | None = None
    allowed_profiles: tuple[str, ...] = ()
    runner: str = "standard"
    test_command: str | None = None
    protected_paths: tuple[str, ...] = ()
    ui_globs: tuple[str, ...] = (
        "**/*.html",
        "**/*.htm",
        "**/*.css",
        "**/*.scss",
        "**/*.js",
        "**/*.jsx",
        "**/*.ts",
        "**/*.tsx",
        "**/*.vue",
        "**/*.svelte",
        "**/templates/**",
        "**/static/**",
    )
    reviewers: tuple[str, ...] = DEFAULT_REVIEWERS
    reviewer_relevance: bool = False
    registries: tuple[str, ...] = ()
    sensitive: bool = False
    max_rounds: int | None = None
    max_fix_attempts: int | None = None
    budget_usd: Decimal | None = None
    concurrency: int | None = None
    smoke: SmokeConfig | None = None
    access: AppAccess | None = None

    @model_validator(mode="after")
    def _check(self) -> "AppDef":
        if self.smoke is not None and self.runner != "web":
            raise ValueError("smoke requires runner web")
        if self.local_path is not None:
            if self.repo_kind != "local":
                raise ValueError("local_path needs repo_kind local")
            _check_local_path(self.local_path)
        policy = self.merge_policy
        if policy is None:
            policy = MergePolicy.LOCAL_MERGE if self.repo_kind == "local" else MergePolicy.PR_MANUAL
            object.__setattr__(self, "merge_policy", policy)
        elif self.repo_kind == "local" and policy != MergePolicy.LOCAL_MERGE:
            raise ValueError(f"local apps cannot use {policy.value}")
        return self


class TriageMatch(StrictModel):
    keywords: tuple[str, ...] = ()
    globs: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


class TriageRule(StrictModel):
    name: str | None = None
    match: TriageMatch = TriageMatch()
    set: dict[Literal["kind", "size", "risk", "touches_ui"], str | bool] = {}
    profile: str | None = None
    budget_usd: Decimal | None = None
    reviewers: tuple[str, ...] | None = None
    require_human_approval: bool = False


class TriageConfig(StrictModel):
    mode: TriageMode = TriageMode.RULES
    rules: tuple[TriageRule, ...] = ()


class ImproverConfig(StrictModel):
    enabled: bool = False
    failed_run_threshold: int = 25
    max_failure_age_days: int = 7
    bench_suite: str | None = None


class FactoryConfig(StrictModel):
    schema_version: Literal["factory/v1"] = Field(alias="schemaVersion")
    name: str
    defaults: Defaults = Defaults()
    budgets: Budgets = Budgets()
    concurrency: Concurrency = Concurrency()
    providers: dict[str, ProviderDef]
    registries: dict[Slug, RegistryDef] = {}
    models: dict[str, ModelAliasDef]
    apps: dict[Slug, AppDef]
    triage: TriageConfig = TriageConfig()
    improver: ImproverConfig = ImproverConfig()

    @model_validator(mode="after")
    def _scopes_unique(self) -> "FactoryConfig":
        seen: dict[str, str] = {}
        for rname, reg in self.registries.items():
            for scope in reg.scopes:
                if scope in seen:
                    raise ValueError(
                        f"scope {scope} is used by registries '{seen[scope]}' and '{rname}'"
                    )
                seen[scope] = rname
        return self


# --------------------------------------------------------------------------
# Per-file models
# --------------------------------------------------------------------------

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

FACTORY_SERVICE_NAMES = frozenset(
    {
        "postgres",
        "docker-proxy",
        "gateway",
        "gateway-edge",
        "gateway-db",
        "gateway-migrate",
        "migrate",
        "api",
        "orchestrator",
        "egress",
        "router",
        "phoenix",
        "gateway-auth",
        "pkg-mirror",
        "pkg-mirror-pypi",
        "pkg-mirror-auth",
    }
)

_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata"})
_LOOPBACK_HOSTS = frozenset({"localhost"})


class Tier(StrictModel):
    label: str = Field(pattern=r"^[A-Z]$")
    model: str
    description: str


class RoleProfile(StrictModel):
    engine: Literal["opencode", "claude_code", "gemini_cli", "direct", "fake"]
    model: str
    mode: SelectionMode = SelectionMode.FIXED
    tiers: tuple[Tier, ...] = Field(default=(), max_length=6)
    fallback: tuple[str, ...] = ()
    sticky: bool | None = None
    allow_experimental: bool = False


class Profile(StrictModel):
    description: str = ""
    continuity: Literal["fresh", "resume"] | None = None
    roles: dict[Role, RoleProfile]


class RoleFile(StrictModel):
    frontmatter: dict[str, Any] = {}
    body: str = ""


class RunnerDef(StrictModel):
    image: str
    cpus: float = 2.0
    memory_mb: int = 4096
    pids: int = 512
    allowed_hosts: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    max_work_gb: int = Field(20, ge=1, le=1000)


class AutomationTrigger(StrictModel):
    provider: str
    event: str
    filter: dict[str, Any] = {}


class AutomationDef(StrictModel):
    enabled: bool = True
    trigger: AutomationTrigger
    app: str
    feature_template: str
    profile: str | None = None


class WebhookDef(StrictModel):
    auth_mode: str
    signature_scheme: Literal["github", "sentry", "standard_webhooks", "stripe"]
    secret_env: str = Field(pattern=r"^WEBHOOK_[A-Z0-9_]+_SECRET$")
    delivery_id_header: str


class ScorerLabel(StrictModel):
    value: str
    score: float = Field(ge=0, le=1)
    description: str


class ScorerDef(StrictModel):
    labels: tuple[ScorerLabel, ...] = Field(min_length=1, max_length=20)
    passing_score: float = Field(ge=0, le=1)
    sampling_rate: float = Field(default=0.10, ge=0, le=1)
    judge: str
    applies_to: tuple[str, ...] = ()
    rubric: str = ""


class BenchConfigDef(StrictModel):
    name: str
    profile: str


class BenchTaskDef(StrictModel):
    title: str
    feature: str
    success_criteria: tuple[str, ...] = ()
    repo: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_sha: str | None = None
    definition_sha: str
    hidden_checks: tuple[str, ...] = ()
    check_command: str | None = None
    scorers: tuple[str, ...] = ()


class BenchSuiteDef(StrictModel):
    description: str = ""
    budget_usd: Decimal | None = None
    max_parallel: int = 1
    configurations: tuple[BenchConfigDef, ...] = Field(min_length=1, max_length=6)
    tasks: dict[str, BenchTaskDef] = {}


class Definition(StrictModel):
    factory: FactoryConfig
    profiles: dict[str, Profile] = {}
    roles: dict[Role, RoleFile] = {}
    runners: dict[str, RunnerDef] = {}
    automations: dict[str, AutomationDef] = {}
    webhooks: dict[str, WebhookDef] = {}
    scorers: dict[str, ScorerDef] = {}
    benchmarks: dict[str, BenchSuiteDef] = {}

    def app(self, slug: str) -> AppDef:
        return self.factory.apps[slug]


# --------------------------------------------------------------------------
# Policy and validation
# --------------------------------------------------------------------------


def _setting_default(name: str) -> tuple[str, ...]:
    default = Settings.model_fields[name].default
    return tuple(default)


@dataclass(frozen=True)
class DefinitionPolicy:
    allowed_provider_hosts: frozenset[str] = frozenset(_setting_default("allowed_provider_hosts"))
    allowed_image_prefixes: tuple[str, ...] = _setting_default("allowed_image_prefixes")
    require_image_digest: bool = False
    allowed_local_model_hosts: frozenset[str] = frozenset({"host.docker.internal"})
    host_repos_root: str | None = None

    def __post_init__(self) -> None:
        if any(not h for h in self.allowed_provider_hosts):
            raise ValueError("allowed_provider_hosts must not contain an empty host")
        if any(not h for h in self.allowed_local_model_hosts):
            raise ValueError("allowed_local_model_hosts must not contain an empty host")
        for p in self.allowed_image_prefixes:
            if not p or not p.endswith("/"):
                raise ValueError(f"image prefix {p!r} must be non-empty and end with '/'")
        if self.host_repos_root is not None and not self.host_repos_root.startswith("/"):
            raise ValueError("host_repos_root must be an absolute path")

    @classmethod
    def from_settings(cls, s: Settings) -> "DefinitionPolicy":
        return cls(
            allowed_provider_hosts=frozenset(s.allowed_provider_hosts),
            allowed_image_prefixes=tuple(s.allowed_image_prefixes),
            require_image_digest=s.mode is FactoryMode.SERVER,
            allowed_local_model_hosts=frozenset(s.allowed_local_model_hosts),
            host_repos_root=s.host_repos_root,
        )


@dataclass(frozen=True)
class DefinitionIssue:
    loc: tuple[str | int, ...]
    type: str
    msg: str


_REQUIRED_ROLES = (
    Role.SPEC_WRITER,
    Role.BUILDER,
    Role.REVIEWER_SECURITY,
    Role.REVIEWER_UX,
    Role.REVIEWER_UI,
    Role.REVIEWER_CODE,
    Role.APPROVER,
)

_SHA256_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")


def _base_url_problem(url: str, keyed: bool, policy: DefinitionPolicy) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return "base_url has no host"
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if host in _METADATA_HOSTS or (ip is not None and ip.is_link_local):
        return f"'{host}' is a metadata or link-local address"
    if host in _LOOPBACK_HOSTS or (ip is not None and (ip.is_loopback or ip.is_unspecified)):
        return f"'{host}' is a loopback or unspecified address"
    if host in FACTORY_SERVICE_NAMES:
        return f"'{host}' is a factory service"
    scheme = urlsplit(url).scheme
    if keyed:
        if scheme != "https":
            return "a provider with an API key must use https"
        if host not in policy.allowed_provider_hosts:
            return f"'{host}' is not in FACTORY_ALLOWED_PROVIDER_HOSTS"
        return None
    if host in policy.allowed_local_model_hosts:
        return None
    if scheme == "https" and host in policy.allowed_provider_hosts:
        return None
    return f"'{host}' is not in FACTORY_ALLOWED_LOCAL_MODEL_HOSTS"


def check_references(defn: Definition, policy: DefinitionPolicy) -> tuple[DefinitionIssue, ...]:
    out: list[DefinitionIssue] = []

    def ref(loc: tuple[str | int, ...], msg: str) -> None:
        out.append(DefinitionIssue(loc, "definition_ref", msg))

    f = defn.factory

    def check_profile(loc: tuple[str | int, ...], name: str | None) -> None:
        if name is not None and name not in defn.profiles:
            ref(loc, f"unknown profile '{name}'")

    def check_model(loc: tuple[str | int, ...], alias: str) -> None:
        if alias not in f.models:
            ref(loc, f"unknown model alias '{alias}'")

    for pname in defn.profiles:
        if not PROFILE_NAME_RE.match(pname):
            ref(("profiles", pname), f"profile name '{pname}' must match {PROFILE_NAME_RE.pattern}")

    check_profile(("factory", "defaults", "profile"), f.defaults.profile)
    for i, alias in enumerate(f.defaults.claude_classifier_fallbacks):
        check_model(("factory", "defaults", "claude_classifier_fallbacks", i), alias)

    for pname, prov in f.providers.items():
        if prov.base_url is not None:
            problem = _base_url_problem(prov.base_url, prov.api_key_env is not None, policy)
            if problem:
                ref(("factory", "providers", pname, "base_url"), problem)

    for alias, m in f.models.items():
        if m.provider not in f.providers:
            ref(("factory", "models", alias, "provider"), f"unknown provider '{m.provider}'")

    for slug in f.budgets.app_monthly_usd:
        if slug not in f.apps:
            ref(
                ("factory", "budgets", "app_monthly_usd"),
                f"app_monthly_usd: unknown app {slug}",
            )

    for slug, app in f.apps.items():
        base = ("factory", "apps", slug)
        check_profile((*base, "profile"), app.profile)
        for i, name in enumerate(app.allowed_profiles):
            check_profile((*base, "allowed_profiles", i), name)
        if app.runner not in defn.runners:
            ref((*base, "runner"), f"unknown runner '{app.runner}'")
        for i, reg in enumerate(app.registries):
            if reg not in f.registries:
                ref((*base, "registries", i), f"unknown registry '{reg}'")
        bad = [r for r in app.reviewers if r not in DEFAULT_REVIEWERS]
        if bad:
            ref((*base, "reviewers"), f"unknown reviewer {bad[0]!r}; allowed: {DEFAULT_REVIEWERS}")
        elif app.sensitive and "security" not in app.reviewers:
            ref((*base, "reviewers"), "a sensitive app must keep the security reviewer")
        root = policy.host_repos_root
        if root is not None and app.local_path is not None:
            rparts = PurePosixPath(root).parts
            if PurePosixPath(app.local_path).parts[: len(rparts)] != rparts:
                ref(
                    (*base, "local_path"),
                    f"local_path '{app.local_path}' is not inside FACTORY_HOST_REPOS_ROOT",
                )

    for i, rule in enumerate(f.triage.rules):
        check_profile(("factory", "triage", "rules", i, "profile"), rule.profile)

    for pname, prof in defn.profiles.items():
        for role, rp in prof.roles.items():
            base = ("profiles", pname, "roles", role.value)
            check_model((*base, "model"), rp.model)
            for i, t in enumerate(rp.tiers):
                check_model((*base, "tiers", i, "model"), t.model)
            for i, fb in enumerate(rp.fallback):
                check_model((*base, "fallback", i), fb)
        for role in _REQUIRED_ROLES:
            if role not in prof.roles:
                ref(("profiles", pname, "roles"), f"profile '{pname}' has no {role.value}")

    for rname, runner in defn.runners.items():
        loc = ("runners", rname, "image")
        if not any(runner.image.startswith(p) for p in policy.allowed_image_prefixes):
            ref(loc, f"image '{runner.image}' is not under an allowed image prefix")
        elif policy.require_image_digest and not _SHA256_SUFFIX.search(runner.image):
            ref(loc, f"image '{runner.image}' must be pinned by @sha256 digest")

    for aname, auto in defn.automations.items():
        check_profile(("automations", aname, "profile"), auto.profile)
        if auto.app not in f.apps:
            ref(("automations", aname, "app"), f"unknown app '{auto.app}'")

    for sname, sc in defn.scorers.items():
        check_model(("scorers", sname, "judge"), sc.judge)
        scores = [lb.score for lb in sc.labels]
        if not any(x < sc.passing_score for x in scores):
            ref(
                ("scorers", sname, "labels"),
                f"no label scores below passing_score {sc.passing_score}",
            )
        elif not any(x >= sc.passing_score for x in scores):
            ref(
                ("scorers", sname, "labels"),
                f"no label reaches passing_score {sc.passing_score}",
            )

    for bname, suite in defn.benchmarks.items():
        for i, cfg in enumerate(suite.configurations):
            check_profile(("benchmarks", bname, "configurations", i, "profile"), cfg.profile)

    return tuple(out)


def validate_definition(
    data: Mapping[str, Any], policy: DefinitionPolicy | None = None
) -> tuple[Definition | None, tuple[DefinitionIssue, ...]]:
    policy = policy or DefinitionPolicy()
    try:
        defn = Definition.model_validate(data)
    except ValidationError as e:
        return None, tuple(
            DefinitionIssue(tuple(err["loc"]), err["type"], err["msg"]) for err in e.errors()
        )
    issues = check_references(defn, policy)
    return defn, issues


# --------------------------------------------------------------------------
# JSON Schema
# --------------------------------------------------------------------------


def definition_json_schema() -> dict[str, Any]:
    models = [
        FactoryConfig,
        Profile,
        RoleFile,
        RunnerDef,
        AutomationDef,
        WebhookDef,
        ScorerDef,
        BenchSuiteDef,
    ]
    _, schema = models_json_schema([(m, "validation") for m in models], by_alias=True)
    defs: dict[str, Any] = schema["$defs"]
    root = dict(defs.pop("FactoryConfig"))
    root["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    root["$defs"] = defs
    return root


def write_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(definition_json_schema(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    write_schema(Path("schema/definition.schema.json"))


def runner_spec(defn: Definition, name: str) -> RunnerSpec:
    r = defn.runners[name]
    return RunnerSpec(
        name=name,
        image=r.image,
        cpus=r.cpus,
        memory_mb=r.memory_mb,
        pids=r.pids,
        allowed_hosts=tuple(r.allowed_hosts),
        max_work_gb=r.max_work_gb,
    )
