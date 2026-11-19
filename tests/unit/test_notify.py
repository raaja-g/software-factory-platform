import json
from datetime import timedelta
from uuid import UUID

import pytest
import structlog
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.domain.types import FactoryMode
from swfactory.notify import Notifier
from swfactory.settings import Settings
from swfactory.watchdog import job_link, waiting_reminder

J = UUID("00000000-0000-0000-0000-000000000001")


async def test_send_posts_text_and_link(respx_mock):
    route = respx_mock.post("https://hooks.example/x").respond(200)
    await Notifier("https://hooks.example/x").send(
        "job.failed", "demo:004 failed: boom", link="https://factory.example.com/#/job/J"
    )
    assert json.loads(route.calls.last.request.content) == {
        "text": "[factory] job.failed: demo:004 failed: boom\nhttps://factory.example.com/#/job/J"
    }


async def test_send_without_url_logs_only():
    with structlog.testing.capture_logs() as logs:
        await Notifier(None).send("job.failed", "x")
    assert [entry["event"] for entry in logs] == ["notify.log_only"]


async def test_send_never_raises(respx_mock):
    respx_mock.post("https://hooks.example/x").respond(500)
    with structlog.testing.capture_logs() as logs:
        await Notifier("https://hooks.example/x").send("job.failed", "x")
    assert logs[-1]["event"] == "notify.failed"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("FACTORY_NOTIFY_WEBHOOK_URL", raising=False)
    s = Settings(mode=FactoryMode.PERSONAL, _env_file=None)
    assert (s.notify_webhook_url, s.notify_waiting_after_h) == (None, 4)


def test_empty_webhook_means_unset(monkeypatch):
    monkeypatch.setenv("FACTORY_NOTIFY_WEBHOOK_URL", "")
    assert Settings(mode=FactoryMode.PERSONAL).notify_webhook_url is None


def test_webhook_from_file(monkeypatch, tmp_path):
    f = tmp_path / "hook"
    f.write_text("https://hooks.example/secret-path\n")
    monkeypatch.delenv("FACTORY_NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("FACTORY_NOTIFY_WEBHOOK_URL_FILE", str(f))
    s = Settings(mode=FactoryMode.PERSONAL)
    assert s.notify_webhook_url is not None
    assert s.notify_webhook_url.get_secret_value() == "https://hooks.example/secret-path"


@pytest.mark.parametrize("hours,expected", [(3, None), (4, 0), (27.9, 0), (28, 1), (52, 2)])
def test_waiting_reminder(hours, expected):
    assert waiting_reminder(waited=timedelta(hours=hours), after_h=4) == expected


def test_job_link():
    s = Settings(mode=FactoryMode.SERVER, public_url="https://factory.example.com")
    assert job_link(s, J) == f"https://factory.example.com/#/job/{J}"


def test_notify_test_cli_log_only(monkeypatch):
    monkeypatch.delenv("FACTORY_NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FACTORY_NOTIFY_WEBHOOK_URL_FILE", raising=False)
    r = CliRunner().invoke(app, ["notify", "test"])
    assert r.exit_code == 0 and "log only" in r.stdout
