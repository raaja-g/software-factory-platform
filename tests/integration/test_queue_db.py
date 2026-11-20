from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import JobKind
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db

C1 = "c" * 40
D1 = "d" * 40
ENQ: dict[str, Any] = {
    "app_slug": "v1-parity",
    "feature": "f",
    "title": None,
    "source": "api",
    "source_ref": None,
    "requested_by": "admin",
}


@pytest.fixture
async def active(sessions):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    fac = r.definition.factory.model_copy(
        update={"apps": {"v1-parity": AppDef.model_validate(base.model_dump(mode="json"))}}
    )
    r = replace(
        r,
        sha=f"sha-queue-{datetime.now(UTC).timestamp()}",
        definition=r.definition.model_copy(update={"factory": fac}),
    )
    assert await DefinitionStore(sessions).save(r)


async def test_enqueue_benchmark_fields_atomic(sessions, active):
    async with sessions.begin() as s:
        job = await enqueue_job(
            s, **ENQ, kind=JobKind.BENCHMARK,
            branch="bench/0000abcd/t/c", base_sha=C1, definition_sha=D1,
        )  # fmt: skip
    assert (job.branch, job.base_sha, job.definition_sha) == ("bench/0000abcd/t/c", C1, D1)


async def test_enqueue_rejects_bench_fields_for_normal(sessions, active):
    extras: list[dict[str, Any]] = [{"branch": "x"}, {"base_sha": C1}, {"definition_sha": D1}]
    for extra in extras:
        async with sessions() as s:
            with pytest.raises(ValueError, match="only for benchmark jobs"):
                await enqueue_job(s, **ENQ, **extra)
