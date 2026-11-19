import json

import structlog
import yaml
from structlog.testing import capture_logs
from tests.unit.mirror.test_config import REGS

from swfactory.mirror.service import sync_mirror


def lambda_token(value):
    async def get():
        return value

    return get


async def test_sync_writes_private_files_and_reports_change(tmp_path, defn_with_registries):
    defn = defn_with_registries(REGS)
    env = tmp_path / ".env"
    env.write_text("REGISTRY_ACME_PY_TOKEN=tok-abc\n")
    out = tmp_path / "out"
    out.mkdir()
    kw = {"out_dir": out, "env_file": env, "adc": lambda: "ya29.x", "github": lambda_token("ghs_x")}
    assert await sync_mirror(defn, **kw) is True
    assert {p.name for p in out.iterdir()} == {"npm.yaml", "pypi.json", "ready"}
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in out.iterdir())
    assert await sync_mirror(defn, **kw) is False


async def test_one_failed_token_does_not_stop_the_others(tmp_path, defn_with_registries):
    defn = defn_with_registries(REGS)
    structlog.reset_defaults()
    with capture_logs() as logs:
        await sync_mirror(defn, out_dir=tmp_path, env_file=None, adc=lambda: "ya29.x", github=None)
    cfg = yaml.safe_load((tmp_path / "npm.yaml").read_text())
    assert cfg["uplinks"]["ar"]["auth"]["token"] == "ya29.x" and "gh" not in cfg["uplinks"]
    assert {e["registry"] for e in logs if e["event"] == "mirror.token_failed"} == {"gh", "py"}
    assert (tmp_path / "ready").exists()
    assert json.loads((tmp_path / "pypi.json").read_text())["routes"] == [
        {"prefix": "acme-", "deny": True}
    ]
