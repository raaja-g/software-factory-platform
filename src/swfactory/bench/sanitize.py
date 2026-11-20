"""Sanitizing the checks box before a benchmark's hidden checks run in it.

The checks box is a clean clone of what the agent under test committed, and the hidden checks are
the benchmark's quality score. Python and pytest load a handful of files from the tree on their
own, so an agent that commits them can decide the score without doing the work.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath
from typing import Any

from swfactory.gitops.service import SAFE_GIT_ENV, safe_git
from swfactory.sandbox.paths import REPO

# Files that take over the interpreter or the test runner, wherever they sit in the tree.
TAKEOVER_FILES = (
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "pytest.py",
    "unittest.py",
    "pytest.pyc",  # a sourceless module in the cwd is importable too
    "unittest.pyc",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
)
TAKEOVER_SUFFIX = ".pth"
# Directories (or links to them) that shadow the test runner, because the cwd is on sys.path.
TAKEOVER_DIRS = ("pytest", "_pytest", "unittest")
# Installed environments. They are not part of what the agent committed: what is not tracked in
# git under them (a venv's own pytest and .pth files) is left alone; tracked files still count.
ENV_DIRS = frozenset(
    {".venv", "venv", "node_modules", ".tox", ".nox", "site-packages", "__pypackages__", ".eggs"}
)
TIMEOUT_S = 120
CHUNK = 200
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")


async def sanitize_tree(box: Any, base_sha: str, keep: Collection[str]) -> list[str]:
    """Undo, in the box's checkout of `REPO`, every file that could take over the test runner.

    Any path (outside ``.git``) named ``conftest.py``, ``sitecustomize.py``, ``usercustomize.py``,
    ``*.pth``, ``pytest.py``, ``unittest.py``, ``pytest.ini``, ``.pytest.ini`` or ``tox.ini``, or
    a directory (or link) named ``pytest``, ``_pytest`` or ``unittest``, is restored from the
    commit `base_sha` when that commit has it (a directory: its tracked files are restored and
    anything else in it is deleted), and deleted when it does not. Paths in `keep` (the task's
    own check files) are never touched. Returns the relative paths it changed, sorted.

    Nothing in the tree is executed or imported: the scan is ``find``, the rest is ``git`` with
    hooks and filters off. File names travel as argv items or NUL-separated output, never through
    a shell. Raises ``RuntimeError`` when it cannot be sure (the caller must not run the checks).

    Residual risk, deliberately not handled: ``pyproject.toml`` and ``setup.cfg`` carry pytest
    settings too, but a job may legitimately edit them, so they are NOT restored. Code that runs
    when the tree was set up (build backends, install scripts) is not covered either.
    """
    if not _SHA.fullmatch(base_sha):
        raise ValueError(f"not a commit id: {base_sha!r}")
    kept = {posixpath.normpath(k) for k in keep}
    found = [c for c in await _candidates(box) if c not in kept]
    if not found:
        return []
    tracked: list[str] | None = None
    todo: list[str] = []
    for path in found:
        if ENV_DIRS.intersection(PurePosixPath(path).parts[:-1]):
            if tracked is None:
                tracked = await _git_list(box, "ls-files", "-z")
            if not any(t == path or t.startswith(path + "/") for t in tracked):
                continue
        todo.append(path)
    if not todo:
        return []
    base_files = await _git_list(box, "ls-tree", "-r", "-z", "--name-only", base_sha)
    changed: set[str] = set()
    for path in todo:
        under = path + "/"
        restore = [f for f in base_files if f == path or f.startswith(under)]
        protected = [k for k in kept if k.startswith(under)]
        if not restore and not protected:
            await _run(box, ["rm", "-rf", "--", f"./{path}"], f"remove {path}")
            changed.add(path)
            continue
        wanted = {*restore, *protected}
        listing = await _run(
            box,
            ["find", f"./{path}", "(", "-type", "f", "-o", "-type", "l", ")", "-print0"],
            f"list {path}",
        )
        for extra in _paths(listing):
            if extra not in wanted:
                await _run(box, ["rm", "-rf", "--", f"./{extra}"], f"remove {extra}")
                changed.add(path)
        differs = await _differs(box, base_sha, restore)
        for start in range(0, len(differs), CHUNK):
            part = differs[start : start + CHUNK]
            for f in part:  # a link or a directory in the way: git would not replace it safely
                await _run(box, ["rm", "-rf", "--", f"./{f}"], f"remove {f}")
            await _run(
                box,
                safe_git("--literal-pathspecs", "checkout", base_sha, "--", *part),
                f"restore {path}",
                env=SAFE_GIT_ENV,
            )
            changed.add(path)
        # empty directories left behind are not worth keeping (an empty `pytest/` is a package)
        await box.exec(
            ["find", f"./{path}", "-type", "d", "-empty", "-delete"], cwd=REPO, timeout_s=TIMEOUT_S
        )
    return sorted(changed)


def _name_test() -> list[str]:
    names = [*TAKEOVER_FILES, f"*{TAKEOVER_SUFFIX}"]
    argv: list[str] = []
    for name in names:
        argv += ["-name", name, "-o"]
    dirs = ["(", *("-name", TAKEOVER_DIRS[0])]
    for name in TAKEOVER_DIRS[1:]:
        dirs += ["-o", "-name", name]
    dirs += [")", "(", "-type", "d", "-o", "-type", "l", ")"]
    return ["(", *argv, "(", *dirs, ")", ")"]


async def _candidates(box: Any) -> list[str]:
    """Every takeover candidate under the tree, relative to it (`.git` is never entered)."""
    argv = ["find", ".", "-name", ".git", "-prune", "-o", *_name_test(), "-print0", "-prune"]
    return _paths(await _run(box, argv, "scan the tree"))


def _paths(nul_separated: str) -> list[str]:
    out: list[str] = []
    for raw in nul_separated.split("\0"):
        if not raw:
            continue
        if "�" in raw:  # not valid UTF-8: we could not name it back to the tree
            raise RuntimeError(f"cannot sanitize a path that is not valid UTF-8: {raw!r}")
        out.append(posixpath.normpath(raw))
    return out


async def _git_list(box: Any, *args: str) -> list[str]:
    return _paths(await _run(box, safe_git(*args), f"git {args[0]}", env=SAFE_GIT_ENV))


async def _differs(box: Any, base_sha: str, files: Sequence[str]) -> list[str]:
    """Which of `files` (all present in `base_sha`) differ in the work tree, or are missing."""
    out: list[str] = []
    for start in range(0, len(files), CHUNK):
        argv = safe_git(
            "--literal-pathspecs",
            "diff",
            "--name-only",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            "--",
            *files[start : start + CHUNK],
        )
        out += _paths(await _run(box, argv, "compare with the base", env=SAFE_GIT_ENV))
    return out


async def _run(box: Any, argv: list[str], what: str, *, env: Any = None) -> str:
    res = await box.exec(argv, cwd=REPO, env=env, timeout_s=TIMEOUT_S)
    if res.exit_code != 0:
        raise RuntimeError(f"sanitizer could not {what}: {res.stderr.strip() or res.exit_code}")
    return res.stdout
