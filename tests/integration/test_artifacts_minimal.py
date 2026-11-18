import pytest
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.artifacts import ArtifactStore
from swfactory.sandbox.fake import FakeSandbox
from swfactory.sandbox.paths import IN

pytestmark = pytest.mark.db


async def test_put_get_list_materialize(sessions, tmp_path):
    await seed_demo(sessions)
    j = (await enq(sessions, app="demo")).id
    store = ArtifactStore(sessions, tmp_path)
    box = FakeSandbox(j)
    await store.put(j, "spec.md", "v1")
    meta = await store.put(j, "spec.md", "v2")
    assert meta.size == 2 and len(meta.sha256) == 64
    assert (await store.get_text(j, "spec.md"), [m.path for m in await store.list(j)]) == (
        "v2",
        ["spec.md"],
    )
    await store.materialize(j, box, ["spec.md"])
    assert await box.read_text(f"{IN}/spec.md") == "v2"
