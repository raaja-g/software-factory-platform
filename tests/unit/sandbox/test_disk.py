import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from swfactory.domain.types import FactoryMode
from swfactory.sandbox.base import RunnerSpec, WorkspaceOverLimit
from swfactory.sandbox.disk import (
    HeadTailBuffer,
    claim_new_allowed,
    disk_paths,
    disk_usage_pct,
    over_work_limit,
)
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.settings import Settings

J = uuid4()
STD = RunnerSpec("standard", "img", 2, 4096, 512, ())


def _settings(**kw):
    return Settings(FACTORY_MODE=FactoryMode.PERSONAL, _env_file=None, **kw)


def test_head_tail_keeps_both_ends():
    b = HeadTailBuffer(10)
    for chunk in (b"0123456789", b"abcdefghij", b"KLMNOPQRST"):
        b.feed(chunk)
    assert b.getvalue() == b"01234\n[omitted: 20 bytes]\nPQRST" and b.omitted == 20


def test_head_tail_under_limit_is_untouched():
    b = HeadTailBuffer(10)
    b.feed(b"abc")
    b.feed(b"def")
    assert b.getvalue() == b"abcdef" and b.omitted == 0


def test_disk_usage_reports_the_fullest_path(monkeypatch):
    stats = {"/": (1000, 400), "/repos": (1000, 90)}  # (f_blocks, f_bavail)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda p: SimpleNamespace(f_blocks=stats[p][0], f_bavail=stats[p][1], f_frsize=4096),
    )
    assert disk_usage_pct(["/", "/repos"]) == (91, "/repos")


def test_disk_paths_skip_missing(tmp_path):
    s = _settings(FACTORY_REPOS_DIR=str(tmp_path), FACTORY_ARTIFACTS_DIR=str(tmp_path / "absent"))
    assert disk_paths(s) == ("/", str(tmp_path))


def test_settings_defaults_and_bounds():
    s = _settings()
    assert (s.disk_high_water_pct, s.exec_output_max_bytes) == (85, 2_000_000)
    with pytest.raises(ValidationError):
        _settings(FACTORY_DISK_HIGH_WATER_PCT=100)


def test_claim_new_allowed_up_to_the_mark():
    assert claim_new_allowed(85, 85) and not claim_new_allowed(86, 85)


def test_over_work_limit():
    assert over_work_limit(used_kb=20 * 2**20 + 1, max_work_gb=20)
    assert not over_work_limit(used_kb=20 * 2**20, max_work_gb=20)


async def test_marked_job_raises_until_destroyed():
    provider = FakeSandboxProvider()
    box = await provider.ensure(J, STD)
    provider.mark_over_limit(J, 20)
    with pytest.raises(WorkspaceOverLimit, match="workspace over 20 GB"):
        await box.exec(["true"])
    await provider.destroy(J)
    assert (await (await provider.ensure(J, STD)).exec(["true"])).exit_code == 0
