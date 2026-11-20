"""Acceptance: run the shipped v1-parity benchmark suite (opt-in, needs `make up`).

Seed the repo first: `docker compose exec orchestrator factory bench seed v1-parity`.
"""

import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swfactory.cli.api_client import ApiClient
from swfactory.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]
DONE = ("completed", "complete", "failed", "cancelled", "budget_stopped")


class BenchApi:
    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def start_bench(self, suite: str, configs: list[str]) -> str:
        est = self.client.bench_estimate(suite, configs=configs)
        out = self.client.bench_run(suite, accept_estimate_usd=est["estimate_usd"], configs=configs)
        return str(out["run_id"])

    def report(self, run: str) -> dict[str, Any]:
        return self.client.bench_report(run)


def wait_report(api: BenchApi, run: str, timeout_s: float, poll_s: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        rep = api.report(run)
        if rep["status"] in DONE or time.monotonic() > deadline:
            return rep
        time.sleep(poll_s)


def report_md(rep: dict[str, Any]) -> str:
    lines = [
        f"# v1-parity benchmark run {rep['id']}",
        "",
        f"Status {rep['status']}, spent ${Decimal(rep['cost_usd']):.2f} of "
        f"${Decimal(rep['budget_usd']):.2f}.",
        "",
        "| config | trials | completed | hidden pass | mean rounds | cost |",
        "|---|---|---|---|---|---|",
    ]
    for c in rep["configs"]:
        lines.append(
            f"| {c['name']} | {c['trials']} | {c['completed']} | {c['hidden_pass_rate']} "
            f"| {c['mean_rounds']} | ${Decimal(c['total_cost_usd']):.2f} |"
        )
    for task, rate in (rep["configs"][0].get("task_pass") or {}).items():
        lines.append(f"\n- hidden checks, {task}: {rate}")
    lines.append(f"\nInvalid tasks: {rep['invalid_tasks'] or 'none'}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def api() -> BenchApi:
    if not os.environ.get("FACTORY_E2E_BENCH"):
        pytest.skip("opt-in: set FACTORY_E2E_BENCH=1 with `make up` and `factory bench seed`")
    return BenchApi(ApiClient.from_settings(get_settings()))


@pytest.mark.e2e
def test_v1_parity_gemini(api):
    run = api.start_bench("v1-parity", configs=["gemini"])
    rep = wait_report(api, run, timeout_s=4 * 3600)
    assert rep["invalid_tasks"] == []  # every check passes on its merge and fails at its base
    assert rep["configs"][0]["completed"] == 4 and Decimal(rep["cost_usd"]) <= Decimal("40")
    (ROOT / "ref/acceptance/m10-v1-parity.md").write_text(report_md(rep))


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("FACTORY_E2E_CLAUDE") != "1", reason="opt-in: claude-vertex replay"
)
def test_v1_parity_claude(api):
    run = api.start_bench("v1-parity", configs=["claude"])
    rep = wait_report(api, run, timeout_s=4 * 3600)
    assert rep["configs"][0]["completed"] == 4 and Decimal(rep["cost_usd"]) <= Decimal("40")
    (ROOT / "ref/acceptance/m10-v1-parity-claude.md").write_text(report_md(rep))
