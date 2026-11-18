"""factory/v1 models for ``factory.yaml``."""

import re
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from swfactory.domain.types import DEFAULT_REVIEWERS, MergePolicy, TriageMode

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
    lease_s: int = 90
    lease_renew_s: int = 30
    definition_poll_s: int = 300
    router_timeout_s: int = 2
    judge_sampling: float = Field(default=0.10, ge=0, le=1)


class Budgets(StrictModel):
    job_usd: Decimal = Decimal("10")
    daily_usd: Decimal = Decimal("150")


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


class ModelAliasDef(StrictModel):
    provider: str
    upstream_id: str
    strength: Literal["strong", "mid", "cheap", "local"]
    price_in_per_mtok: Decimal | None = None
    price_out_per_mtok: Decimal | None = None
    context_window: int | None = None


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
    ui_globs: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = DEFAULT_REVIEWERS
    registries: tuple[str, ...] = ()
    sensitive: bool = False
    max_rounds: int | None = None
    max_fix_attempts: int | None = None
    budget_usd: Decimal | None = None
    concurrency: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "AppDef":
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
