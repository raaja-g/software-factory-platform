import tomllib
from pathlib import Path


def test_pyproject_contract():
    p = tomllib.loads(Path("pyproject.toml").read_text())
    ini = p["tool"]["pytest"]["ini_options"]
    assert p["project"]["requires-python"] == ">=3.12,<3.14"
    assert p["project"]["scripts"]["factory"] == "swfactory.cli.main:app"
    assert {m.split(":")[0] for m in ini["markers"]} == {"db", "docker", "live", "e2e", "ui"}
    assert ini["addopts"].split()[0] == "--strict-markers" and "not ui" in ini["addopts"]
