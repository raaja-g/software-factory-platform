"""The dashboard can be served by its own container instead of the API.

By default (and for `factory serve` in development) the API serves the dashboard's static files.
With FACTORY_SERVE_WEB=false it serves only the API, and the separate UI container forwards
/api and /login to it; the browser then talks to the UI's origin, which the API accepts through
FACTORY_PUBLIC_URL."""

from tests.unit.api.api_support import PERSONAL_TOKEN, settings
from tests.unit.api.test_auth import bearer, make_client

UI = "http://127.0.0.1:7300"


def test_the_api_serves_the_dashboard_by_default():
    c = make_client(settings())
    r = c.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_without_serve_web_the_api_serves_no_dashboard_but_still_the_api():
    c = make_client(settings(FACTORY_SERVE_WEB="false"))
    assert c.get("/").status_code == 404
    assert c.get("/app.js").status_code == 404
    assert c.get("/index.html").status_code == 404
    me = c.get("/api/me", headers=bearer(PERSONAL_TOKEN))
    assert me.status_code == 200 and me.json()["via"] == "token"
    assert c.get("/readyz").status_code in (200, 503)  # the health route is still there


def test_the_ui_origin_is_accepted_for_a_mutating_request_when_it_is_the_public_url():
    c = make_client(settings(FACTORY_SERVE_WEB="false", FACTORY_PUBLIC_URL=UI))
    r = c.post("/api/login-code", headers={**bearer(PERSONAL_TOKEN), "origin": UI})
    assert r.status_code == 200 and r.json()["code"]


def test_an_unrelated_origin_is_still_refused():
    c = make_client(settings(FACTORY_SERVE_WEB="false", FACTORY_PUBLIC_URL=UI))
    r = c.post("/api/login-code", headers={**bearer(PERSONAL_TOKEN), "origin": "http://evil.test"})
    assert r.status_code == 403


def test_login_through_the_ui_sets_the_session_cookie_for_that_origin():
    c = make_client(settings(FACTORY_SERVE_WEB="false", FACTORY_PUBLIC_URL=UI))
    code = c.post("/api/login-code", headers=bearer(PERSONAL_TOKEN)).json()["code"]
    r = c.get(f"/login?code={code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "factory_session" in r.headers["set-cookie"] or "session" in r.headers["set-cookie"]


def test_the_session_cookie_lasts_30_days_by_default_and_stays_locked_down():
    # a cookie with no lifetime ends with the browser, so you needed a new login link every time
    c = make_client(settings(FACTORY_SERVE_WEB="false", FACTORY_PUBLIC_URL=UI))
    code = c.post("/api/login-code", headers=bearer(PERSONAL_TOKEN)).json()["code"]
    cookie = c.get(f"/login?code={code}", follow_redirects=False).headers["set-cookie"].lower()
    assert f"max-age={30 * 86400}" in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie


def test_the_session_lifetime_is_a_setting():
    c = make_client(settings(FACTORY_SESSION_DAYS="7"))
    code = c.post("/api/login-code", headers=bearer(PERSONAL_TOKEN)).json()["code"]
    cookie = c.get(f"/login?code={code}", follow_redirects=False).headers["set-cookie"].lower()
    assert f"max-age={7 * 86400}" in cookie
