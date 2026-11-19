import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

CI = Path(".github/workflows/ci.yml")


def test_ci_lint_and_unit_jobs():
    wf = yaml.safe_load(CI.read_text())
    assert {"lint", "unit"} <= set(wf["jobs"]) and wf["env"]["FACTORY_CI"] == "1"
    lint = " ".join(s.get("run", "") for s in wf["jobs"]["lint"]["steps"])
    assert all(
        c in lint for c in ("uv run ruff check", "uv run ruff format --check", "uv run pyright")
    )
    assert "uv run pytest -q" in [s.get("run", "").strip() for s in wf["jobs"]["unit"]["steps"]]


def test_actions_pinned_by_sha():
    wf = yaml.safe_load(CI.read_text())
    uses = [s["uses"] for j in wf["jobs"].values() for s in j["steps"] if "uses" in s]
    assert uses and [u for u in uses if not re.search(r"@[0-9a-f]{40}$", u)] == []


def test_read_only_permissions():
    assert yaml.safe_load(CI.read_text())["permissions"] == {"contents": "read"}


def run_pytest(tmp_path: Path, body: str, *, ci: bool) -> int:
    (tmp_path / "test_x.py").write_text(body)
    env = {**os.environ, "FACTORY_CI": "1" if ci else ""}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
    ).returncode


def test_ci_turns_skips_into_failures(tmp_path):
    skip = "import pytest\ndef test_a(): pytest.skip('no runner images')\n"
    opt_in = "import pytest\ndef test_a(): pytest.skip('opt-in: set FACTORY_VM_TESTS=1')\n"
    assert run_pytest(tmp_path, skip, ci=True) == 1 and run_pytest(tmp_path, skip, ci=False) == 0
    assert run_pytest(tmp_path, opt_in, ci=True) == 0


def test_ci_db_job():
    steps = yaml.safe_load(CI.read_text())["jobs"]["db"]["steps"]
    runs = [s.get("run", "").strip() for s in steps]
    assert any(
        r.startswith("FACTORY_TEST_DATABASE_URL=") and r.endswith("uv run pytest -m db -q")
        for r in runs
    )
    assert any(r in ("docker compose up -d --wait postgres", "make test-db") for r in runs)


def test_ci_docker_job_builds_images_and_sets_db():
    steps = yaml.safe_load(CI.read_text())["jobs"]["docker"]["steps"]
    runs = [s.get("run", "").strip() for s in steps]
    build = runs.index("docker compose --profile build build runner runner-web")
    test = next(i for i, r in enumerate(runs) if r.endswith("uv run pytest -m docker -q"))
    assert build < test and runs[test].startswith("FACTORY_TEST_DATABASE_URL=")
    assert any(r in ("docker compose up -d --wait postgres", "make test-db") for r in runs[:test])


def load(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def run_lines(node: dict) -> list[str]:
    steps = (
        node["steps"] if "steps" in node else [s for j in node["jobs"].values() for s in j["steps"]]
    )
    return [
        re.sub(r"^FACTORY_TEST_DATABASE_URL=(?:\$\([^)]*\)|\S)+\s+", "", s["run"])
        for s in steps
        if "run" in s
    ]


def job(name: str) -> dict:
    return load(".github/workflows/ci.yml")["jobs"][name]


def test_jobs():
    assert set(load(".github/workflows/ci.yml")["jobs"]) == {"lint", "unit", "db", "docker", "ui"}


def test_four_pytest_runs():
    cmds = [c.strip() for c in run_lines(load(".github/workflows/ci.yml"))]
    assert [c for c in cmds if c.startswith("uv run pytest")] == [
        "uv run pytest -q",
        "uv run pytest -m db -q",
        "uv run pytest -m docker -q",
        "uv run pytest -m ui -q",
    ]


def test_db_job_starts_compose_postgres():
    assert "make test-db" in [c.strip() for c in run_lines(job("db"))]
    assert "make test-db" in [c.strip() for c in run_lines(job("docker"))]


def test_ui_job_installs_chromium():
    runs = [c.strip() for c in run_lines(job("ui"))]
    assert "uv run playwright install --with-deps chromium" in runs


def test_no_live_or_e2e_in_ci():
    runs = run_lines(load(".github/workflows/ci.yml"))
    assert [c for c in runs if re.search(r"-m\s+['\"]?(live|e2e)", c)] == []
