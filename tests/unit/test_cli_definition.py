from tests.unit.definition.test_loader import break_merge_policy
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.definition.loader import init_definition_repo


def test_cli_validate_ok_and_fail(tmp_path):
    d = tmp_path / "d"
    sha = init_definition_repo(d)
    ok = CliRunner().invoke(app, ["def", "validate", str(d)])
    assert ok.exit_code == 0 and ok.stdout.strip() == f"valid: {sha[:12]} (2 apps, 4 profiles)"
    bad = CliRunner().invoke(app, ["def", "validate", str(break_merge_policy(tmp_path / "b"))])
    assert bad.exit_code == 1 and bad.stdout.startswith("factory.yaml:14: apps.demo.merge_polcy:")


def test_cli_init_refuses_non_empty_dir(tmp_path):
    (tmp_path / "x").write_text("x")
    r = CliRunner().invoke(app, ["init", str(tmp_path)])
    assert r.exit_code == 1 and "not empty" in r.output


def test_cli_init_creates_repo(tmp_path):
    r = CliRunner().invoke(app, ["init", str(tmp_path / "new")])
    assert r.exit_code == 0 and (tmp_path / "new" / "factory.yaml").exists()
