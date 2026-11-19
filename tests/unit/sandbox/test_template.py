import re
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.template import (
    PROXY_ENV_KEYS,
    VOLUME_RE,
    container_config,
    home_volume_name,
    isolated_names,
    job_network_name,
    network_create_args,
    verified_volume_name,
    volume_name,
)

JOB = UUID("01890000-0000-7000-8000-000000000001")
EXP = datetime(2026, 1, 1, tzinfo=UTC)
NET = job_network_name(JOB)
STD = RunnerSpec("standard", "swfactory/runner:0.1.0", 2.0, 4096, 512, ())
SPECS = [
    STD,
    replace(STD, name="web", image="swfactory/runner-web:0.1.0"),
    replace(STD, cpus=0.5, memory_mb=512, pids=64),
    replace(STD, cpus=8.0, memory_mb=32768, pids=4096),
    replace(STD, allowed_hosts=("pypi.org", "github.com")),
]


def cfg(spec: RunnerSpec, variant: str) -> dict:
    if variant == "job":
        return container_config(
            JOB,
            spec,
            work=volume_name(JOB),
            home=home_volume_name(JOB),
            network=NET,
            expires_at=EXP,
        )
    return container_config(
        JOB,
        spec,
        work=isolated_names(JOB, "review-code-r1")[1],
        home=None,
        network=NET,
        expires_at=EXP,
        snapshot=verified_volume_name(JOB),
    )


job, iso = cfg(STD, "job"), cfg(STD, "iso")


def test_identity_and_privileges():
    hc = job["HostConfig"]
    assert job["User"] == "10001:10001" and hc["CapDrop"] == ["ALL"]
    assert hc["SecurityOpt"] == ["no-new-privileges:true"] and hc["ReadonlyRootfs"] is True
    assert hc["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,size=1g"} and hc["Init"] is True


@pytest.mark.parametrize("variant", ["job", "iso"])
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s.name}-{s.cpus}")
def test_never_privileged_no_binds_no_capadd_no_host_namespaces(spec, variant):
    hc = cfg(spec, variant)["HostConfig"]
    assert not hc.get("Privileged") and "Binds" not in hc
    assert not hc.get("CapAdd") and not hc.get("Devices")
    assert hc["NetworkMode"] == f"factory-net-{JOB}" and "PidMode" not in hc
    assert hc.get("IpcMode", "private") != "host"
    assert all(m["Type"] == "volume" and VOLUME_RE.fullmatch(m["Source"]) for m in hc["Mounts"])


def test_job_mounts_two_volumes():
    assert {
        m["Target"]: (m["Source"], m.get("ReadOnly", False)) for m in job["HostConfig"]["Mounts"]
    } == {
        "/work": (volume_name(JOB), False),
        "/home/factory": (home_volume_name(JOB), False),
    }


def test_isolated_mounts_snapshot_ro_and_fresh_home():
    hc = iso["HostConfig"]
    assert {m["Target"]: (m["Source"], m.get("ReadOnly", False)) for m in hc["Mounts"]} == {
        "/work": (isolated_names(JOB, "review-code-r1")[1], False),
        "/snapshot": (verified_volume_name(JOB), True),
    }
    assert hc["Tmpfs"]["/home/factory"] == "rw,nosuid,nodev,size=512m,uid=10001,gid=10001"
    assert iso["Labels"]["factory.ephemeral"] == "true" and "factory.ephemeral" not in job["Labels"]


def test_limits_from_runner_spec():
    hc = job["HostConfig"]
    assert (hc["NanoCpus"], hc["Memory"], hc["MemorySwap"], hc["PidsLimit"]) == (
        2_000_000_000,
        4096 * 2**20,
        4096 * 2**20,
        512,
    )
    assert hc["Ulimits"] == [{"Name": "nofile", "Soft": 4096, "Hard": 4096}]


def test_labels():
    assert job["Labels"] == {
        "factory.job_id": str(JOB),
        "factory.managed": "true",
        "factory.expires": str(int(EXP.timestamp())),
        "factory.max_work_gb": "20",
    }


def test_env_holds_no_secrets():
    assert not any(re.search("KEY|TOKEN|SECRET|PASSWORD", e) for e in job["Env"] + iso["Env"])


def test_job_network():
    assert job["NetworkingConfig"] == {"EndpointsConfig": {f"factory-net-{JOB}": {}}}
    args = network_create_args(JOB, EXP)
    assert (args["name"], args["driver"], args["internal"]) == (
        f"factory-net-{JOB}",
        "bridge",
        False,
    )
    assert args["labels"] == {
        "factory.job_id": str(JOB),
        "factory.managed": "true",
        "factory.expires": str(int(EXP.timestamp())),
    }


def test_runtime_and_proxy_env():
    assert "Runtime" not in job["HostConfig"]
    c = container_config(
        JOB, STD, work=volume_name(JOB), home=None, network=NET, expires_at=EXP,
        runtime="runsc", proxy_env={"HTTPS_PROXY": "http://egress:3128"},
    )  # fmt: skip
    assert c["HostConfig"]["Runtime"] == "runsc" and "HTTPS_PROXY=http://egress:3128" in c["Env"]


def test_rejects_host_network_and_bind_like_volume():
    def mk(**kw):
        base = dict(work=volume_name(JOB), home=None, network=NET, expires_at=EXP)
        return container_config(JOB, STD, **{**base, **kw})

    with pytest.raises(ValueError):
        mk(work="/var/run")
    for bad in ("host", "bridge", "none", "container:x", "sandbox-net", job_network_name(uuid4())):
        with pytest.raises(ValueError):
            mk(network=bad)
    with pytest.raises(ValueError):
        mk(snapshot="/")
    with pytest.raises(ValueError):
        mk(runtime="sysbox")
    with pytest.raises(ValueError):
        mk(proxy_env={"LD_PRELOAD": "x"})
    with pytest.raises(ValueError):
        isolated_names(JOB, "home")
    with pytest.raises(ValueError):
        isolated_names(JOB, "verified")
    assert verified_volume_name(JOB) == f"factory-job-{JOB}-verified"
    assert VOLUME_RE.fullmatch(verified_volume_name(JOB))


def test_mirror_env_is_allowed_and_reaches_the_container():
    from swfactory.mirror.config import mirror_env

    assert set(mirror_env()) <= PROXY_ENV_KEYS
    cfg = container_config(
        JOB, STD, work=volume_name(JOB), home=None, network=NET, expires_at=EXP,
        proxy_env=mirror_env(),
    )  # fmt: skip
    assert set(mirror_env()) <= {e.split("=", 1)[0] for e in cfg["Env"]}


@pytest.mark.parametrize(
    ("attach", "expect"), [(("gateway-edge", "pkg-mirror"), True), (("gateway-edge",), False)]
)
def test_provider_sets_mirror_env_only_when_attached(attach, expect):
    from swfactory.mirror.config import mirror_env
    from swfactory.sandbox.docker import DockerSandboxProvider

    p = DockerSandboxProvider(lambda: None, attach=attach)  # type: ignore[arg-type]
    assert (p._proxy_env() == mirror_env()) is expect and (p._proxy_env() is None) is not expect
