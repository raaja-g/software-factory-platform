"""Fixed hardened container template (pure: no I/O, no clock)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from swfactory.sandbox import paths
from swfactory.sandbox.base import RunnerSpec

LABEL_JOB = "factory.job_id"
LABEL_MANAGED = "factory.managed"
LABEL_EXPIRES = "factory.expires"
LABEL_EPHEMERAL = "factory.ephemeral"
LABEL_MAX_WORK_GB = "factory.max_work_gb"

SANDBOX_TTL = timedelta(days=7)
EPHEMERAL_TTL = timedelta(hours=2)

ALLOWED_RUNTIMES = frozenset({"runc", "runsc"})
PROXY_ENV_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NPM_CONFIG_REGISTRY",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "UV_DEFAULT_INDEX",
        "UV_INSECURE_HOST",
    }
)
VOLUME_RE = re.compile(r"^factory-job-[0-9a-f-]{36}(-[a-z0-9-]{1,40})?$")
_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_UID = 10001

LABEL_KIND = "factory.kind"
GITOPS_ROOT = "/git"
HARDENED_GIT_ENV: Mapping[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/tmp",
}


def volume_name(job_id: UUID) -> str:
    return f"factory-job-{job_id}"


def home_volume_name(job_id: UUID) -> str:
    return f"factory-job-{job_id}-home"


def verified_volume_name(job_id: UUID) -> str:
    return f"factory-job-{job_id}-verified"


def container_name(job_id: UUID) -> str:
    return f"factory-sandbox-{job_id}"


def job_network_name(job_id: UUID) -> str:
    return f"factory-net-{job_id}"


def isolated_names(job_id: UUID, name: str) -> tuple[str, str]:
    if not _NAME_RE.fullmatch(name) or name in ("home", "verified"):
        raise ValueError(f"invalid isolated container name: {name!r}")
    return (f"factory-sandbox-{job_id}-{name}", f"factory-job-{job_id}-{name}")


def _labels(job_id: UUID, expires_at: datetime) -> dict[str, str]:
    return {
        LABEL_JOB: str(job_id),
        LABEL_MANAGED: "true",
        LABEL_EXPIRES: str(int(expires_at.timestamp())),
    }


def network_create_args(job_id: UUID, expires_at: datetime) -> dict[str, Any]:
    return {
        "name": job_network_name(job_id),
        "driver": "bridge",
        "internal": False,
        "check_duplicate": True,
        "labels": _labels(job_id, expires_at),
    }


def _volume(name: str, what: str) -> str:
    if not VOLUME_RE.fullmatch(name):
        raise ValueError(f"{what} is not a factory job volume name: {name!r}")
    return name


def container_config(
    job_id: UUID,
    runner: RunnerSpec,
    *,
    work: str,
    home: str | None,
    network: str,
    expires_at: datetime,
    snapshot: str | None = None,
    runtime: str | None = None,
    proxy_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if network != job_network_name(job_id):
        raise ValueError("network must be this job's own network")
    if runtime is not None and runtime not in ALLOWED_RUNTIMES:
        raise ValueError(f"runtime not allowed: {runtime!r}")
    extra_env = dict(proxy_env or {})
    if not set(extra_env) <= PROXY_ENV_KEYS:
        raise ValueError("proxy_env may only hold proxy variables")

    mounts: list[dict[str, Any]] = [
        {"Type": "volume", "Source": _volume(work, "work"), "Target": "/work"}
    ]
    tmpfs = {"/tmp": "rw,nosuid,nodev,size=1g"}
    labels = _labels(job_id, expires_at)
    labels[LABEL_MAX_WORK_GB] = str(runner.max_work_gb)
    if home is None:
        tmpfs[paths.HOME] = f"rw,nosuid,nodev,size=512m,uid={_UID},gid={_UID}"
    else:
        mounts.append({"Type": "volume", "Source": _volume(home, "home"), "Target": paths.HOME})
    if snapshot is not None:
        mounts.append(
            {
                "Type": "volume",
                "Source": _volume(snapshot, "snapshot"),
                "Target": "/snapshot",
                "ReadOnly": True,
            }
        )
        labels[LABEL_EPHEMERAL] = "true"

    mem = runner.memory_mb * 2**20
    host_config: dict[str, Any] = {
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
        "Tmpfs": tmpfs,
        "Init": True,
        "Mounts": mounts,
        "NetworkMode": network,
        "NanoCpus": int(runner.cpus * 1_000_000_000),
        "Memory": mem,
        "MemorySwap": mem,
        "PidsLimit": runner.pids,
        "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 4096}],
    }
    if runtime is not None:
        host_config["Runtime"] = runtime

    env = {
        "HOME": paths.HOME,
        "UV_CACHE_DIR": f"{paths.HOME}/.cache/uv",
        "npm_config_cache": f"{paths.HOME}/.npm",
        "XDG_CACHE_HOME": f"{paths.HOME}/.cache",
        "TMPDIR": "/tmp",
        **extra_env,
    }
    return {
        "Image": runner.image,
        "User": f"{_UID}:{_UID}",
        "Cmd": ["sleep", "infinity"],
        "WorkingDir": "/work",
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": labels,
        "HostConfig": host_config,
        "NetworkingConfig": {"EndpointsConfig": {network: {}}},
    }


def gitops_volume(job_id: UUID) -> str:
    return f"gitops-{job_id}"


def gitops_config(
    job_id: UUID,
    *,
    image: str,
    network: str,
    argv: Sequence[str],
    token: str | None,
) -> dict[str, Any]:
    """Hardened config for a credentialed-git container: only the gitops volume, never a job's."""
    env = dict(HARDENED_GIT_ENV)
    if token is not None:
        env["GIT_TOKEN"] = token
    return {
        "Image": image,
        "User": f"{_UID}",
        "Cmd": list(argv),
        "WorkingDir": GITOPS_ROOT,
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {
            LABEL_JOB: str(job_id),
            LABEL_MANAGED: "true",
            LABEL_KIND: "gitops",
        },
        "HostConfig": {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=256m"},
            "Init": True,
            "Mounts": [{"Type": "volume", "Source": gitops_volume(job_id), "Target": GITOPS_ROOT}],
            "NetworkMode": network,
            "Memory": 2048 * 2**20,
            "MemorySwap": 2048 * 2**20,
            "PidsLimit": 256,
            "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 4096}],
        },
        "NetworkingConfig": {"EndpointsConfig": {network: {}}},
    }
