import re
from pathlib import Path


def read(p: str) -> str:
    return Path(p).read_text()


def lines(p: str) -> list[str]:
    return [ln for ln in read(p).splitlines() if ln.strip()]


def requirements(*paths: str) -> list[list[str]]:
    """One block per pin (a requirement line plus its hash continuation lines), comments dropped."""
    out: list[list[str]] = []
    for p in paths:
        cur: list[str] = []
        for ln in read(p).splitlines():
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if not ln.startswith(" "):
                cur = [ln]
                out.append(cur)
            else:
                cur.append(ln)
    return out


def test_dockerfile_supply_chain():
    d = read("docker/router.Dockerfile")
    assert "--extra-index-url" not in d and d.count("--require-hashes --no-deps") == 2
    assert "--index-url https://download.pytorch.org/whl/cpu -r /router/torch.lock" in d
    assert "--index-url https://pypi.org/simple -r /router/requirements.lock" in d
    assert re.search(r"revision=['\"][0-9a-f]{40}['\"]", d)
    assert "sha256sum -c /router/weights.sha256" in d
    assert re.search(r"^FROM python:3\.11-slim-trixie@sha256:[0-9a-f]{64}$", d, re.M)
    assert all(
        re.fullmatch(r"[0-9a-f]{64}  \S+\.(safetensors|json|txt)", ln)
        for ln in lines("router/weights.sha256")
    )
    for pickle in ("*.bin", "*.pt", "*.pkl"):
        assert pickle not in d
    reqs = requirements("router/torch.lock", "router/requirements.lock")
    assert reqs and all(any("--hash=sha256:" in ln for ln in req) for req in reqs)


def test_dockerfile_runtime_settings():
    d = read("docker/router.Dockerfile")
    for env in (
        "HF_HUB_OFFLINE=1",
        "LAYA_PORT=8000",
        "LAYA_PRELOAD=1",
        "LAYA_MAX_LOADED=1",
        "LAYA_DEVICE=cpu",
    ):
        assert env in d
    assert "USER 10001" in d and 'CMD ["python", "/router/shim.py"]' in d
    assert re.search(r"chown 10001:10001 /heads", d)


def test_locks_pin_what_the_plan_names():
    torch = read("router/torch.lock")
    assert "torch==2.14.0" in torch and len(requirements("router/torch.lock")) >= 1
    assert all(r[0].startswith("torch==2.14.0") for r in requirements("router/torch.lock"))
    assert "laya==0.4.31" in read("router/requirements.lock")
    assert not re.search(r"^(nvidia|triton|torch)", read("router/requirements.lock"), re.M)


def test_weights_hash_every_checkpoint_file():
    names = {ln.split("  ")[1] for ln in lines("router/weights.sha256")}
    assert {
        "model.safetensors",
        "multilingual/model.safetensors",
        "typed-decisions/model.safetensors",
    } <= names


def test_shim_requires_key(monkeypatch, capsys):
    import importlib.util

    import pytest

    spec = importlib.util.spec_from_file_location("router_shim", "router/shim.py")
    assert spec and spec.loader
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    with pytest.raises(SystemExit) as ei:
        shim.main()
    assert ei.value.code == 1 and "LAYA_API_KEY is required" in capsys.readouterr().err
    one = shim.add_head_version({"answers": {}})
    assert one["head_version"] is None  # type: ignore[index]
    batch = shim.add_head_version({"results": [{"answers": {}}, {"answers": {}}]})
    assert [r["head_version"] for r in batch["results"]] == [None, None]  # type: ignore[index]
