import json

import pytest
from pydantic import ValidationError
from tests.fakes.gate_case import run_gate

from swfactory.definition.models import AppDef, SmokeConfig
from swfactory.domain.types import VerifyStatus
from swfactory.sandbox.base import ExecResult
from swfactory.sandbox.fake import FakeSandbox
from swfactory.verify.smoke import (
    SmokeResult,
    SmokeRunner,
    parse_smoke_output,
    screenshot_path,
)

CFG = SmokeConfig(
    start_command="python3 app.py", port=8000, paths=["/", "/about"], screenshots=["home", "about"]
)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8


def _er(out: str = "") -> ExecResult:
    return ExecResult(0, out, "", False, 0.0)


def _box(payload: dict) -> FakeSandbox:
    from uuid import uuid4

    box = FakeSandbox(uuid4(), [_er("4242\n"), _er(json.dumps(payload) + "\n"), _er()])
    box.files["/tmp/smoke/home.png"] = PNG
    box.files["/tmp/smoke/about.png"] = PNG
    return box


@pytest.fixture
def smoke_box_500():
    return _box({"ready": True, "statuses": {"/": 200, "/about": 500}, "console_errors": []})


@pytest.fixture
def smoke_box_not_ready():
    return _box({"ready": False, "statuses": {}, "console_errors": []})


class FakeSmokeRunner:
    def __init__(self, shots, passed=True):
        self.shots, self.passed = tuple(shots), passed

    async def run(self, box, cfg, *, timeout_s=300):
        return SmokeResult(self.passed, self.shots, {"/": 200}, (), "ok" if self.passed else "bad")


def test_config_requires_matching_names():
    with pytest.raises(ValidationError):
        SmokeConfig(start_command="x", port=8000, paths=["/"], screenshots=[])


def test_config_name_pattern():
    with pytest.raises(ValidationError):
        SmokeConfig(start_command="x", port=8000, paths=["/"], screenshots=["Home"])


def test_smoke_requires_web_runner():
    with pytest.raises(ValidationError, match="web"):
        AppDef.model_validate(
            {
                "repo_kind": "local",
                "repo_url": "d",
                "runner": "standard",
                "smoke": {
                    "start_command": "x",
                    "port": 8000,
                    "paths": ["/"],
                    "screenshots": ["home"],
                },
            }
        )


def test_smoke_defaults_to_none():
    app = AppDef.model_validate({"repo_kind": "local", "repo_url": "d", "runner": "standard"})
    assert app.smoke is None and "url" not in SmokeConfig.model_fields


def test_screenshot_path():
    assert screenshot_path("home") == "/work/.factory/in/screenshots/home.png"


def test_parse_output_last_json_line():
    assert parse_smoke_output('noise\n{"ready": true, "statuses": {"/": 200}}\n')["ready"] is True


async def test_non_2xx_path_fails(smoke_box_500):
    r = await SmokeRunner().run(smoke_box_500, CFG)
    assert not r.passed and "/about 500" in r.detail and r.statuses == {"/": 200, "/about": 500}
    assert (
        smoke_box_500.exec_calls[-1][-1].startswith("kill -- -4242")
        or "kill -- -4242" in (smoke_box_500.exec_calls[-1][-1])
    )


async def test_not_ready_fails(smoke_box_not_ready):
    r = await SmokeRunner().run(smoke_box_not_ready, CFG)
    assert not r.passed and r.detail == "app not ready on port 8000 after 60 s"


async def test_all_ok_passes():
    r = await SmokeRunner().run(
        _box({"ready": True, "statuses": {"/": 200, "/about": 302}, "console_errors": ["x"]}), CFG
    )
    assert r.passed and [n for n, _ in r.shots] == ["home", "about"] and r.console_errors == ("x",)


async def test_gate_writes_shots_into_job_in():
    c = await run_gate(smoke=FakeSmokeRunner([("home", PNG)]))
    assert c.r.screenshots == ("home",)
    assert await c.job_box.read_bytes(screenshot_path("home")) == PNG
    assert await c.iso.read_bytes(screenshot_path("home")) == PNG
    assert c.kinds["screenshots/home.png"] == "image"
    assert "| Smoke | passed |" in c.r.report


async def test_gate_smoke_failure_fails():
    c = await run_gate(smoke=FakeSmokeRunner([], passed=False))
    assert c.r.status == VerifyStatus.FAILED and c.r.reason == "smoke failed: bad"
