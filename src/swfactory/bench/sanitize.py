"""Sanitizing the checks box before a benchmark's hidden checks run in it.

The checks box is a clean clone of what the agent under test committed, and the hidden checks are
the benchmark's quality score. They run in a tree the agent wrote, so the agent can try to decide
the score without doing the work. ``sanitize_tree`` takes away the two ways it can do that
through files the interpreter or the test runner load on their own:

1. Takeover files, by fixed name, anywhere in the tree: ``conftest.py``, ``sitecustomize.py``,
   ``usercustomize.py``, ``*.pth``, ``pytest.py``, ``unittest.py`` (and ``.pyc``), the pytest
   config files ``pytest.ini``, ``.pytest.ini``, ``tox.ini``, and directories (or links) named
   ``pytest``, ``_pytest``, ``unittest``.
2. Import shadowing, by protected module name, at the import roots. ``python -m pytest`` puts the
   cwd (the repo root) and, in pytest's default ``prepend`` import mode, the directory of each
   test file (the first directory upward without an ``__init__.py``) at the FRONT of
   ``sys.path``, ahead of the standard library and site-packages. A committed top-level
   ``pluggy.py``, ``re.py`` or ``json/`` package, a sourceless ``.pyc`` or a compiled ``.so`` is
   imported in place of the real module. The repo cannot be taken off ``sys.path`` (the task's
   own check command needs it importable), so what shadows is removed instead.

*Protected names* are computed inside the checks box (never on the host), by ``python3 -I`` run
from ``/`` so that nothing in the tree is imported: the standard library names
(``sys.stdlib_module_names``) plus the top-level import names of every installed distribution
(``importlib.metadata.packages_distributions`` and a scan of the site-packages directories for
``*.py``, ``*.pyc``, ``*.so``/``*.pyd`` stems and package directories), for the box's ``python3``
and, when present, the repo's ``.venv/bin/python``. Never ``-S``: the metadata needs
site-packages. Not protected: the project's own distribution (installed from the repo itself,
per its ``direct_url.json``), which is the agent's work, and a short list of very common project
names (``test``, ``tests``, ``src``, ``docs``, ``utils``, ``helpers``, ``fixtures`` ...) that
badly packaged distributions sometimes also install. A ``.venv`` that git tracks is never run:
it is the agent's code.

*Import roots* are where such a file would be found at the top level: the repo root; ``src/`` if it
exists; for every check file (`keep` path) its own directory (``python path/to/check.py`` puts the
script directory first) and the directory pytest puts first for it, the first directory upward
without an ``__init__.py``; and the same for every ancestor directory of a check file that holds a
``conftest.py`` (pytest imports it, and with it that directory). Only the top level of a root is
looked at: ``src/app/types.py`` is ``app.types``, not ``types``, and is left alone. This is
narrower than "every ancestor of a check file": a directory that is a package on the way up is
not itself on ``sys.path``, so a ``pkg/types.py`` is not a shadow.

An entry of a root (file, package, namespace directory, link) whose import name is protected
(for compiled names the part before the first dot: ``pluggy.cpython-312-x86_64-linux-gnu.so``
is ``pluggy``) is handled like the fixed takeover names: restored from the commit `base_sha` when
that commit has it (a directory: its tracked files are restored and anything else in it is
deleted, so an unchanged legitimate file stays as it is and is not reported), and deleted when it
does not. Paths in `keep` are never touched. Returns the relative paths it changed, sorted.

The trade-off: a job's legitimately added root-level module that is named like a standard library
or installed module (``packaging.py``, ``json.py``) is removed. It would shadow the real module
for the runner anyway, and the trial is not failed for it; the path is recorded (the caller logs
and stores it). ``helpers.py``, ``utils.py``, ``fixtures/``, ``mymodule.py`` and
``tests/helpers.py`` are not touched.

Nothing in the tree is executed or imported: the scan is ``find``, the rest is ``git`` with
hooks and filters off, and the discovery runs python isolated (``-I``) with cwd ``/``. File names
travel as argv items or NUL-separated output, never through a shell. Raises ``RuntimeError`` when
it cannot be sure (discovery failed, a tracked ``.venv``, a check path outside the repo); the
caller must not run the checks.

Residual risk, deliberately not handled: ``pyproject.toml`` and ``setup.cfg`` carry pytest
settings too (``pythonpath``, ``addopts``, plugins), but a job may legitimately edit them, so
they are NOT restored. Code that runs when the tree was set up (build backends, install scripts)
or at test time and writes into ignored or installed-environment paths (a ``.venv``, a
``site-packages``) is not covered either; neither are generic names a badly packaged distribution
shares with a project, or modules that only the check command's own imports reach from somewhere
else. The sanitizer does not make the code under test safe: it only keeps the runner honest.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Collection, Iterable, Sequence
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
# Names of project files and directories so common that distributions with sloppy packaging also
# install them as top-level modules; they are not protected (they would otherwise take a job's
# `tests/` away). `test` is the standard library's own, which the runner never imports.
COMMON_PROJECT_NAMES = frozenset(
    {
        "test",
        "tests",
        "src",
        "doc",
        "docs",
        "example",
        "examples",
        "script",
        "scripts",
        "benchmark",
        "benchmarks",
        "tool",
        "tools",
        "util",
        "utils",
        "helper",
        "helpers",
        "fixture",
        "fixtures",
        "conf",
        "config",
        "common",
        "lib",
    }
)
VENV_PYTHON = f"{REPO}/.venv/bin/python"
# Run by `python3 -I` from `/` inside the checks box: what a python of the box would import
# from outside the repo. Prints one JSON object. `own` distributions are the project itself.
DISCOVER = """
import importlib.metadata as md, json, os, re, site, sys, sysconfig
from urllib.parse import unquote, urlparse

repo = os.path.realpath(sys.argv[1])
norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()
own = set()
for dist in md.distributions():
    raw = dist.read_text("direct_url.json")
    if not raw:
        continue
    url = json.loads(raw).get("url", "")
    if url.startswith("file:"):
        path = os.path.realpath(unquote(urlparse(url).path))
        if path == repo or path.startswith(repo + os.sep):
            own.add(norm(dist.metadata["Name"]))
installed, own_tops = set(), set()
for top, dists in md.packages_distributions().items():
    if all(norm(d) in own for d in dists):
        own_tops.add(top)
    elif top.isidentifier():
        installed.add(top)
sites = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), *site.getsitepackages()}
for directory in sites:
    if not os.path.isdir(directory):
        continue
    for entry in os.scandir(directory):
        name = entry.name
        if entry.is_dir():
            mod = name
        elif name.endswith((".py", ".pyc")):
            mod = name.rsplit(".", 1)[0]
        elif name.endswith((".so", ".pyd")):
            mod = name.split(".", 1)[0]
        else:
            continue
        if mod.isidentifier() and mod != "__pycache__" and mod not in own_tops:
            installed.add(mod)
print(json.dumps({"stdlib": sorted(sys.stdlib_module_names), "installed": sorted(installed)}))
"""


async def sanitize_tree(box: Any, base_sha: str, keep: Collection[str]) -> list[str]:
    """Undo, in the box's checkout of `REPO`, every file that could take over the test runner or
    shadow a module it imports. The rule, the import roots, the protected names and the residual
    risk are in the module docstring. Returns the relative paths it changed, sorted.

    Paths in `keep` (the task's own check files) are never touched. Raises ``RuntimeError`` when it
    cannot be sure (the caller must not run the checks).
    """
    if not _SHA.fullmatch(base_sha):
        raise ValueError(f"not a commit id: {base_sha!r}")
    kept = {posixpath.normpath(k) for k in keep}
    for k in kept:
        if k.startswith(("/", "..")) or k == ".":
            raise RuntimeError(f"check path is not inside the repo: {k!r}")
    protected = await _protected_names(box)
    tree = _Tree(box, base_sha, kept)
    changed = await tree.undo(await _candidates(box))
    roots = await _import_roots(box, kept)
    changed |= await tree.undo(await _shadows(box, roots, protected))
    return sorted(changed)


class _Tree:
    """Restores or removes paths of the box's checkout against the base commit."""

    def __init__(self, box: Any, base_sha: str, kept: set[str]) -> None:
        self.box = box
        self.base_sha = base_sha
        self.kept = kept
        self._tracked: list[str] | None = None
        self._base_files: list[str] | None = None

    async def undo(self, found: Iterable[str]) -> set[str]:
        todo: list[str] = []
        for path in dict.fromkeys(found):
            if path in self.kept:
                continue
            if ENV_DIRS.intersection(PurePosixPath(path).parts[:-1]):
                if self._tracked is None:
                    self._tracked = await _git_list(self.box, "ls-files", "-z")
                if not any(t == path or t.startswith(path + "/") for t in self._tracked):
                    continue
            todo.append(path)
        changed: set[str] = set()
        if not todo:
            return changed
        if self._base_files is None:
            self._base_files = await _git_list(
                self.box, "ls-tree", "-r", "-z", "--name-only", self.base_sha
            )
        for path in todo:
            if await self._undo_one(path, self._base_files):
                changed.add(path)
        return changed

    async def _undo_one(self, path: str, base_files: list[str]) -> bool:
        box, base_sha = self.box, self.base_sha
        did = False
        under = path + "/"
        restore = [f for f in base_files if f == path or f.startswith(under)]
        protected = [k for k in self.kept if k.startswith(under)]
        if not restore and not protected:
            await _run(box, ["rm", "-rf", "--", f"./{path}"], f"remove {path}")
            return True
        wanted = {*restore, *protected}
        listing = await _run(
            box,
            ["find", f"./{path}", "(", "-type", "f", "-o", "-type", "l", ")", "-print0"],
            f"list {path}",
        )
        for extra in _paths(listing):
            if extra not in wanted:
                await _run(box, ["rm", "-rf", "--", f"./{extra}"], f"remove {extra}")
                did = True
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
            did = True
        # empty directories left behind are not worth keeping (an empty `pytest/` is a package)
        await box.exec(
            ["find", f"./{path}", "-type", "d", "-empty", "-delete"], cwd=REPO, timeout_s=TIMEOUT_S
        )
        return did


async def _protected_names(box: Any) -> frozenset[str]:
    """Module names a file at an import root must not have: see the module docstring."""
    pythons = [["python3"]]
    probe = await box.exec(["test", "-x", VENV_PYTHON], cwd=REPO, timeout_s=TIMEOUT_S)
    if probe.exit_code == 0:
        if await _git_list(box, "--literal-pathspecs", "ls-files", "-z", "--", ".venv"):
            raise RuntimeError("the tree commits a .venv: its python is the agent's, not run")
        pythons.append([VENV_PYTHON])
    names: set[str] = set()
    for python in pythons:
        res = await box.exec([*python, "-I", "-c", DISCOVER, REPO], cwd="/", timeout_s=TIMEOUT_S)
        what = f"discover the protected module names with {python[0]}"
        if res.exit_code != 0:
            raise RuntimeError(f"sanitizer could not {what}: {res.stderr.strip() or res.exit_code}")
        try:
            data = json.loads(res.stdout)
            stdlib, installed = data["stdlib"], data["installed"]
            names |= {n for n in stdlib if n not in COMMON_PROJECT_NAMES}
            names |= {n for n in installed if n not in COMMON_PROJECT_NAMES}
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"sanitizer could not {what}: unreadable answer ({exc})") from exc
    if len(names) < 100:  # an empty answer would protect nothing
        raise RuntimeError("sanitizer could not discover the protected module names: too few")
    return frozenset(names)


async def _exists(box: Any, directory: str, name: str) -> bool:
    argv = ["find", f"./{directory}", "-maxdepth", "1", "-name", name, "-print", "-quit"]
    res = await box.exec(argv, cwd=REPO, timeout_s=TIMEOUT_S)
    if res.exit_code != 0:
        if "No such file" in res.stderr:
            return False
        raise RuntimeError(f"sanitizer could not look in {directory}: {res.stderr.strip()}")
    return bool(res.stdout.strip())


async def _import_roots(box: Any, kept: Collection[str]) -> list[str]:
    """Directories (relative to the repo, `.` is the root) whose top level `sys.path` may start
    with when the checks run."""
    roots: set[str] = {"."}
    if await _exists(box, ".", "src"):
        roots.add("src")
    inits: dict[str, bool] = {}

    async def basedir(directory: str) -> str:
        """The first directory upward from `directory` without an __init__.py (pytest prepend)."""
        while directory != ".":
            if directory not in inits:
                inits[directory] = await _exists(box, directory, "__init__.py")
            if not inits[directory]:
                return directory
            directory = posixpath.dirname(directory) or "."
        return "."

    conftests: dict[str, bool] = {}
    for path in sorted(kept):
        own = posixpath.dirname(path) or "."
        roots.add(own)
        roots.add(await basedir(own))
        directory = own
        while directory != ".":
            if directory not in conftests:
                conftests[directory] = await _exists(box, directory, "conftest.py")
            if conftests[directory]:
                roots.add(await basedir(directory))
            directory = posixpath.dirname(directory) or "."
    return sorted(roots)


def _module_name(entry: str, *, directory_like: bool) -> str | None:
    """The top-level import name of a directory entry, or None when it is not importable."""
    name = posixpath.basename(entry)
    if directory_like and name.isidentifier():
        return name
    for suffix in (".py", ".pyc"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return stem if stem.isidentifier() else None
    for suffix in (".so", ".pyd"):
        if name.endswith(suffix):
            stem = name.split(".", 1)[0]
            return stem if stem.isidentifier() else None
    return None


async def _shadows(box: Any, roots: Sequence[str], protected: frozenset[str]) -> list[str]:
    """Top-level entries of the import roots whose import name is a protected module."""
    found: list[str] = []
    for root in roots:
        base = ["find", f"./{root}", "-mindepth", "1", "-maxdepth", "1"]
        dirs = _paths(
            await _run(
                box, [*base, "(", "-type", "d", "-o", "-type", "l", ")", "-print0"], f"scan {root}"
            )
        )
        others = _paths(await _run(box, [*base, "!", "-type", "d", "-print0"], f"scan {root}"))
        for entries, directory_like in ((dirs, True), (others, False)):
            found += [
                e for e in entries if _module_name(e, directory_like=directory_like) in protected
            ]
    return found


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
