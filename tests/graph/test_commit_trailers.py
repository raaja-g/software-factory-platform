from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.fakes.artifacts import InMemoryArtifactStore
from tests.fakes.gate_case import RUNNER, StubGit
from tests.fakes.host_sandbox import HostSandbox

from swfactory.gitops.hooks import PREPARE_COMMIT_MSG, install_trailer_hook
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.verify.gate import VerifyGate

JOB_S = "11111111-2222-3333-4444-555555555555"
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip


def _repo(box: HostSandbox) -> Path:
    repo = box.root / "work" / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return repo


def last_commit_message(box: HostSandbox) -> str:
    repo = box.root / "work" / "repo"
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def trailers_of_head(box: HostSandbox) -> dict[str, str]:
    repo = box.root / "work" / "repo"
    out = subprocess.run(
        ["git", "log", "-1", "--format=%(trailers:only,unfold)"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    pairs = [ln.split(": ", 1) for ln in out.splitlines() if ": " in ln]
    return {k: v for k, v in pairs if k.startswith("Factory-")}


@pytest.fixture
def host_box_with_repo(tmp_path: Path) -> HostSandbox:
    box = HostSandbox(tmp_path / "box")
    _repo(box)
    return box


def test_hook_script_is_posix_sh():
    assert PREPARE_COMMIT_MSG.startswith("#!/bin/sh")


async def test_shell_commit_gets_three_trailers(host_box_with_repo):
    box = host_box_with_repo
    await install_trailer_hook(box)
    res = await box.exec(
        ["bash", "-c", "echo x > f && git add f && git commit -qm 'add f'"],
        env={**IDENT, "FACTORY_JOB": JOB_S, "FACTORY_ROLE": "builder",
             "FACTORY_MODEL": "claude-haiku@vertex"},
    )  # fmt: skip
    assert res.exit_code == 0, res.stderr
    assert trailers_of_head(box) == {
        "Factory-Job": JOB_S,
        "Factory-Role": "builder",
        "Factory-Model": "claude-haiku@vertex",
    }


async def test_no_env_no_trailers(host_box_with_repo):
    box = host_box_with_repo
    await install_trailer_hook(box)
    env = {k: "" for k in ("FACTORY_JOB", "FACTORY_ROLE", "FACTORY_MODEL")}
    res = await box.exec(
        ["bash", "-c", "echo y > g && git add g && git commit -qm 'add g'"], env={**IDENT, **env}
    )
    assert res.exit_code == 0, res.stderr
    assert trailers_of_head(box) == {}


async def test_existing_trailers_not_duplicated(host_box_with_repo):
    box = host_box_with_repo
    await install_trailer_hook(box)
    res = await box.exec(
        ["bash", "-c",
         "echo z > h && git add h && git commit -qm 'add h' -m 'Factory-Role: builder'"],
        env={**IDENT, "FACTORY_JOB": JOB_S, "FACTORY_ROLE": "builder",
             "FACTORY_MODEL": "claude-haiku@vertex"},
    )  # fmt: skip
    assert res.exit_code == 0, res.stderr
    assert last_commit_message(box).count("Factory-Role:") == 1


async def test_gate_auto_commit_has_all_trailers(host_box_with_repo):
    box = host_box_with_repo
    repo = box.root / "work" / "repo"
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "base"], cwd=repo, check=True)  # fmt: skip
    subprocess.run(["git", "switch", "-q", "-c", "factory/001-x"], cwd=repo, check=True)
    (repo / "dirty.txt").write_text("edit")
    provider = FakeSandboxProvider()
    job = SimpleNamespace(id=uuid4(), seq=1, slug="x", branch="factory/001-x")
    app = SimpleNamespace(protected_paths=(), test_command=None, default_branch="main",
                          runner="standard", smoke=None)  # fmt: skip
    git = StubGit(True, ())
    gate = VerifyGate(git, InMemoryArtifactStore(), provider, None)  # type: ignore[arg-type]
    trailers = {"Factory-Job": JOB_S, "Factory-Role": "builder", "Factory-Model": "flash@vertex"}
    try:
        await gate.run(job, app, box, runner=RUNNER, round=1, attempt=1, trailers=trailers)
    except Exception:  # noqa: BLE001 - later steps run against fakes
        pass
    assert trailers_of_head(box) == trailers
