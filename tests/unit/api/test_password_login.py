"""Sign in with a password on a login page instead of a one-time link (personal mode).

The password is FACTORY_LOGIN_PASSWORD; when it is not set the personal API token is the password,
so an existing setup keeps working. A wrong password never sets a cookie, repeated wrong passwords
lock the form for a minute, and a sign-out clears the cookie."""

import pytest
from tests.unit.api.api_support import PERSONAL_TOKEN, server, settings
from tests.unit.api.test_auth import bearer, make_client

import swfactory.api.app as app_mod

UI = "http://127.0.0.1:7300"
PW = "correct horse battery"
USER = "Raja"


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    async def instant(_s):
        return None

    monkeypatch.setattr(app_mod.asyncio, "sleep", instant)


def client(**kw):
    kw.setdefault("FACTORY_LOGIN_USER", USER)
    return make_client(settings(FACTORY_PUBLIC_URL=UI, FACTORY_SERVE_WEB="false", **kw))


def login(c, password, origin=UI, username=USER):
    body = {"username": username, "password": password}
    return c.post("/api/login", json=body, headers={"origin": origin})


def test_the_right_password_signs_you_in_and_the_cookie_works():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    r = login(c, PW)
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookie = r.headers["set-cookie"].lower()
    assert (
        "httponly" in cookie and "samesite=strict" in cookie and f"max-age={30 * 86400}" in cookie
    )
    me = c.get("/api/me")  # the client keeps the cookie
    assert me.status_code == 200 and me.json()["via"] == "cookie"


def test_without_a_login_password_the_api_token_is_the_password():
    c = client()
    assert login(c, PERSONAL_TOKEN).status_code == 200


def test_the_api_token_no_longer_works_as_the_password_once_one_is_set():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    assert login(c, PERSONAL_TOKEN).status_code == 401


def test_a_wrong_password_is_refused_and_sets_no_cookie():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    r = login(c, "nope")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.get("/api/me").status_code == 401
    assert login(c, "").status_code == 401


def test_five_wrong_passwords_lock_the_form_even_for_the_right_one():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    for _ in range(5):
        assert login(c, "wrong").status_code == 401
    locked = login(c, PW)
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0


def test_a_right_password_resets_the_count():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    for _ in range(4):
        login(c, "wrong")
    assert login(c, PW).status_code == 200
    for _ in range(4):
        assert login(c, "wrong").status_code == 401  # the earlier four no longer count


def test_a_login_from_another_origin_is_refused():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    assert login(c, PW, origin="http://evil.test").status_code == 403


def test_sign_out_clears_the_cookie():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    login(c, PW)
    r = c.post("/api/logout", headers={"origin": UI})
    assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()
    assert c.get("/api/me").status_code == 401


def test_a_short_login_password_is_rejected_at_startup():
    with pytest.raises(ValueError, match="at least 8"):
        settings(FACTORY_LOGIN_PASSWORD="short")


def test_the_password_form_does_not_exist_in_server_mode():
    s = server()
    c = make_client(s)
    body = {"username": "x", "password": "x"}
    r = c.post("/api/login", json=body, headers={"origin": "http://127.0.0.1:7080"})
    assert r.status_code == 404


def test_the_token_still_authenticates_api_calls_as_before():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    assert c.get("/api/me", headers=bearer(PERSONAL_TOKEN)).json()["via"] == "token"


def test_the_username_must_match_too_and_a_wrong_one_looks_like_a_wrong_password():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    wrong_user = login(c, PW, username="someone-else")
    wrong_pw = login(c, "nope", username=USER)
    assert wrong_user.status_code == wrong_pw.status_code == 401
    assert wrong_user.json() == wrong_pw.json()  # never reveals which of the two was wrong
    assert "set-cookie" not in wrong_user.headers


def test_the_username_is_not_case_sensitive():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    assert login(c, PW, username="rAJA").status_code == 200


def test_a_blank_username_is_refused():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    assert login(c, PW, username="").status_code == 401


def test_without_a_login_user_the_local_user_name_is_the_username():
    c = make_client(settings(FACTORY_PUBLIC_URL=UI, FACTORY_SERVE_WEB="false"))
    body = {"username": "local", "password": PERSONAL_TOKEN}
    assert c.post("/api/login", json=body, headers={"origin": UI}).status_code == 200


def test_wrong_usernames_count_toward_the_lockout():
    c = client(FACTORY_LOGIN_PASSWORD=PW)
    for _ in range(5):
        assert login(c, PW, username="guess").status_code == 401
    assert login(c, PW).status_code == 429
