from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from swfactory.definition.loader import defaults_dir, init_definition_repo, load_definition
from swfactory.definition.models import Definition, ImproverConfig
from swfactory.domain.types import Role
from swfactory.events import EVENT_KINDS

ON: dict[str, Any] = {"enabled": True, "bench_suite": "v1-parity"}


def _load(tmp: Path, improver: dict[str, Any], drop_role: tuple[str, str] | None = None):
    init_definition_repo(tmp)
    f = tmp / "factory.yaml"
    data = yaml.safe_load(f.read_text())
    data["improver"] = copy.deepcopy(improver)
    f.write_text(yaml.safe_dump(data))
    if drop_role:
        pf = tmp / "profiles" / f"{drop_role[0]}.yaml"
        pd = yaml.safe_load(pf.read_text())
        del pd["roles"][drop_role[1]]
        pf.write_text(yaml.safe_dump(pd))
    return load_definition(str(tmp))


def errs(tmp: Path, **kw: Any) -> str:
    r = _load(tmp, kw["improver"], kw.get("drop_role"))
    return "\n".join(str(e) for e in r.errors)


def shipped() -> Definition:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    return r.definition


def test_defaults():
    c = ImproverConfig()
    assert (c.enabled, c.failed_run_threshold, c.max_failure_age_days, c.bench_suite) == (
        False,
        25,
        7,
        None,
    )


def test_cycle_cap():
    assert ImproverConfig().max_cycle_usd == Decimal("120")
    with pytest.raises(ValidationError):
        ImproverConfig(max_cycle_usd=Decimal("0"))


def test_threshold_range():
    with pytest.raises(ValidationError) as e:
        ImproverConfig(failed_run_threshold=51)
    assert e.value.errors()[0]["loc"] == ("failed_run_threshold",)
    with pytest.raises(ValidationError):
        ImproverConfig(failed_run_threshold=0)
    assert ImproverConfig(failed_run_threshold=50).failed_run_threshold == 50


def test_shipped_defaults_load_and_keep_improver_off(tmp_path: Path):
    r = _load(tmp_path, {})
    assert r.definition is not None, r.errors
    imp = r.definition.factory.improver
    assert (imp.enabled, imp.bench_suite, imp.max_cycle_usd) == (False, None, Decimal("120"))
    shipped_yaml = yaml.safe_load((defaults_dir() / "factory.yaml").read_text())
    assert shipped_yaml["improver"] == {
        "enabled": False,
        "failed_run_threshold": 25,
        "max_failure_age_days": 7,
        "bench_suite": "v1-parity",
        "max_cycle_usd": 120,
    }


def test_enabled_with_shipped_suite_is_valid(tmp_path: Path):
    r = _load(tmp_path, ON)
    assert r.definition is not None, r.errors


def test_enabled_needs_known_suite(tmp_path: Path):
    assert "improver.bench_suite" in errs(
        tmp_path, improver={"enabled": True, "bench_suite": "nope"}
    )


def test_enabled_needs_a_suite_at_all(tmp_path: Path):
    assert "improver.bench_suite" in errs(tmp_path, improver={"enabled": True})


def test_enabled_needs_improver_role(tmp_path: Path):
    defn = _load(tmp_path / "ok", {}).definition
    assert defn is not None
    prof = defn.factory.defaults.profile
    assert "roles.improver" in errs(tmp_path / "bad", improver=ON, drop_role=(prof, "improver"))


def test_disabled_does_not_need_role_or_suite(tmp_path: Path):
    defn = _load(tmp_path / "ok", {}).definition
    assert defn is not None
    prof = defn.factory.defaults.profile
    r = _load(tmp_path / "bad", {"enabled": False}, drop_role=(prof, "improver"))
    assert r.definition is not None, r.errors


def test_shipped_profiles_have_agentic_improver():
    d = shipped()
    assert set(d.profiles) >= {"team-gemini", "claude-vertex", "mixed-economy", "local"}
    assert all(
        p.roles[Role.IMPROVER].engine in {"opencode", "claude_code"} for p in d.profiles.values()
    )
    assert d.profiles["local"].roles[Role.IMPROVER].model == "local@ollama"
    assert d.profiles["claude-vertex"].roles[Role.IMPROVER].engine == "claude_code"


def test_event_kind():
    assert "improvement.updated" in EVENT_KINDS
