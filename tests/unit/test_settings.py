import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from swfactory.domain.types import FactoryMode
from swfactory.settings import Settings, get_settings, split_csv


def test_mode_from_env(monkeypatch):
    monkeypatch.setenv("FACTORY_MODE", "server")
    assert Settings().mode is FactoryMode.SERVER
    assert "mode" in Settings().model_fields_set
    monkeypatch.setenv("FACTORY_MODE", "cloud")
    with pytest.raises(ValidationError) as ei:
        Settings()
    assert ei.value.errors()[0]["loc"] == ("FACTORY_MODE",)


def test_mode_default_is_not_explicit(monkeypatch):
    monkeypatch.delenv("FACTORY_MODE", raising=False)
    s = Settings(_env_file=None)
    assert s.mode is FactoryMode.PERSONAL and "mode" not in s.model_fields_set


def test_password_file_merged(tmp_path, monkeypatch):
    (tmp_path / "pw").write_text("s3cret\n")
    monkeypatch.setenv(
        "FACTORY_DATABASE_URL", "postgresql+psycopg://factory@127.0.0.1:5432/factory"
    )
    monkeypatch.setenv("FACTORY_DATABASE_PASSWORD_FILE", str(tmp_path / "pw"))
    s = Settings(_env_file=None)
    assert s.sqlalchemy_url() == "postgresql+psycopg://factory:s3cret@127.0.0.1:5432/factory"
    assert s.libpq_dsn() == "postgresql://factory:s3cret@127.0.0.1:5432/factory"
    assert "s3cret" not in repr(s)


def test_env_example_lists_every_setting():
    keys = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert {str(f.validation_alias) for f in Settings.model_fields.values()} <= keys
    assert {
        "FACTORY_MODE",
        "FACTORY_DATABASE_URL",
        "FACTORY_ALLOWED_PROVIDER_HOSTS",
        "FACTORY_ALLOWED_IMAGE_PREFIXES",
    } <= keys


def test_empty_values_use_defaults(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "FACTORY_ALLOWED_IMAGE_PREFIXES=\nFACTORY_ALLOWED_PROVIDER_HOSTS=\nFACTORY_WORKER_ID=\n"
    )
    monkeypatch.setenv("FACTORY_LOG_LEVEL", "")
    s = Settings(_env_file=tmp_path / ".env")
    assert s.allowed_image_prefixes == ("swfactory/",)
    assert s.allowed_provider_hosts == (
        "generativelanguage.googleapis.com",
        "api.groq.com",
        "openrouter.ai",
    )
    assert s.worker_id.startswith(socket.gethostname() + "-") and s.log_level == "INFO"


def test_comma_lists_drop_blank_items(monkeypatch):
    monkeypatch.setenv("FACTORY_ALLOWED_PROVIDER_HOSTS", "a.example, ,b.example,")
    monkeypatch.setenv("FACTORY_ALLOWED_IMAGE_PREFIXES", " swfactory/ ,")
    s = Settings(_env_file=None)
    assert s.allowed_provider_hosts == ("a.example", "b.example")
    assert s.allowed_image_prefixes == ("swfactory/",)


def test_split_csv():
    assert split_csv("a, ,b") == ("a", "b")
    assert split_csv(["a ", "", "b"]) == ("a", "b")


def test_host_folder_settings(monkeypatch):
    assert Settings(_env_file=None).host_repos_root is None
    assert Settings(_env_file=None).host_home is None
    monkeypatch.setenv("FACTORY_HOST_REPOS_ROOT", "/Users/me/work")
    monkeypatch.setenv("FACTORY_HOST_HOME", "/Users/me")
    s = Settings(_env_file=None)
    assert (s.host_repos_root, s.host_home) == ("/Users/me/work", "/Users/me")


def test_image_prefix_must_end_with_slash(monkeypatch):
    monkeypatch.setenv("FACTORY_ALLOWED_IMAGE_PREFIXES", "docker.io")
    with pytest.raises(ValidationError, match="must end with '/'"):
        Settings(_env_file=None)


def test_get_settings_cached():
    assert get_settings() is get_settings()


def test_settings_register_secrets(tmp_path, monkeypatch):
    from swfactory.logging import redact

    (tmp_path / "pw").write_text("filepw-9876543\n")
    monkeypatch.setenv("FACTORY_DATABASE_PASSWORD_FILE", str(tmp_path / "pw"))
    Settings(_env_file=None)
    assert "filepw-9876543" not in redact("x filepw-9876543 y")


def test_sandbox_attach_default():
    assert Settings(mode=FactoryMode.PERSONAL).sandbox_attach == ("gateway-edge", "pkg-mirror")
