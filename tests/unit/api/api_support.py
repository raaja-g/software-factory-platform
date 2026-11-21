import base64
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from swfactory.api.app import ApiDeps
from swfactory.api.auth import IAP_ISSUER, hash_token
from swfactory.api.sessions import InMemoryWebSessionStore
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import FactoryMode
from swfactory.settings import Settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PERSONAL_TOKEN = "sfp_personal_test_token"
AUDIENCE = "/projects/1/global/backendServices/2"
TOKEN = "sfp_good"


def settings(**kw):
    base = {
        "FACTORY_MODE": "personal",
        "FACTORY_DATABASE_URL": "postgresql+psycopg://factory@127.0.0.1:7432/x",
        "FACTORY_API_TOKEN": PERSONAL_TOKEN,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


def server(**kw):
    kw.setdefault("FACTORY_IAP_AUDIENCE", AUDIENCE)
    kw.setdefault("FACTORY_MODE", "server")
    kw.setdefault("FACTORY_API_TOKEN", None)
    kw = {k: v for k, v in kw.items() if v is not None}
    return settings(**kw)


class FakeUsers:
    def __init__(self):
        self.roles = {"sub@x.io": "submitter", "tok@x.io": "approver"}
        self.tokens = {
            hash_token(TOKEN): ("tok@x.io", None, None),
            hash_token("sfp_revoked"): ("tok@x.io", NOW - timedelta(days=1), None),
            hash_token("sfp_expired"): ("tok@x.io", None, datetime.now(UTC) - timedelta(days=1)),
        }

    async def role_of(self, email):
        return self.roles.get(email)

    async def token_email(self, token_hash):
        from swfactory.api.auth import token_usable

        row = self.tokens.get(token_hash)
        if row is None:
            return None
        email, revoked, expires = row
        ok = token_usable(revoked_at=revoked, expires_at=expires, now=datetime.now(UTC))
        return email if ok else None


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return None


class FakeDefinitions:
    def __init__(self, active=True):
        self._active = active

    async def active(self):
        if not self._active:
            raise NoActiveDefinition("none")
        return "sha", SimpleNamespace(factory=SimpleNamespace(apps={}))


def fake_deps(s=None, *, active=True, web_sessions=None):
    return ApiDeps(
        sessions=lambda: _Session(),  # pyright: ignore[reportArgumentType]
        events=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        artifacts=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=FakeDefinitions(active),  # pyright: ignore[reportArgumentType]
        users=FakeUsers(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=s or settings(),
        web_sessions=web_sessions if web_sessions is not None else InMemoryWebSessionStore(),
    )


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def pem(key) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def jwt(*, key=KEY, aud=AUDIENCE, iss=IAP_ISSUER, alg="ES256", email="sub@x.io"):
    header = {"alg": alg, "typ": "JWT", "kid": "kid1"}
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "email": email, "iat": now, "exp": now + 600}
    signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing}.{_b64(sig)}"


class FakeKeys:
    async def certs(self):
        return {"kid1": pem(KEY)}


MODE = FactoryMode
