from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import AUDIENCE, FakeKeys, server

from swfactory.api import access as access_mod
from swfactory.api.app import ApiDeps, create_app
from swfactory.api.auth import hash_token
from swfactory.api.routes import jobs as jobs_routes
from swfactory.artifacts import ArtifactMeta

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
SHA = "a" * 40
JOB_ID = uuid4()
JOB = {"app": "todo", "feature": "Add a thing"}
TOKENS = {"viewer": "sfp_v", "submitter": "sfp_s", "approver": "sfp_a", "admin": "sfp_d"}


class Users:
    async def role_of(self, email):
        return email.split("@")[0]

    async def token_email(self, token_hash):
        for role, tok in TOKENS.items():
            if hash_token(tok) == token_hash:
                return f"{role}@x.io"
        return None


class Sessions:
    def __call__(self):
        return self._ctx()

    @asynccontextmanager
    async def _ctx(self):
        yield SimpleNamespace()

    def begin(self):
        return self._ctx()


class Definitions:
    async def active(self):
        app = SimpleNamespace(budget_usd=None, access=None)
        budgets = SimpleNamespace(job_usd=Decimal("10"), job_max_usd=Decimal("25"))
        return "sha", SimpleNamespace(factory=SimpleNamespace(apps={"todo": app}, budgets=budgets))


class Artifacts:
    def __init__(self):
        now = datetime.now(UTC)
        self.items = {
            "screenshots/home.png": ("image", PNG),
            "notes.html": ("md", b"<script>x</script>"),
        }
        self.now = now

    async def list(self, job_id):
        return [ArtifactMeta(p, k, "", len(b), None, self.now) for p, (k, b) in self.items.items()]

    async def get_bytes(self, job_id, path):
        return self.items[path][1]

    async def get_text(self, job_id, path):
        return self.items[path][1].decode()


class Events:
    def __init__(self):
        self.emitted = []

    async def emit(self, job_id, kind, data):
        self.emitted.append((kind, dict(data)))


class FakeCommands:
    def __init__(self):
        self.calls = []

    async def __call__(self, s, **kw):
        self.calls.append(kw)
        return uuid4()


async def slug_of(s, job):
    return "todo"


@pytest.fixture
def fake_commands(monkeypatch):
    fake = FakeCommands()

    async def resolve(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="submitter@x.io")

    monkeypatch.setattr(jobs_routes, "submit_command", fake)
    monkeypatch.setattr(access_mod, "resolve_job_ref", resolve)
    monkeypatch.setattr(access_mod, "app_slug_of", slug_of)
    return fake


@pytest.fixture
def fake_artifacts(monkeypatch):
    async def resolve(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="x")

    monkeypatch.setattr(access_mod, "resolve_job_ref", resolve)
    monkeypatch.setattr(access_mod, "app_slug_of", slug_of)


def make(role):
    s = server()
    deps = ApiDeps(
        sessions=Sessions(),  # pyright: ignore[reportArgumentType]
        events=Events(),  # pyright: ignore[reportArgumentType]
        artifacts=Artifacts(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=Definitions(),  # pyright: ignore[reportArgumentType]
        users=Users(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=s,
    )
    app = create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    c = TestClient(app, base_url="http://127.0.0.1:7080")
    c.headers["authorization"] = f"Bearer {TOKENS[role]}"
    c.deps = deps  # pyright: ignore[reportAttributeAccessIssue]
    return c


@pytest.fixture
def client_viewer():
    return make("viewer")


@pytest.fixture
def client_submitter():
    return make("submitter")


@pytest.fixture
def client_approver():
    return make("approver")


@pytest.fixture
def client_admin():
    return make("admin")


def post_action(c, action, **kw):
    return c.post("/api/jobs/todo:1/actions", json={"action": action, **kw})


def test_audience_constant_used():
    assert AUDIENCE


def test_submitter_cannot_approve(client_submitter, fake_commands):
    assert post_action(client_submitter, "approve").status_code == 403
    assert fake_commands.calls == []


def test_viewer_cannot_post_actions(client_viewer, fake_commands):
    assert post_action(client_viewer, "cancel").status_code == 403


def test_submitter_cancels_own_job_only(client_submitter, fake_commands, monkeypatch):
    assert post_action(client_submitter, "cancel").status_code == 202
    assert fake_commands.calls[0]["requested_by"] == "submitter@x.io"

    async def other(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="someone@x.io")

    monkeypatch.setattr(access_mod, "resolve_job_ref", other)
    assert post_action(client_submitter, "cancel").status_code == 403
    assert len(fake_commands.calls) == 1


def test_action_body_cannot_set_requested_by(client_approver, fake_commands):
    r = post_action(client_approver, "approve", requested_by="boss@x.io")
    assert r.status_code == 422


def test_rework_needs_note(client_approver, fake_commands):
    assert post_action(client_approver, "rework", note="").status_code == 422


def test_head_sha_must_be_hex40(client_approver, fake_commands):
    assert post_action(client_approver, "approve", head_sha="main").status_code == 422


def test_action_records_requested_by_and_emits(client_approver, fake_commands):
    r = post_action(client_approver, "approve", head_sha=SHA)
    assert r.status_code == 202 and "command_id" in r.json()
    assert fake_commands.calls[0]["requested_by"] == "approver@x.io"
    assert fake_commands.calls[0]["head_sha"] == SHA
    kind, data = client_approver.deps.events.emitted[0]
    assert (kind, data["action"], data["by"], data["status"]) == (
        "human.action", "approve", "approver@x.io", "requested",
    )  # fmt: skip


def test_refused_command_is_409(client_approver, monkeypatch):
    from swfactory.commands import CommandRefused

    async def refuse(s, **kw):
        raise CommandRefused("the branch moved; reload")

    async def resolve(s, ref):
        return SimpleNamespace(id=JOB_ID, requested_by="x")

    monkeypatch.setattr(jobs_routes, "submit_command", refuse)
    monkeypatch.setattr(access_mod, "resolve_job_ref", resolve)
    monkeypatch.setattr(access_mod, "app_slug_of", slug_of)
    r = post_action(client_approver, "approve")
    assert (r.status_code, r.json()["detail"]) == (409, "the branch moved; reload")


@pytest.mark.parametrize(
    "path", ["..%2f..%2fetc%2fpasswd", "%2fetc%2fpasswd", "round-1%2f..%2f..%2fx"]
)
def test_artifact_path_traversal_refused(client_viewer, fake_artifacts, path):
    assert client_viewer.get(f"/api/jobs/todo:1/artifacts/{path}").status_code == 400


def test_artifact_png_served_as_image(client_viewer, fake_artifacts):
    r = client_viewer.get("/api/jobs/todo:1/artifacts/screenshots/home.png")
    assert (r.status_code, r.headers["content-type"], r.content) == (200, "image/png", PNG)
    assert "content-disposition" not in r.headers


def test_non_png_image_is_octet_stream(client_viewer, fake_artifacts):
    client_viewer.deps.artifacts.items["x.png"] = ("image", b"GIF89a")
    r = client_viewer.get("/api/jobs/todo:1/artifacts/x.png")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == 'attachment; filename="x.png"'


def test_text_artifact_is_a_plain_text_download(client_viewer, fake_artifacts):
    r = client_viewer.get("/api/jobs/todo:1/artifacts/notes.html")
    assert (r.headers["content-type"], r.headers["content-disposition"], r.text) == (
        "text/plain; charset=utf-8",
        'attachment; filename="notes.html"',
        "<script>x</script>",
    )


def test_missing_artifact_404(client_viewer, fake_artifacts):
    assert client_viewer.get("/api/jobs/todo:1/artifacts/nope.md").status_code == 404


def test_create_over_budget_422_for_submitter(client_submitter):
    assert client_submitter.post("/api/jobs", json={**JOB, "budget_usd": "50"}).status_code == 422
    assert client_submitter.post("/api/jobs", json={**JOB, "budget_usd": "11"}).status_code == 422


def test_create_job_rejects_extra_fields(client_submitter):
    r = client_submitter.post("/api/jobs", json={**JOB, "requested_by": "x"})
    assert r.status_code == 422


def test_viewer_cannot_create(client_viewer):
    assert client_viewer.post("/api/jobs", json=JOB).status_code == 403


def test_add_budget_needs_admin(client_approver, fake_commands):
    assert post_action(client_approver, "add_budget", amount_usd="5").status_code == 403
    assert fake_commands.calls == []


def test_add_budget_amount_must_be_positive(client_admin, fake_commands):
    assert post_action(client_admin, "add_budget", amount_usd="0").status_code == 422
    assert post_action(client_admin, "add_budget").status_code == 422
    assert post_action(client_admin, "retry", amount_usd="5").status_code == 422
    assert fake_commands.calls == []
