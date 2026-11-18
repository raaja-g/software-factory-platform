from pathlib import Path

import yaml

compose = yaml.safe_load(Path("compose.yaml").read_text())


def svc(name):
    return compose["services"][name]


def services():
    return list(compose["services"].items())


def mounts_socket(s):
    return any("docker.sock" in str(v) for v in s.get("volumes", []))


def test_proxy_grants():
    env = svc("docker-proxy")["environment"]
    for k in (
        "CONTAINERS",
        "POST",
        "EXEC",
        "IMAGES",
        "NETWORKS",
        "VOLUMES",
        "ALLOW_START",
        "ALLOW_STOP",
        "ALLOW_RESTARTS",
    ):
        assert str(env[k]) == "1"
    for k in ("BUILD", "SECRETS", "AUTH", "SYSTEM"):
        assert str(env[k]) == "0"
    proxy = svc("docker-proxy")
    assert proxy["read_only"] is True and "ports" not in proxy and "@sha256:" in proxy["image"]
    assert proxy["networks"] == ["docker-net"] and "core-net" in svc("postgres")["networks"]


def shares_net_with_proxy():
    proxy = set(svc("docker-proxy")["networks"])
    return sorted(
        n for n, s in services() if n != "docker-proxy" and proxy & set(s.get("networks", []))
    )


def test_proxy_alone_on_an_internal_network():
    assert compose["networks"]["docker-net"] == {"internal": True}
    assert set(shares_net_with_proxy()) <= {"orchestrator"}


def test_only_proxy_mounts_the_socket():
    assert [n for n, s in services() if mounts_socket(s)] == ["docker-proxy"]


def test_networks_and_repos_volume():
    assert "core-net" in compose["networks"] and "sandbox-net" not in compose["networks"]
    assert not any(n.startswith("factory-net") for n in compose["networks"])
    assert compose["volumes"]["repos"] == {}
