import pytest
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.settings import get_settings

pytestmark = pytest.mark.db


def test_db_upgrade_prints_revision(pg_url, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    r = CliRunner().invoke(app, ["db", "upgrade"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == "database at revision 0006_run_cost_nullable"
