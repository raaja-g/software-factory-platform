import re
from pathlib import Path

from swfactory.settings import EDGE_ALLOWED_PATHS, Settings

ROOT = Path(__file__).resolve().parents[3]


def test_edge_conf_body_limit_and_resolver():
    conf = (ROOT / "docker/gateway-edge.conf").read_text()
    targets = re.findall(r"proxy_pass\s+(\S+);", conf)
    for line in (
        "client_max_body_size 64m;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
        "proxy_http_version 1.1;",
        "resolver 127.0.0.11 valid=10s",
    ):
        assert line in conf
    assert targets and set(targets) == {"$gateway"}
    assert "set $gateway http://gateway:4000;" in conf


def test_edge_locations_match_allowlist():
    conf = (ROOT / "docker/gateway-edge.conf").read_text()
    assert sorted(re.findall(r"location ~ (\S+) \{", conf)) == sorted(EDGE_ALLOWED_PATHS)


def test_allowlist_regexes_accept_and_refuse():
    ok = [
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/models",
        "/v1/messages/count_tokens",
        "/v1beta/models/m:generateContent",
    ]
    bad = [
        "/key/generate",
        "/v1/models/x",
        "/anthropic/v1/messages",
        "/health",
        "/vertex_ai/v1/projects/p:generateContent",
    ]
    assert all(any(re.search(p, x) for p in EDGE_ALLOWED_PATHS) for x in ok)
    assert not any(re.search(p, x) for p in EDGE_ALLOWED_PATHS for x in bad)


def test_gateway_settings_defaults(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test-master")
    s = Settings(_env_file=None, FACTORY_DATABASE_URL="postgresql+psycopg://u@h/d")
    assert s.gateway_url == "http://127.0.0.1:4000"
    assert s.gateway_sandbox_url == "http://gateway-edge:4000"
    assert s.gateway_master_key.get_secret_value() == "sk-test-master"
