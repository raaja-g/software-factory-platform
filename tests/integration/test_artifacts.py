import pytest
from sqlalchemy import text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.artifacts import MAX_DB_BYTES, ArtifactStore
from swfactory.logging import register_secret
from swfactory.sandbox.fake import FakeSandbox
from swfactory.sandbox.paths import IN

pytestmark = pytest.mark.db


@pytest.fixture
async def job_id(sessions):
    await seed_demo(sessions)
    return (await enq(sessions, app="demo")).id


@pytest.fixture
def store(sessions, tmp_path):
    return ArtifactStore(sessions, tmp_path)


@pytest.fixture
def box(job_id):
    return FakeSandbox(job_id)


@pytest.fixture
def row(sessions):
    async def _row(job_id, path):
        async with sessions() as s:
            return (
                await s.execute(
                    text("SELECT * FROM app.artifacts_latest WHERE job_id=:j AND path=:p"),
                    {"j": job_id, "p": path},
                )
            ).one()

    return _row


@pytest.fixture
def rows(sessions):
    async def _rows(job_id, path):
        async with sessions() as s:
            return (
                await s.execute(
                    text("SELECT * FROM app.artifacts WHERE job_id=:j AND path=:p"),
                    {"j": job_id, "p": path},
                )
            ).all()

    return _rows


async def test_small_text_in_db(store, job_id, row):
    meta = await store.put(job_id, "spec.md", "a" * 10_240)
    r = await row(job_id, "spec.md")
    assert (meta.size, r.content, r.storage_uri) == (10_240, "a" * 10_240, None)


async def test_large_text_to_volume(store, job_id, row):
    text_ = "b" * (MAX_DB_BYTES + 1)
    await store.put(job_id, "logs/build.log", text_, kind="log")
    r = await row(job_id, "logs/build.log")
    assert r.content is None and r.storage_uri.startswith("file://")
    assert await store.get_text(job_id, "logs/build.log") == text_


async def test_same_content_is_one_row(store, job_id, rows):
    await store.put(job_id, "build.md", "v1")
    await store.put(job_id, "build.md", "v1")
    assert len(await rows(job_id, "build.md")) == 1
    await store.put(job_id, "build.md", "v2")
    assert len(await rows(job_id, "build.md")) == 2
    assert await store.get_text(job_id, "build.md") == "v2"


async def test_bytes_always_to_file(store, job_id, row):
    data = b"\x89PNG" + bytes(5_116)
    await store.put(job_id, "screenshots/home.png", data, kind="image")
    assert (await row(job_id, "screenshots/home.png")).storage_uri.startswith("file://")
    assert await store.get_text(job_id, "screenshots/home.png") is None
    assert await store.get_bytes(job_id, "screenshots/home.png") == data
    assert await store.get_bytes(job_id, "missing.png") is None


async def test_put_redacts_registered_secrets(store, job_id):
    register_secret("canary-3b9d1f7a-secret")
    await store.put(job_id, "build.md", "key=canary-3b9d1f7a-secret")
    assert await store.get_text(job_id, "build.md") == "key=[REDACTED]"


async def test_materialize_into_in(store, job_id, box):
    await store.put(job_id, "round-1/review-code.md", "VERDICT: PASS")
    await store.put(job_id, "spec.md", "# Spec")
    await store.materialize(job_id, box, paths=["round-1/review-code.md"])
    assert await box.read_text(f"{IN}/round-1/review-code.md") == "VERDICT: PASS"
    assert await box.read_text(f"{IN}/spec.md") is None
