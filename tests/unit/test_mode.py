import pytest
from tests.unit.api.api_support import settings

from swfactory.api.auth import ModeError, check_bind_host, resolve_mode
from swfactory.domain.types import FactoryMode
from swfactory.settings import Settings


def personal(**kw):
    return settings(**{f"FACTORY_{k.upper()}": v for k, v in kw.items()})


def test_mode_required(monkeypatch):
    monkeypatch.delenv("FACTORY_MODE", raising=False)
    # _env_file=None: the developer's .env may set FACTORY_MODE
    with pytest.raises(ModeError):
        resolve_mode(Settings(_env_file=None), on_gce=lambda: False)  # pyright: ignore[reportCallIssue]


def test_personal_refused_with_iap_audience():
    with pytest.raises(ModeError):
        resolve_mode(
            personal(iap_audience="/projects/1/global/backendServices/2"), on_gce=lambda: False
        )


def test_personal_refused_on_gce():
    with pytest.raises(ModeError):
        resolve_mode(personal(), on_gce=lambda: True)


def test_personal_resolves():
    assert resolve_mode(personal(), on_gce=lambda: False) == FactoryMode.PERSONAL


def test_personal_refuses_public_bind():
    with pytest.raises(ValueError):
        check_bind_host(personal(in_container=False), "0.0.0.0")


def test_loopback_bind_allowed():
    assert check_bind_host(personal(), "127.0.0.1") is None


def test_container_bind_allowed():
    assert check_bind_host(personal(in_container=True), "0.0.0.0") is None
