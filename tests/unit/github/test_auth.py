import json

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from swfactory.domain.types import FactoryMode
from swfactory.github.auth import (
    PERMS_DELIVER,
    PERMS_PUSH,
    AppAuth,
    PatAuth,
    app_jwt,
    make_auth,
)
from swfactory.logging import redact
from swfactory.settings import Settings


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://x@h/d", **kw)  # type: ignore[call-arg]


def test_app_jwt_claims(rsa_pem: str) -> None:
    claims = jwt.decode(
        app_jwt("Iv23abc", rsa_pem, now=1_000_000),
        load_pem_private_key(rsa_pem.encode(), None).public_key(),  # type: ignore[arg-type]
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23abc"}


def _mock(router: respx.MockRouter, token: str, org: bool = False) -> respx.Route:
    prefix = "/orgs/acme" if org else "/repos/acme/web"
    router.get(f"https://api.github.com{prefix}/installation").respond(json={"id": 7})
    return router.post("https://api.github.com/app/installations/7/access_tokens").respond(
        201, json={"token": token}
    )


async def test_token_narrowed_to_one_repo(rsa_pem: str, clock: Clock) -> None:
    with respx.mock(assert_all_called=True) as router:
        mint = _mock(router, "ghs_abc")
        auth = AppAuth("Iv1", rsa_pem, httpx.AsyncClient(), clock=clock)
        assert await auth.token_for("acme/web", PERMS_PUSH) == "ghs_abc"
        assert json.loads(mint.calls[0].request.content) == {
            "repositories": ["web"],
            "permissions": PERMS_PUSH,
        }
        assert mint.calls[0].request.headers["authorization"].startswith("Bearer ")


async def test_org_token_has_no_repositories(rsa_pem: str, clock: Clock) -> None:
    with respx.mock() as router:
        mint = _mock(router, "ghs_org", org=True)
        auth = AppAuth("Iv1", rsa_pem, httpx.AsyncClient(), clock=clock)
        await auth.org_token("acme", {"packages": "read"})
        assert json.loads(mint.calls[0].request.content) == {"permissions": {"packages": "read"}}


def test_push_token_cannot_write_checks_or_prs() -> None:
    assert set(PERMS_PUSH) == {"contents", "metadata"}


async def test_token_cached_55_minutes_and_length_not_assumed(rsa_pem: str, clock: Clock) -> None:
    long = "ghs_" + "x" * 300
    with respx.mock() as router:
        mint = _mock(router, long)
        auth = AppAuth("Iv1", rsa_pem, httpx.AsyncClient(), clock=clock)
        assert await auth.token_for("acme/web", PERMS_DELIVER) == long
        clock.advance(54 * 60)
        await auth.token_for("acme/web", PERMS_DELIVER)
        assert mint.call_count == 1
        clock.advance(2 * 60)
        await auth.token_for("acme/web", PERMS_DELIVER)
        assert mint.call_count == 2
        assert long not in redact(f"token {long}")


async def test_expires_at_shortens_cache(rsa_pem: str, clock: Clock) -> None:
    with respx.mock() as router:
        router.get("https://api.github.com/repos/acme/web/installation").respond(json={"id": 7})
        mint = router.post("https://api.github.com/app/installations/7/access_tokens").respond(
            201, json={"token": "ghs_short", "expires_at": "1970-01-12T14:00:00Z"}
        )
        # clock is 1970-01-12T13:46:40Z; expiry is 13m20s away, minus the 5 min margin
        auth = AppAuth("Iv1", rsa_pem, httpx.AsyncClient(), clock=clock)
        await auth.token_for("acme/web", PERMS_PUSH)
        clock.advance(9 * 60)
        await auth.token_for("acme/web", PERMS_PUSH)
        assert mint.call_count == 2


async def test_pat_ignores_permissions() -> None:
    auth = PatAuth("github_pat_" + "a" * 30)
    assert await auth.token_for("a/b", PERMS_PUSH) == await auth.org_token("a", {})


def test_pat_refused_in_server_mode() -> None:
    with pytest.raises(ValueError, match="FACTORY_MODE=personal"):
        make_auth(
            _settings(
                mode=FactoryMode.SERVER, github_token="github_pat_x", github_app_client_id=None
            )
        )


def test_make_auth_picks_pat_in_personal_and_app_with_client_id(
    rsa_pem: str, tmp_path: object
) -> None:
    assert isinstance(make_auth(_settings(github_token="github_pat_x")), PatAuth)
    from pathlib import Path

    key = Path(str(tmp_path)) / "k.pem"
    key.write_text(rsa_pem)
    s = _settings(
        mode=FactoryMode.SERVER, github_app_client_id="Iv1", github_app_private_key_file=key
    )
    assert isinstance(make_auth(s), AppAuth)
    with pytest.raises(ValueError, match="no GitHub credentials"):
        make_auth(_settings())
