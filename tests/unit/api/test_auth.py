from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import (
    NOW,
    OTHER_KEY,
    PERSONAL_TOKEN,
    TOKEN,
    FakeKeys,
    fake_deps,
    jwt,
    server,
    settings,
)

from swfactory.api.app import SECURITY_HEADERS, create_app
from swfactory.api.auth import (
    TOKEN_DAYS_DEFAULT,
    AuthError,
    LoginCodes,
    ModeError,
    hash_token,
    load_personal_token,
    new_token,
    origin_allowed,
    session_value,
    token_usable,
    verify_iap_assertion,
)

JOB = {"title": "x"}
PUBLIC = "https://factory.example.com"


def bearer(t):
    return {"authorization": f"Bearer {t}"}


def make_client(s, **kw):
    app = create_app(s, fake_deps(s, **kw), on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    return TestClient(app, base_url="http://127.0.0.1:8080")


@pytest.fixture
def client_personal():
    return make_client(settings())


@pytest.fixture
def client_personal_cookie():
    c = make_client(settings())
    c.cookies.set("sf_session", session_value(PERSONAL_TOKEN))
    return c


@pytest.fixture
def client_personal_no_def():
    return make_client(settings(), active=False)


@pytest.fixture
def client_server():
    return make_client(server())


def iap(email):
    return {"x-goog-iap-jwt-assertion": jwt(email=email)}


def test_personal_mode_needs_token(client_personal):
    assert client_personal.get("/api/me").status_code == 401


def test_personal_bearer_is_local_admin(client_personal):
    assert client_personal.get("/api/me", headers=bearer(PERSONAL_TOKEN)).json() == {
        "email": "local",
        "role": "admin",
        "mode": "personal",
        "via": "token",
    }


def test_personal_wrong_bearer(client_personal):
    assert client_personal.get("/api/me", headers=bearer("nope")).status_code == 401


def test_login_code_sets_strict_cookie_once(client_personal):
    code = client_personal.post("/api/login-code", headers=bearer(PERSONAL_TOKEN)).json()["code"]
    r = client_personal.get(f"/login?code={code}", follow_redirects=False)
    assert r.status_code == 303
    c = r.headers["set-cookie"]
    assert "sf_session=" in c and "HttpOnly" in c and "samesite=strict" in c.lower()
    assert client_personal.get(f"/login?code={code}", follow_redirects=False).status_code == 401


def test_login_code_needs_bearer(client_personal_cookie):
    assert client_personal_cookie.post("/api/login-code").status_code == 401


def test_cookie_authenticates(client_personal_cookie):
    assert client_personal_cookie.get("/api/me").json()["via"] == "cookie"


def test_untrusted_host_400(client_personal):
    assert client_personal.get("/healthz", headers={"host": "evil.example"}).status_code == 400


def test_security_headers_on_every_response(client_personal):
    for r in (
        client_personal.get("/healthz"),
        client_personal.get("/api/me"),
        client_personal.get("/healthz", headers={"host": "evil.example"}),
    ):
        assert {k: r.headers[k] for k in SECURITY_HEADERS} == SECURITY_HEADERS
    assert SECURITY_HEADERS == {
        "Content-Security-Policy": (
            "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    }


def test_cross_origin_mutation_403(client_personal_cookie):
    r = client_personal_cookie.post(
        "/api/jobs", json=JOB, headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "origin not allowed"}


def test_web_root_serves_the_dashboard_with_security_headers(client_personal):
    r = client_personal.get("/")
    assert r.status_code == 200 and "<title>Factory floor</title>" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client_personal.get("/app.js").status_code == 200
    assert client_personal.get("/nope.js").status_code == 404


def srv(**kw):
    return settings(FACTORY_API_URL="http://127.0.0.1:8080", **kw)


@pytest.mark.parametrize(
    "origin, ok",
    [
        (None, True),
        ("http://127.0.0.1:8080", True),
        (PUBLIC, True),
        ("https://factory.example.com:443", True),
        ("HTTPS://Factory.Example.com", True),
        ("http://factory.example.com", False),
        ("https://factory.example.com:8443", False),
        ("http://127.0.0.1", False),
        ("http://localhost:8080", False),
        ("null", False),
        ("https://evil.example", False),
    ],
)
def test_origin_allowed(origin, ok):
    assert origin_allowed(origin, srv(FACTORY_PUBLIC_URL=PUBLIC)) is ok


def test_public_origin_needs_public_url():
    assert origin_allowed(PUBLIC, srv()) is False


def test_server_mode_requires_audience():
    with pytest.raises(RuntimeError):
        create_app(server(FACTORY_IAP_AUDIENCE=""), fake_deps(), on_gce=lambda: False)


def _bad_iap():
    return [
        {},
        {"x-goog-authenticated-user-email": "accounts.google.com:a@x.io"},
        {"x-goog-iap-jwt-assertion": jwt(key=OTHER_KEY)},
        {"x-goog-iap-jwt-assertion": jwt(aud="/projects/1/global/backendServices/999")},
        {"x-goog-iap-jwt-assertion": jwt(iss="https://evil")},
        {"x-goog-iap-jwt-assertion": jwt(alg="HS256")},
        {"authorization": "Bearer sfp_revoked"},
        {"authorization": "Bearer sfp_expired"},
        {"authorization": "Bearer sfp_unknown"},
        {"cookie": "sf_session=forged"},
    ]


@pytest.mark.parametrize("headers", _bad_iap())
def test_server_refuses_forged_or_missing_identity(client_server, headers):
    assert client_server.get("/api/me", headers=headers).status_code == 401


def test_valid_iap_maps_user_role(client_server):
    assert client_server.get("/api/me", headers=iap("sub@x.io")).json()["role"] == "submitter"


def test_unknown_iap_user_forbidden(client_server):
    assert client_server.get("/api/me", headers=iap("nobody@x.io")).status_code == 403


def test_bearer_token_hashed_lookup(client_server):
    assert client_server.get("/api/me", headers=bearer(TOKEN)).json()["via"] == "token"


def test_server_has_no_login(client_server):
    assert client_server.get("/login?code=x", follow_redirects=False).status_code == 404


def test_healthz_no_auth_no_db(client_server):
    assert client_server.get("/healthz").json() == {"ok": True}


def test_readyz_reports_mode(client_personal):
    assert client_personal.get("/readyz").json()["mode"] == "personal"


def test_readyz_503_without_definition(client_personal_no_def):
    assert client_personal_no_def.get("/readyz").status_code == 503


def test_readyz_server_503_without_worker(client_server):
    assert client_server.get("/readyz").status_code == 503


def test_token_usable():
    assert token_usable(revoked_at=None, expires_at=None, now=NOW) is True
    assert token_usable(revoked_at=None, expires_at=NOW + timedelta(days=1), now=NOW) is True
    assert token_usable(revoked_at=None, expires_at=NOW, now=NOW) is False
    assert token_usable(revoked_at=NOW - timedelta(days=1), expires_at=None, now=NOW) is False
    assert TOKEN_DAYS_DEFAULT == 90


def test_token_helpers():
    t = new_token()
    assert t.startswith("sfp_") and len(hash_token(t)) == 64
    assert session_value("a") != session_value("b")


def test_login_codes_expire():
    now = [0.0]
    codes = LoginCodes(clock=lambda: now[0])
    c = codes.issue(ttl_s=10)
    now[0] = 11
    assert codes.redeem(c) is False
    assert codes.redeem("never-issued") is False


def test_load_personal_token_from_file(tmp_path):
    f = tmp_path / "t"
    f.write_text("sfp_file\n")
    s = settings(FACTORY_API_TOKEN=None, FACTORY_API_TOKEN_FILE=str(f))
    assert load_personal_token(s) == "sfp_file"
    with pytest.raises(ModeError):
        load_personal_token(
            settings(FACTORY_API_TOKEN=None, FACTORY_API_TOKEN_FILE=str(tmp_path / "x"))
        )


def test_verify_iap_rejects_garbage():
    with pytest.raises(AuthError):
        verify_iap_assertion("garbage", audience="a", certs={})
