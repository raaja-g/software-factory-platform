from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from swfactory.definition.loader import defaults_dir, init_definition_repo, load_definition
from swfactory.definition.models import BenchConfigDef, validate_definition
from swfactory.domain.types import Role, SelectionMode, TriageMode

SHA = "a" * 40
TASK = "003-todo-web"
CHECK = "tests/test_web.py"


def _task(**over: Any) -> dict[str, Any]:
    return {
        "title": "t",
        "feature": "f",
        "repo": "r",
        "base_sha": SHA,
        "merge_sha": "b" * 40,
        "definition_sha": "c" * 40,
        "hidden_checks": [CHECK],
        "check_command": "pytest {file}",
        "scorers": ["merge-readiness"],
        **over,
    }


def _write(root: Path, task: dict[str, Any], check: str | None = "import os\n") -> Path:
    init_definition_repo(root)
    s = root / "benchmarks" / "v1"
    (s / "tasks").mkdir(parents=True)
    (s / "suite.yaml").write_text(
        yaml.safe_dump({"configurations": [{"name": "base", "profile": "team-gemini"}]})
    )
    (s / "tasks" / f"{TASK}.yaml").write_text(yaml.safe_dump(task))
    if check is not None:
        f = s / "checks" / TASK / CHECK
        f.parent.mkdir(parents=True)
        f.write_text(check)
    return root


def _errors(root: Path) -> str:
    r = load_definition(str(root))
    assert r.definition is None
    return "\n".join(str(e) for e in r.errors)


def test_loader_reads_check_files(tmp_path: Path):
    d = load_definition(str(_write(tmp_path / "d", _task()))).definition
    assert d is not None
    assert d.benchmarks["v1"].tasks[TASK].check_files[CHECK].startswith("import")


def test_missing_check_file_error(tmp_path: Path):
    assert f"benchmarks/v1/checks/{TASK}/{CHECK}" in _errors(_write(tmp_path / "d", _task(), None))


def test_check_file_size_limit(tmp_path: Path):
    big = "x" * (256 * 1024 + 1)
    assert "over 256 KB" in _errors(_write(tmp_path / "d", _task(), big))


def test_check_files_not_from_yaml(tmp_path: Path):
    root = _write(tmp_path / "d", _task(check_files={CHECK: "x"}))
    assert "check_files" in _errors(root)


def test_check_command_needs_file_placeholder(tmp_path: Path):
    root = _write(tmp_path / "d", _task(check_command="make test"))
    assert "check_command must contain {file}" in _errors(root)


def test_unknown_scorer_and_bad_path(tmp_path: Path):
    root = _write(tmp_path / "d", _task(scorers=["ghost"]))
    assert "unknown scorer 'ghost'" in _errors(root)
    root = _write(tmp_path / "e", _task(hidden_checks=["../x"]))
    assert "must be relative" in _errors(root)


def test_mode_and_triage_overrides():
    c = BenchConfigDef.model_validate(
        {
            "name": "laya",
            "profile": "team-gemini",
            "roles": {"builder": {"mode": "laya"}},
            "triage_modes": {"kind": "laya"},
        }
    )
    assert (c.roles[Role.BUILDER].mode, c.triage_modes["kind"]) == (
        SelectionMode.LAYA,
        TriageMode.LAYA,
    )


def _base() -> dict[str, Any]:
    d = load_definition(str(defaults_dir())).definition
    assert d is not None
    return d.model_dump(mode="json", by_alias=True)


def errors_of(suite: dict[str, Any]) -> str:
    data = _base()
    data["benchmarks"] = {"v1": suite}
    defn, issues = validate_definition(data)
    assert defn is None or issues
    return "\n".join(i.msg for i in issues)


def suite(configs: int = 1, names: list[str] | None = None, **cfg: Any) -> dict[str, Any]:
    names = names or [f"c{i}" for i in range(configs)]
    return {"configurations": [{"name": n, "profile": "team-gemini", **cfg} for n in names]}


def test_unknown_triage_question():
    assert "colour" in errors_of(suite(triage_modes={"colour": "laya"}))


def test_config_limits():
    assert "at most 6 configurations" in errors_of(suite(configs=7))
    assert "duplicate configuration name 'a'" in errors_of(suite(names=["a", "a"]))
    assert "Bad Name" in errors_of(suite(names=["Bad Name"]))


def test_unknown_profile_and_defaults():
    assert "unknown profile 'nope'" in errors_of(
        {"configurations": [{"name": "a", "profile": "nope"}]}
    )
    data = _base()
    data["benchmarks"] = {"v1": suite()}
    defn, issues = validate_definition(data)
    assert defn is not None and not issues
    s = defn.benchmarks["v1"]
    assert (str(s.budget_usd), s.max_parallel) == ("40", 1)


def test_schema_omits_check_files():
    from swfactory.definition.models import definition_json_schema

    assert "check_files" not in definition_json_schema()["$defs"]["BenchTaskDef"]["properties"]


@pytest.mark.parametrize("bad", ["a" * 41, "-a", "A"])
def test_bad_suite_name(bad: str):
    data = _base()
    data["benchmarks"] = {bad: suite()}
    assert validate_definition(data)[0] is None
