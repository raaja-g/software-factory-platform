import pytest
from alembic.script import ScriptDirectory
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.migrate import alembic_config
from swfactory.settings import get_settings

pytestmark = pytest.mark.db


def test_db_upgrade_prints_revision(pg_url, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    r = CliRunner().invoke(app, ["db", "upgrade"])
    assert r.exit_code == 0, r.output
    head = ScriptDirectory.from_config(alembic_config(pg_url)).get_current_head()  # not hardcoded
    assert r.stdout.strip() == f"database at revision {head}"
