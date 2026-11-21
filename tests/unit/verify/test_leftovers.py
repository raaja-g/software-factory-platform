"""The gate's safety-net commit of whatever the builder left uncommitted.

It used to `git add -A`, which only honours the repo's own .gitignore. A repo with none (the demo
repo had no .gitignore) got Python bytecode (`__pycache__/*.pyc`) committed and merged. Known
build junk is now left out whether or not the repo ignores it; real source files still go in."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.fakes.host_sandbox import HostSandbox

from swfactory.sandbox.paths import REPO
from swfactory.verify.gate import commit_leftovers

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def put(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture
def repo(tmp_path: Path):
    box = HostSandbox(tmp_path / "box")
    root = tmp_path / "box" / "work" / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    put(root, {"README.md": "# demo\n"})
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return box, root


def tracked(root: Path) -> set[str]:
    return set(git(root, "ls-files").splitlines())


async def test_source_is_committed_and_bytecode_is_left_out(repo):
    box, root = repo
    put(root, {
        "src/app/__init__.py": "__version__ = '1'\n",
        "tests/test_app.py": "def test_x():\n    pass\n",
        "src/app/__pycache__/__init__.cpython-313.pyc": "bytecode",
        "tests/__pycache__/test_app.cpython-313-pytest-9.pyc": "bytecode",
    })  # fmt: skip
    assert await commit_leftovers(box, {"Factory-Job": "j1"}) is True
    assert tracked(root) == {"README.md", "src/app/__init__.py", "tests/test_app.py"}
    assert "Factory-Job: j1" in git(root, "log", "-1", "--format=%B")


@pytest.mark.parametrize(
    "junk",
    [
        "pkg/__pycache__/m.cpython-312.pyc",
        "m.pyc",
        "node_modules/left-pad/index.js",
        "web/node_modules/x/y.js",
        ".venv/bin/python",
        "sub/.venv/lib/x.py",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/3.12/x.json",
        ".ruff_cache/0/x",
        ".DS_Store",
        "docs/.DS_Store",
    ],
)
async def test_known_junk_is_never_committed(repo, junk):
    box, root = repo
    put(root, {"real.py": "x = 1\n", junk: "junk"})
    await commit_leftovers(box, {})
    assert tracked(root) == {"README.md", "real.py"}


async def test_a_tree_with_only_junk_makes_no_commit(repo):
    box, root = repo
    before = git(root, "rev-parse", "HEAD")
    put(root, {"src/__pycache__/a.pyc": "bytecode", ".DS_Store": "x"})
    assert await commit_leftovers(box, {}) is False
    assert git(root, "rev-parse", "HEAD") == before


async def test_a_clean_tree_makes_no_commit(repo):
    box, root = repo
    assert await commit_leftovers(box, {}) is False


async def test_the_repos_own_gitignore_still_applies(repo):
    box, root = repo
    put(root, {".gitignore": "secret.txt\n", "secret.txt": "s", "real.py": "x = 1\n"})
    await commit_leftovers(box, {})
    assert tracked(root) == {"README.md", ".gitignore", "real.py"}


async def test_source_files_with_similar_names_are_kept(repo):
    box, root = repo
    put(root, {
        "pycache_notes.md": "notes",
        "node_modules_policy.md": "policy",
        "src/venv_utils.py": "x = 1\n",
        "docs/.pytest_cache_explained.md": "doc",
    })  # fmt: skip
    await commit_leftovers(box, {})
    assert {"pycache_notes.md", "node_modules_policy.md", "src/venv_utils.py"} <= tracked(root)
    assert "docs/.pytest_cache_explained.md" in tracked(root)


def test_the_helper_runs_in_the_repo_directory():
    assert REPO == "/work/repo"
