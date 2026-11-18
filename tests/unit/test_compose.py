import re
from pathlib import Path

import yaml


def test_compose_postgres():
    doc = yaml.safe_load(Path("compose.yaml").read_text())
    svc = doc["services"]["postgres"]
    assert re.fullmatch(r"postgres:18\.6@sha256:[0-9a-f]{64}", svc["image"])
    assert "pgdata:/var/lib/postgresql" in svc["volumes"]
    assert not any(v.endswith("/var/lib/postgresql/data") for v in svc["volumes"])
    assert "pgdata" in doc["volumes"]
    assert doc["secrets"]["pg_password"]["file"] == "./secrets/pg_password"
    assert "pg_isready" in " ".join(svc["healthcheck"]["test"])
    assert "POSTGRES_PASSWORD" not in svc["environment"]
    assert svc["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/pg_password"
    assert svc["secrets"] == ["pg_password"]
    assert all(p.startswith("127.0.0.1:") for p in svc.get("ports", []))


def test_compose_runner_build_services():
    doc = yaml.safe_load(Path("compose.yaml").read_text())
    assert {"postgres", "runner", "runner-web"} <= set(doc["services"])
    for name in ("runner", "runner-web"):
        assert doc["services"][name]["profiles"] == ["build"]
