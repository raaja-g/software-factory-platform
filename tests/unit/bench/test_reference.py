from __future__ import annotations

from swfactory.bench.reference import CheckVerdict, reference_verdict, task_check_key
from swfactory.definition.models import BenchTaskDef

SHA = "a" * 40
TASK = BenchTaskDef(
    title="t",
    feature="f",
    repo="local:demo",
    base_sha=SHA,
    merge_sha="b" * 40,
    definition_sha=SHA,
    check_command="pytest {file}",
    check_files={"tests/test_web.py": "x = 0"},
)


def test_reference_valid():
    assert reference_verdict({"tests/test_web.py": 0}, {"tests/test_web.py": 1}) == CheckVerdict(
        True, ""
    )


def test_check_passing_at_base_is_invalid():
    v = reference_verdict(
        {"tests/test_smoke.py": 0, "tests/test_web.py": 0},
        {"tests/test_smoke.py": 0, "tests/test_web.py": 1},
    )
    assert v == CheckVerdict(False, "tests/test_smoke.py passes at the base commit")


def test_check_failing_on_merge_is_invalid():
    v = reference_verdict({"tests/test_web.py": 5}, {"tests/test_web.py": 1})
    assert v.reason == "tests/test_web.py fails on the merged result (exit 5)"


def test_check_that_could_not_run_is_invalid():
    v = reference_verdict({"tests/test_web.py": None}, {})
    assert v.reason == "tests/test_web.py could not run on the merged result"


def test_check_that_could_not_run_at_base_is_invalid():
    v = reference_verdict({"tests/test_web.py": 0}, {"tests/test_web.py": None})
    assert v.reason == "tests/test_web.py could not run at the base commit"


def test_key_tracks_check_files_and_commits():
    base = task_check_key("v1", TASK)
    assert base == task_check_key("v1", TASK)
    changed = TASK.model_copy(update={"check_files": {"tests/test_web.py": "x = 1"}})
    assert base != task_check_key("v1", changed)
    assert base != task_check_key("v1", TASK.model_copy(update={"merge_sha": "c" * 40}))
