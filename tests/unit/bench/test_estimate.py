from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from swfactory.bench.runner import bench_branch, estimate_run
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef, RoleOverride
from swfactory.domain.types import Role

SHA = "a" * 40
CONFIGS = (
    BenchConfigDef(name="gemini", profile="team-gemini"),
    BenchConfigDef(name="claude", profile="team-claude"),
    BenchConfigDef(
        name="economy",
        profile="team-economy",
        roles={Role.BUILDER: RoleOverride(model="cheap")},
    ),
)
TASKS = ("001-a", "002-b", "003-todo-web", "004-d")


def make_suite(scorers: tuple[str, ...]) -> BenchSuiteDef:
    task = BenchTaskDef(
        title="t",
        feature="f",
        repo="r",
        base_sha=SHA,
        merge_sha=SHA,
        definition_sha=SHA,
        check_command="pytest {file}",
        scorers=scorers,
    )
    return BenchSuiteDef(configurations=CONFIGS, tasks={n: task for n in TASKS})


V1 = make_suite(("merge-readiness", "scope-creep"))
H = {"team-gemini": [Decimal("1.00"), Decimal("1.50"), Decimal("2.00")]}
SC = {
    "001-a": Decimal("2.00"),
    "002-b": Decimal("2.50"),
    "003-todo-web": Decimal("3.00"),
    "004-d": Decimal("3.50"),
}


def run(suite: BenchSuiteDef, **kw):
    return estimate_run(
        suite,
        CONFIGS,
        repetitions=kw.get("repetitions", 1),
        history=kw.get("history", H),
        source_costs=SC,
        job_cap=Decimal("10"),
        judge_step_usd=Decimal("0.02"),
        default_scorers=4,
    )


def test_bench_branch():
    rid = UUID("0190f3a2-0000-7000-8000-00000000abcd")
    assert bench_branch(rid, "003-todo-web", "gemini") == "bench/0000abcd/003-todo-web/gemini"
    assert bench_branch(rid, "003-todo-web", "gemini", 2).endswith("/gemini-r2")


def test_estimate():
    e = run(V1)
    assert (e.trials, e.per_config["gemini"], e.per_config["claude"], e.basis["economy"]) == (
        12,
        Decimal("6.16"),
        Decimal("11.16"),
        "job budget cap",
    )
    assert e.per_config["economy"] == Decimal("40.16")
    assert (e.total_usd, e.max_usd) == (Decimal("57.48"), Decimal("120.48"))
    assert e.basis["gemini"] == "history" and e.basis["claude"] == "source job cost"


def test_judge_cost_uses_each_tasks_scorers():
    assert run(make_suite(())).max_usd == Decimal("120.96")


def test_history_below_three_jobs_is_ignored():
    e = run(V1, history={"team-gemini": [Decimal("1")]})
    assert e.basis["gemini"] == "source job cost"


def test_repetitions_multiply_trials():
    e = run(V1, repetitions=2)
    assert e.trials == 24 and e.total_usd == Decimal("114.96")
