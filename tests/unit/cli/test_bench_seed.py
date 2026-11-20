from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.settings import get_settings


def test_seed_imports_the_bundle_and_says_how_many_base_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_REPOS_DIR", str(tmp_path))
    monkeypatch.delenv("FACTORY_DEFINITION", raising=False)
    get_settings.cache_clear()
    first = CliRunner().invoke(app, ["bench", "seed", "v1-parity"])
    assert first.exit_code == 0, first.output
    assert first.stdout.strip() == "seeded v1-parity: 4 base commits"
    assert (tmp_path / "v1-parity.git").is_dir()
    again = CliRunner().invoke(app, ["bench", "seed", "v1-parity"])
    assert again.exit_code == 0 and again.stdout.strip() == "seeded v1-parity: 4 base commits"
    get_settings.cache_clear()
