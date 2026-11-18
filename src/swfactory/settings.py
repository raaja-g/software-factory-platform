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


CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(split_csv)]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

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
    def _register_secrets(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                register_secret(value.get_secret_value())
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
