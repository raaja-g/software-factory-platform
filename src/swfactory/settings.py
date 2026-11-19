"""Environment-driven settings."""

import os
import socket
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import (
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from swfactory.domain.types import FactoryMode
from swfactory.logging import register_secret


def split_csv(value: str | Sequence[str]) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else value
    return tuple(s for s in (i.strip() for i in items) if s)


# Paths the gateway-edge proxy forwards to the gateway (docker/gateway-edge.conf is checked
# against this tuple). Everything else is refused with 403.
EDGE_ALLOWED_PATHS: tuple[str, ...] = (
    r"^/v1/chat/completions$",
    r"^/v1/messages$",
    r"^/v1/messages/count_tokens$",
    r"^/v1/models$",
    r"^/v1beta/models/[^/]+:(generateContent|streamGenerateContent|countTokens)$",
)

CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(split_csv)]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", populate_by_name=True
    )

    mode: FactoryMode = Field(default=FactoryMode.PERSONAL, validation_alias="FACTORY_MODE")
    database_url: SecretStr = Field(validation_alias="FACTORY_DATABASE_URL")
    database_password_file: Path | None = Field(
        default=None, validation_alias="FACTORY_DATABASE_PASSWORD_FILE"
    )
    definition_source: str | None = Field(default=None, validation_alias="FACTORY_DEFINITION")
    definition_ref: str = Field(default="main", validation_alias="FACTORY_DEFINITION_REF")
    log_level: str = Field(default="INFO", validation_alias="FACTORY_LOG_LEVEL")
    worker_id: str = Field(default_factory=_default_worker_id, validation_alias="FACTORY_WORKER_ID")
    allowed_provider_hosts: CsvTuple = Field(
        default=("generativelanguage.googleapis.com", "api.groq.com", "openrouter.ai"),
        validation_alias="FACTORY_ALLOWED_PROVIDER_HOSTS",
    )
    allowed_local_model_hosts: CsvTuple = Field(
        default=("host.docker.internal",), validation_alias="FACTORY_ALLOWED_LOCAL_MODEL_HOSTS"
    )
    allowed_image_prefixes: CsvTuple = Field(
        default=("swfactory/",), validation_alias="FACTORY_ALLOWED_IMAGE_PREFIXES"
    )
    artifacts_dir: Path = Field(
        default=Path("/var/lib/factory/artifacts"), validation_alias="FACTORY_ARTIFACTS_DIR"
    )
    host_repos_root: str | None = Field(default=None, validation_alias="FACTORY_HOST_REPOS_ROOT")
    host_home: str | None = Field(default=None, validation_alias="FACTORY_HOST_HOME")
    docker_host: str = Field(
        default_factory=lambda: os.environ.get("DOCKER_HOST") or "tcp://docker-proxy:2375",
        validation_alias="FACTORY_DOCKER_HOST",
    )
    repos_dir: str = Field(default="/repos", validation_alias="FACTORY_REPOS_DIR")
    max_bundle_mb: int = Field(default=2048, validation_alias="FACTORY_MAX_BUNDLE_MB")
    gitops_timeout_s: int = Field(default=900, validation_alias="FACTORY_GITOPS_TIMEOUT_S")
    disk_high_water_pct: int = Field(
        default=85, ge=50, le=99, validation_alias="FACTORY_DISK_HIGH_WATER_PCT"
    )
    exec_output_max_bytes: int = Field(
        default=2_000_000, ge=10_000, validation_alias="FACTORY_EXEC_OUTPUT_MAX_BYTES"
    )
    gateway_url: str = Field(
        default="http://127.0.0.1:4000", validation_alias="FACTORY_GATEWAY_URL"
    )
    gateway_config_path: str | None = Field(default=None, validation_alias="FACTORY_GATEWAY_CONFIG")
    gateway_sandbox_url: str = Field(
        default="http://gateway-edge:4000", validation_alias="FACTORY_GATEWAY_SANDBOX_URL"
    )
    gateway_master_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="LITELLM_MASTER_KEY"
    )

    sandbox_attach: CsvTuple = Field(
        default=("gateway-edge", "pkg-mirror"), validation_alias="FACTORY_SANDBOX_ATTACH"
    )
    registry_env_file: Path | None = Field(
        default=None, validation_alias="FACTORY_REGISTRY_ENV_FILE"
    )
    registry_adc_file: Path | None = Field(
        default=None, validation_alias="FACTORY_REGISTRY_ADC_FILE"
    )
    mirror_out_dir: Path = Field(
        default=Path("/pkg-auth"), validation_alias="FACTORY_MIRROR_OUT_DIR"
    )

    iap_audience: str | None = Field(default=None, validation_alias="FACTORY_IAP_AUDIENCE")
    in_container: bool = Field(default=False, validation_alias="FACTORY_IN_CONTAINER")
    api_url: str = Field(default="http://127.0.0.1:8080", validation_alias="FACTORY_API_URL")
    api_token: SecretStr | None = Field(default=None, validation_alias="FACTORY_API_TOKEN")
    api_token_file: Path = Field(
        default=Path("secrets/api_token"), validation_alias="FACTORY_API_TOKEN_FILE"
    )
    local_user: str = Field(default="local", validation_alias="FACTORY_LOCAL_USER")
    allowed_hosts: CsvTuple = Field(
        default=("127.0.0.1", "localhost"), validation_alias="FACTORY_ALLOWED_HOSTS"
    )
    public_url: str | None = Field(default=None, validation_alias="FACTORY_PUBLIC_URL")
    db_pool_size: int = Field(default=5, ge=1, validation_alias="FACTORY_DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, ge=0, validation_alias="FACTORY_DB_MAX_OVERFLOW")
    checkpointer_pool_max: int = Field(
        default=4, ge=1, validation_alias="FACTORY_CHECKPOINTER_POOL_MAX"
    )
    db_statement_timeout_s: int = Field(
        default=0, ge=0, validation_alias="FACTORY_DB_STATEMENT_TIMEOUT_S"
    )

    notify_webhook_url: SecretStr | None = Field(
        default=None, validation_alias="FACTORY_NOTIFY_WEBHOOK_URL"
    )
    notify_webhook_url_file: Path | None = Field(
        default=None, validation_alias="FACTORY_NOTIFY_WEBHOOK_URL_FILE"
    )
    notify_waiting_after_h: int = Field(
        default=4, validation_alias="FACTORY_NOTIFY_WAITING_AFTER_H"
    )

    if TYPE_CHECKING:

        def __init__(self, **values: Any) -> None: ...  # fields are populated by alias

    @field_validator("allowed_image_prefixes")
    @classmethod
    def _prefixes_end_with_slash(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for p in v:
            if not p.endswith("/"):
                raise ValueError(f"image prefix {p!r} must end with '/'")
        return v

    def _password(self) -> str | None:
        if self.database_password_file is None:
            return None
        return self.database_password_file.read_text().strip()

    @model_validator(mode="after")
    def _webhook_from_file(self) -> Self:
        f = self.notify_webhook_url_file
        if self.notify_webhook_url is None and f is not None and f.is_file():
            value = f.read_text().strip()
            if value:
                self.notify_webhook_url = SecretStr(value)
        return self

    @model_validator(mode="after")
    def _register_secrets(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                register_secret(value.get_secret_value())
                if name == "database_url":
                    pw = make_url(value.get_secret_value()).password
                    if pw:
                        register_secret(pw)
            elif name.endswith("_file") and isinstance(value, Path) and value.is_file():
                register_secret(value.read_text().strip())
        return self

    def sqlalchemy_url(self) -> str:
        url = make_url(self.database_url.get_secret_value())
        pw = self._password()
        if pw:
            url = url.set(password=pw)
        return url.render_as_string(hide_password=False)

    def libpq_dsn(self) -> str:
        return self.sqlalchemy_url().replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
