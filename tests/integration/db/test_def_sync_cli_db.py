import pytest
from typer.testing import CliRunner

from swfactory.cli.main import app

pytestmark = pytest.mark.db


def test_def_sync_without_source_asks_the_api_to_notify(pg_url, api_env):
    r = CliRunner().invoke(app, ["def", "sync"])
    assert r.exit_code == 0, r.output
    assert "sync requested" in r.stdout
