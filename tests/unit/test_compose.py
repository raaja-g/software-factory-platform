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


# ---- M7.7: complete local stack -------------------------------------------------------------
import pytest  # noqa: E402

from swfactory.db.engine import PG_MAX_CONNECTIONS, connection_budget, make_engine  # noqa: E402
from swfactory.domain.types import FactoryMode  # noqa: E402
from swfactory.settings import Settings  # noqa: E402

README = Path("README.md")


def compose() -> dict:
    return yaml.safe_load(Path("compose.yaml").read_text())


def test_override() -> dict:  # not a test: helper named in the plan
    return yaml.safe_load(Path("compose.test.yaml").read_text())


test_override.__test__ = False  # type: ignore[attr-defined]


def svcs() -> list[dict]:
    return list(compose()["services"].values())


def svcs_items() -> list[tuple[str, dict]]:
    return list(compose()["services"].items())


def svc(name: str) -> dict:
    return compose()["services"][name]


def volumes() -> dict:
    return compose()["volumes"]


# The build-profile services (runner images) are not part of the running stack.
def _stack() -> dict[str, dict]:
    return {n: s for n, s in svcs_items() if s.get("profiles") != ["build"]}


DEFAULT_SERVICES = set(_stack())
ONE_SHOTS = {"migrate", "gateway-db", "gateway-migrate", "gateway-init", "egress-init"}


def test_default_services():
    gw_db = DEFAULT_SERVICES & {"gateway-db", "litellm-postgres"}
    assert len(gw_db) == 1
    assert DEFAULT_SERVICES - gw_db == {
        "postgres",
        "migrate",
        "gateway-init",
        "gateway",
        "gateway-edge",
        "gateway-migrate",
        "docker-proxy",
        "api",
        "orchestrator",
        "pkg-mirror",
        "pkg-mirror-pypi",
        "pkg-mirror-auth",
    }
    assert "router" not in compose()["services"]


def test_no_shared_sandbox_network():
    assert "sandbox-net" not in compose()["networks"]
    assert (svc("gateway")["networks"], svc("gateway-edge")["networks"]) == (
        ["core-net"],
        ["core-net"],
    )


def test_ports_loopback_only():
    assert sorted(p for s in svcs() for p in s.get("ports", [])) == [
        "127.0.0.1:4000:4000",
        "127.0.0.1:8080:8080",
    ]


def test_postgres_not_published_by_default():
    assert "ports" not in svc("postgres")
    assert test_override()["services"]["postgres"]["ports"] == ["127.0.0.1:5432:5432"]


def test_postgres_pg18_layout():
    pg = svc("postgres")
    assert pg["image"].startswith("postgres:18.6@sha256:")
    assert "pgdata:/var/lib/postgresql" in pg["volumes"]
    assert "pg_isready -U factory -d factory" in " ".join(pg["healthcheck"]["test"])
    assert "POSTGRES_PASSWORD_FILE" in pg["environment"]


def test_artifacts_volume_shared():
    assert set(volumes()) == {
        "pgdata",
        "repos",
        "artifacts",
        "gateway-config",
        "pkg-cache-npm",
        "pkg-cache-pypi",
        "pkg-auth",
    }
    assert all(
        "artifacts:/var/lib/factory/artifacts" in svc(n)["volumes"] for n in ("api", "orchestrator")
    )
    assert [svc(n)["environment"]["FACTORY_ARTIFACTS_DIR"] for n in ("api", "orchestrator")] == [
        "/var/lib/factory/artifacts"
    ] * 2


def test_pinned_digests():
    assert [
        n
        for n, s in svcs_items()
        if "image" in s
        and not s["image"].startswith(("swfactory-platform", "swfactory/"))
        and "@sha256:" not in s["image"]
    ] == []


def test_orchestrator_stops_gracefully():
    assert svc("orchestrator")["stop_grace_period"] == "60s"


def test_docker_socket_only_in_proxy():
    assert [
        n for n, s in svcs_items() if any("docker.sock" in v for v in s.get("volumes", []))
    ] == ["docker-proxy"]


def test_only_orchestrator_shares_a_network_with_the_proxy():
    proxy_nets = set(svc("docker-proxy")["networks"])
    assert proxy_nets == {"docker-net"} and compose()["networks"]["docker-net"]["internal"] is True
    assert sorted(
        n
        for n, s in svcs_items()
        if n != "docker-proxy" and proxy_nets & set(s.get("networks", []))
    ) == ["orchestrator"]
    assert "docker-net" in svc("orchestrator")["networks"]
    assert "core-net" in svc("orchestrator")["networks"]


def test_only_orchestrator_mounts_the_host_repos():
    mounts = {
        n: [v for v in s.get("volumes", []) if str(v).endswith(":/host-repos")]
        for n, s in svcs_items()
    }
    assert {n for n, m in mounts.items() if m} == {"orchestrator"}
    assert mounts["orchestrator"][0].startswith("${FACTORY_HOST_REPOS_ROOT")
    env = svc("orchestrator")["environment"]
    assert env["FACTORY_HOST_REPOS_ROOT"] == "${FACTORY_HOST_REPOS_ROOT}"
    assert "FACTORY_HOST_HOME" in svc("api")["environment"]


def test_orchestrator_definition_is_writable():
    defs = [v for v in svc("orchestrator")["volumes"] if str(v).split(":")[1:2] == ["/definition"]]
    assert len(defs) == 1 and not str(defs[0]).endswith(":ro")


def test_restart_policies():
    assert {n: s.get("restart") for n, s in _stack().items()} == {
        n: ("no" if n in ONE_SHOTS else "unless-stopped") for n in _stack()
    }


def test_mode_explicit_on_platform_services():
    assert {
        s["environment"]["FACTORY_MODE"]
        for n, s in svcs_items()
        if s.get("image", "").startswith("swfactory-platform")
    } == {"personal"}


def test_api_core_net_only_personal():
    api = svc("api")
    assert api["networks"] == ["core-net"]
    assert api["environment"]["FACTORY_IN_CONTAINER"] == "1"
    assert "api_token" in api["secrets"]


def test_api_waits_for_migrate():
    assert svc("api")["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_api_never_gets_the_gateway_master_key():
    api = svc("api")
    assert not {"LITELLM_MASTER_KEY", "FACTORY_GATEWAY_URL"} & set(api.get("environment", {}))
    assert "env_file" not in api


def test_connection_budget_fits():
    pg = " ".join(svc("postgres")["command"])
    assert "max_connections=100" in pg and "idle_in_transaction_session_timeout=300s" in pg
    assert PG_MAX_CONNECTIONS == 100
    assert connection_budget(Settings(mode=FactoryMode.PERSONAL)) == 47
    assert 47 + 20 <= PG_MAX_CONNECTIONS
    assert svc("api")["environment"]["FACTORY_DB_STATEMENT_TIMEOUT_S"] == "30"


def test_engine_pool_keywords():
    e = make_engine("postgresql+psycopg://u@h/db", pool_size=3, max_overflow=2)
    assert (e.pool.size(), e.pool._max_overflow, e.pool._pre_ping) == (3, 2, True)  # type: ignore[attr-defined]


def test_pool_settings_defaults():
    s = Settings(mode=FactoryMode.PERSONAL)
    assert (s.db_pool_size, s.db_max_overflow, s.checkpointer_pool_max) == (5, 5, 4)
    assert s.db_statement_timeout_s == 0


def test_readme_quickstart():
    t = README.read_text()
    needed = [
        "make up",
        "make down",
        "http://127.0.0.1:8080",
        "gcloud auth application-default login",
        "factory login-url",
        "FACTORY_HOST_REPOS_ROOT",
        "factory run --repo",
    ]
    assert [s for s in needed if s not in t] == []


def test_makefile_targets():
    t = Path("Makefile").read_text()
    for target in ("up:", "down:", "image:", "login:", "test-db:"):
        assert target in t
    assert "linux/amd64,linux/arm64" in t


@pytest.mark.parametrize("name", ["api", "orchestrator"])
def test_platform_services_use_local_image(name):
    assert svc(name)["image"] == "swfactory-platform:dev"


def test_gateway_config_rendered_into_volume():
    g, i, o = svc("gateway"), svc("gateway-init"), svc("orchestrator")
    assert "gateway-config:/gateway-config:ro" in g["volumes"]
    assert g["command"][:2] == ["--config", "/gateway-config/litellm.yaml"]
    assert not any("litellm.yaml" in v for v in g["volumes"])
    assert g["depends_on"]["gateway-init"]["condition"] == "service_completed_successfully"
    assert i["command"] == [
        "gateway",
        "render",
        "--active",
        "--out",
        "/gateway-config/litellm.yaml",
    ]
    assert i["restart"] == "no"
    assert i["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert i["image"] == o["image"]
    m = svc("migrate")
    assert i.get("secrets") == m.get("secrets")
    assert set(m.get("environment", {}).items()) <= set(i["environment"].items())
    assert "gateway-config:/gateway-config" in o["volumes"]
    assert o["environment"]["FACTORY_GATEWAY_CONFIG"] == "/gateway-config/litellm.yaml"


# ---- M7.7c: package mirror -------------------------------------------------------------------
def test_pkg_mirror_services():
    m, p, a = svc("pkg-mirror"), svc("pkg-mirror-pypi"), svc("pkg-mirror-auth")
    assert m["networks"] == ["mirror-net"] and "ports" not in m
    assert p["network_mode"] == "service:pkg-mirror" and "ports" not in p
    assert "@sha256:" in m["image"] and "@sha256:" in p["image"]
    assert m["read_only"] is True and m["cap_drop"] == ["ALL"]
    assert "pkg-auth:/pkg-auth:ro" in m["volumes"] and "pkg-auth:/pkg-auth:ro" in p["volumes"]
    assert "pkg-auth:/pkg-auth" in a["volumes"]
    assert a["command"][:2] == ["mirror", "run"]
    assert set(a["networks"]) <= {"mirror-net", "core-net"}  # M8.10b adds core-net for the broker
    assert m["depends_on"]["pkg-mirror-auth"]["condition"] == "service_healthy"


def test_registry_credentials_only_in_the_sidecar():
    assert [n for n, s in svcs_items() if "registry_credentials" in s.get("secrets", [])] == [
        "pkg-mirror-auth"
    ]
    assert [
        n
        for n, s in svcs_items()
        if any(str(v).startswith("./.env:") for v in s.get("volumes", []))
    ] == ["pkg-mirror-auth"]
    for n in ("pkg-mirror", "pkg-mirror-pypi", "orchestrator", "api", "gateway-edge"):
        assert "registry_credentials" not in svc(n).get("secrets", [])


def test_mirror_net_is_a_plain_bridge_for_the_mirror_only():
    assert sorted(n for n, s in svcs_items() if "mirror-net" in s.get("networks", [])) == [
        "pkg-mirror",
        "pkg-mirror-auth",
    ]
    assert compose()["networks"]["mirror-net"].get("internal") in (None, False)


def test_mirror_entrypoints_exist_and_are_valid_shell():
    import subprocess

    for f in ("npm-entrypoint.sh", "pypi-entrypoint.sh"):
        assert subprocess.run(["sh", "-n", f"deploy/pkg-mirror/{f}"]).returncode == 0


@pytest.mark.parametrize("name", ["api", "orchestrator"])
def test_notification_webhook_reaches_the_services_that_send_notices(name):
    assert "FACTORY_NOTIFY_WEBHOOK_URL" in svc(name)["environment"]


def test_compose_gitops_net_has_no_services():
    doc = yaml.safe_load(Path("compose.yaml").read_text())
    net = doc["networks"]["gitops-net"]
    assert net["name"] == "gitops-net" and not net.get("internal", False)
    joined = [n for n, s in doc["services"].items() if "gitops-net" in (s.get("networks") or [])]
    assert joined == []


def _secret_names(s: dict) -> set[str]:
    return {x if isinstance(x, str) else x.get("source") for x in s.get("secrets", [])}


def test_app_key_only_in_orchestrator():
    c = compose()
    holders = {n for n, s in c["services"].items() if "github_app_key" in _secret_names(s)}
    assert holders == {"orchestrator"}
    assert "8091" not in str(c["services"]["orchestrator"].get("ports", []))
    assert not any("8091" in str(s.get("ports", [])) for s in c["services"].values())


def test_broker_token_in_orchestrator_and_api_only():
    c = compose()
    holders = {n for n, s in c["services"].items() if "broker_token" in _secret_names(s)}
    assert holders == {"orchestrator", "api", "pkg-mirror-auth"}  # M8.10b: sidecar asks the broker


def test_make_up_creates_broker_token():
    mk = Path("Makefile").read_text()
    assert "secrets/broker_token" in mk and "secrets/api_token" in mk


def test_sidecar_holds_the_broker_token_but_not_the_app_key():
    a = compose()["services"]["pkg-mirror-auth"]
    assert "broker_token" in a["secrets"] and "github_app_key" not in a["secrets"]
    assert "core-net" in a["networks"] and "mirror-net" in a["networks"]
    assert a["environment"]["FACTORY_BROKER_URL"] == "http://orchestrator:8091"
