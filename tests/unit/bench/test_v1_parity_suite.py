import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swfactory.bench.tasks import build_v1_parity, seed_bench_repo
from swfactory.cli.main import app
from swfactory.definition.loader import defaults_dir, load_definition

SUITE = defaults_dir() / "benchmarks" / "v1-parity"
BUNDLE = SUITE / "repo.bundle"
V1 = Path("~/work/software-factory").expanduser()


def load_defaults():
    r = load_definition(str(defaults_dir()))
    assert not r.errors, r.errors
    assert r.definition is not None
    return r.definition


TASKS = list(load_defaults().benchmarks["v1-parity"].tasks.values())
SHIPPED_BASE_SHAS = {
    n: t.base_sha for n, t in load_defaults().benchmarks["v1-parity"].tasks.items()
}


def git(*args: str, cwd: Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return p.stdout


def heads_of(bundle: Path) -> set[str]:
    return {ln.split()[0] for ln in git("bundle", "list-heads", str(bundle)).splitlines()}


def parent_of(bundle: Path, sha: str, tmp: Path) -> str:
    repo = tmp / "p.git"
    if not repo.exists():
        git("init", "-q", "--bare", str(repo))
        git("-C", str(repo), "fetch", "-q", str(bundle), "refs/heads/*:refs/heads/*")
    return git("-C", str(repo), "rev-parse", f"{sha}^").strip()


def ls_tree(bundle: Path, sha: str, tmp: Path) -> list[str]:
    parent_of(bundle, TASKS[1].base_sha, tmp)  # makes sure the scratch repo exists
    return git("-C", str(tmp / "p.git"), "ls-tree", "-r", "--name-only", sha).splitlines()


def test_shipped_suite_validates():
    s = load_defaults().benchmarks["v1-parity"]
    assert list(s.tasks) == [
        "001-bootstrap-python",
        "002-todo-cli",
        "003-todo-web",
        "004-fix-origin-null-403",
    ]
    assert [c.name for c in s.configurations] == ["gemini", "claude", "economy"]
    assert s.budget_usd == Decimal("40") and s.max_parallel == 1
    assert s.tasks["004-fix-origin-null-403"].hidden_checks == (
        "tests/test_browser.py",
        "tests/test_web.py",
    )
    assert all(
        set(t.check_files) == set(t.hidden_checks)
        and re.fullmatch(r"[0-9a-f]{40}", t.base_sha)
        and t.scorers == ("merge-readiness", "spec-adherence")
        and t.success_criteria
        for t in s.tasks.values()
    )
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", t.merge_sha)
        and t.check_command == "python3 -m unittest -q {file}"
        for t in s.tasks.values()
    )


def test_app_is_defined():
    a = load_defaults().factory.apps["v1-parity"]
    assert (a.repo_kind, a.runner, a.test_command, a.merge_policy, a.profile) == (
        "local",
        "web",
        "make test",
        "local_merge",
        "team-gemini",
    )


def test_bundle_has_base_commits():
    assert {t.base_sha for t in TASKS} <= heads_of(BUNDLE)


def test_bundle_has_merge_commits(tmp_path):
    assert {t.merge_sha for t in TASKS} <= heads_of(BUNDLE)
    assert all(parent_of(BUNDLE, t.merge_sha, tmp_path) == t.base_sha for t in TASKS)


def test_bases_have_no_factory_dir(tmp_path):
    for t in TASKS:
        assert not any(p.startswith("factory/") for p in ls_tree(BUNDLE, t.base_sha, tmp_path))
        assert not any(p.startswith("factory/") for p in ls_tree(BUNDLE, t.merge_sha, tmp_path))


def test_history_is_linear(tmp_path):
    assert parent_of(BUNDLE, TASKS[1].base_sha, tmp_path) == TASKS[0].base_sha
    assert parent_of(BUNDLE, TASKS[3].base_sha, tmp_path) == TASKS[2].base_sha


def test_seed_imports_once_and_is_idempotent(tmp_path):
    bare = seed_bench_repo("v1-parity", BUNDLE, tmp_path)
    assert bare == tmp_path / "v1-parity.git"
    again = seed_bench_repo("v1-parity", BUNDLE, tmp_path)
    assert again == bare
    assert git("-C", str(bare), "rev-parse", "main").strip() == TASKS[-1].base_sha
    assert git("-C", str(bare), "rev-parse", "merged-004").strip() == TASKS[-1].merge_sha


def test_cli_seed_uses_the_shipped_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_REPOS_DIR", str(tmp_path))
    monkeypatch.delenv("FACTORY_DEFINITION", raising=False)
    r = CliRunner().invoke(app, ["bench", "seed", "v1-parity"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "v1-parity.git").is_dir()
    assert CliRunner().invoke(app, ["bench", "seed", "v1-parity"]).exit_code == 0
    assert CliRunner().invoke(app, ["bench", "seed", "nope"]).exit_code == 1


@pytest.mark.skipif(not V1.exists(), reason="opt-in: needs ~/work/software-factory")
def test_build_is_reproducible(tmp_path):
    assert build_v1_parity(V1, tmp_path) == SHIPPED_BASE_SHAS
