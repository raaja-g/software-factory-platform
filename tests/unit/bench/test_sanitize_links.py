"""The hidden checks' sanitizer against symlinked import roots and ``__init__.py`` differentials.

``find ./src`` does not follow a symlink ``src``, but python does when it imports from it; and
``find -name __init__.py`` matches links of any kind, while pytest asks ``Path.is_file()``. Either
way the directory python or pytest really puts on ``sys.path`` could go unscanned. These tests
commit such trees, show the attack rigs a failing hidden check on the unsanitized tree (real
``python -m pytest``), and show the normalisation stage of ``sanitize_tree`` takes it away. They
also pin what must stay: honest trees, nested ``types.py`` / ``json.py``, links away from the
import-root paths.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from _pytest.pathlib import resolve_package_path
from tests.unit.bench.test_sanitize_shadow import (
    HARD_EXIT,
    HELPER,
    HIDDEN,
    PYTEST_CMD,
    World,
    commit,
    git,
    put,
)

from swfactory.bench.sanitize import _import_roots, sanitize_tree

# the shadow: a module the check imports, which ends the process with exit code 0 when imported
CHECK = "import wave\n" + HIDDEN
PYPROJECT = '[tool.pytest.ini_options]\npythonpath = ["src"]\n'  # `src` is on sys.path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def relink(root: Path, link: str, target: str, files: Mapping[str, str]) -> None:
    """Replace `link` by a symlink to `target` and put `files` (paths in the repo) in place."""
    path = root / link
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    put(root, files)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


async def rigged(world: World, check_path: str) -> None:
    """The unsanitized tree lets the failing check pass (exit 0): the attack works."""
    put(world.root, {check_path: CHECK})
    code, out = await world.run([*PYTEST_CMD[:-1], check_path])
    assert code == 0, f"the attack should work on the unsanitized tree:\n{out}"


async def fails_honestly(world: World, check_path: str) -> None:
    put(world.root, {check_path: CHECK})
    code, out = await world.run([*PYTEST_CMD[:-1], check_path])
    assert code == 1 and "shout" in out, out  # the feature is missing, nothing else


def not_written_through(world: World, *paths: str) -> None:
    for rel in paths:
        assert not (world.root / rel).exists(), f"{rel}: the check was written through the link"


# --- finding 1: a symlinked import root --------------------------------------------------------


async def test_src_as_a_symlink_to_a_directory_with_a_shadow(world: World):
    world.agent({"pyproject.toml": PYPROJECT})
    relink(world.root, "src", "evil", {"evil/wave.py": HARD_EXIT, "evil/app.py": "x = 1\n"})
    commit(world.root, "agent")
    await rigged(world, "tests/test_hidden.py")

    changed = await world.sanitize()
    assert changed == ["src"]
    assert (world.root / "src").is_dir() and not (world.root / "src").is_symlink()
    assert (world.root / "src/app.py").read_text() != "x = 1\n"  # the base's own is back
    await fails_honestly(world, "tests/test_hidden.py")


async def test_src_as_a_symlink_the_base_does_not_have_stays_absent(world: World):
    git(world.root, "rm", "-rq", "src")
    world.base = commit(world.root, "base without src")
    world.agent({"pyproject.toml": PYPROJECT})
    relink(world.root, "src", "evil", {"evil/wave.py": HARD_EXIT, "evil/app.py": "x = 1\n"})
    commit(world.root, "agent")
    put(world.root, {"tests/test_hidden.py": CHECK})
    code, out = await world.run(PYTEST_CMD)
    assert code == 0, out

    assert await world.sanitize() == ["src"]
    assert not (world.root / "src").exists() and not (world.root / "src").is_symlink()
    code, out = await world.run(PYTEST_CMD)
    assert code != 0 and "No module named 'src" in out, out  # and not the shadow's exit code 0


async def test_tests_as_a_symlink_to_a_directory_with_a_shadow(world: World):
    relink(world.root, "tests", "attack_tests", {"attack_tests/wave.py": HARD_EXIT})
    commit(world.root, "agent")
    await rigged(world, "tests/test_hidden.py")
    (world.root / "attack_tests/test_hidden.py").unlink()  # the write above went through the link

    changed = await world.sanitize()
    assert changed == ["tests"]
    assert (world.root / "tests").is_dir() and not (world.root / "tests").is_symlink()
    assert (world.root / "tests/conftest.py").exists()  # restored from the base
    await fails_honestly(world, "tests/test_hidden.py")
    not_written_through(world, "attack_tests/test_hidden.py")


async def test_a_symlinked_parent_of_a_nested_check_file(world: World):
    # `pkg/tests` is a package, so pytest puts `pkg` (the link itself) first on sys.path
    attack = {"attack_pkg/tests/__init__.py": "", "attack_pkg/wave.py": HARD_EXIT}
    relink(world.root, "pkg", "attack_pkg", attack)
    commit(world.root, "agent")
    await rigged(world, "pkg/tests/test_x.py")
    (world.root / "attack_pkg/tests/test_x.py").unlink()

    changed = await world.sanitize(frozenset({"pkg/tests/test_x.py"}))
    assert changed == ["pkg"]
    assert not (world.root / "pkg").exists()  # the base has no pkg: stays absent
    await fails_honestly(world, "pkg/tests/test_x.py")  # the platform writes real directories
    assert (world.root / "pkg/tests").is_dir() and not (world.root / "pkg").is_symlink()
    not_written_through(world, "attack_pkg/tests/test_x.py")


async def test_the_check_files_own_directory_as_a_symlink(world: World):
    relink(world.root, "tests/unit", "../attack_unit", {"attack_unit/wave.py": HARD_EXIT})
    commit(world.root, "agent")
    await rigged(world, "tests/unit/test_hidden.py")
    (world.root / "attack_unit/test_hidden.py").unlink()

    changed = await world.sanitize(frozenset({"tests/unit/test_hidden.py"}))
    assert changed == ["tests/unit"]
    await fails_honestly(world, "tests/unit/test_hidden.py")
    not_written_through(world, "attack_unit/test_hidden.py")


async def test_a_check_file_that_is_a_symlink_is_replaced_not_written_through(world: World):
    outside = world.root / "attack_sink.py"
    world.agent({"attack_sink.py": "# sink\n"})
    (world.root / "tests/test_hidden.py").symlink_to("../attack_sink.py")
    commit(world.root, "agent")

    assert await world.sanitize() == ["tests/test_hidden.py"]
    put(world.root, {"tests/test_hidden.py": CHECK})
    assert outside.read_text() == "# sink\n"
    assert not (world.root / "tests/test_hidden.py").is_symlink()


async def test_a_symlinked_deep_chain_is_cleaned_top_down(world: World):
    # `a` is a link; the link target holds a `b` that is a link as well
    relink(world.root, "a", "evil", {"evil/wave.py": HARD_EXIT})
    (world.root / "evil/b").symlink_to("../evil2")
    put(world.root, {"evil2/wave.py": HARD_EXIT})
    commit(world.root, "agent")

    assert await world.sanitize(frozenset({"a/b/test_x.py"})) == ["a"]
    assert not (world.root / "a").exists()


async def test_an_undone_link_fails_closed(world: World, monkeypatch: pytest.MonkeyPatch):
    relink(world.root, "src", "evil", {"evil/wave.py": HARD_EXIT})
    commit(world.root, "agent")
    real = world.box.exec

    async def no_rm(argv, **kw):
        if argv[0] == "rm" or "checkout" in argv:  # a removal that silently does nothing
            return await real(["true"], **kw)
        return await real(argv, **kw)

    monkeypatch.setattr(world.box, "exec", no_rm)
    with pytest.raises(RuntimeError, match="symlink"):
        await world.sanitize()


async def test_a_path_resolving_outside_the_repo_fails_closed(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    outside = world.root.parent / "outside"
    (outside / "unit").mkdir(parents=True)
    real = world.box.exec

    async def no_rm(argv, **kw):
        if argv[0] == "rm" or "checkout" in argv:
            return await real(["true"], **kw)
        return await real(argv, **kw)

    relink(world.root, "tests/unit", str(outside / "unit"), {})
    commit(world.root, "agent2")
    monkeypatch.setattr(world.box, "exec", no_rm)
    with pytest.raises(RuntimeError, match="symlink"):
        await world.sanitize(frozenset({"tests/unit/test_hidden.py"}))


async def test_a_real_directory_that_resolves_outside_the_repo_fails_closed(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    real = world.box.exec

    async def elsewhere(argv, **kw):
        res = await real(argv, **kw)
        if argv[0] == "realpath" and argv[-1] == "./tests":
            return type(res)(0, "/elsewhere/tests\n", "", False, 0.0)
        return res

    monkeypatch.setattr(world.box, "exec", elsewhere)
    with pytest.raises(RuntimeError, match="outside the repo"):
        await world.sanitize()


# --- finding 2: __init__.py as seen by find and by pytest --------------------------------------

UNIT = "tests/unit/test_hidden.py"


def pytest_basedir(root: Path, check: str) -> str:
    """The directory pytest (prepend mode) puts on sys.path for `check`, relative to `root`."""
    pkg = resolve_package_path(root / check)
    base = pkg.parent if pkg else (root / check).parent
    return base.relative_to(root).as_posix() or "."


def dangling(root: Path) -> None:
    (root / "tests/__init__.py").symlink_to("nowhere.py")


def to_file(root: Path) -> None:
    (root / "empty.py").write_text("")
    (root / "tests/__init__.py").symlink_to("../empty.py")


def to_directory(root: Path) -> None:
    (root / "tests/__init__.py").symlink_to("unit")


def a_directory(root: Path) -> None:
    (root / "tests/__init__.py").mkdir()
    (root / "tests/__init__.py/x.txt").write_text("x\n")


BREAKS: dict[str, Callable[[Path], None]] = {
    "dangling": dangling,
    "to file": to_file,
    "to directory": to_directory,
    "directory": a_directory,
}


@pytest.mark.parametrize("how", list(BREAKS))
async def test_the_import_roots_hold_the_directory_pytest_really_uses(world: World, how: str):
    world.agent({"tests/unit/__init__.py": "", "tests/unit/wave.py": HARD_EXIT, UNIT: CHECK})
    BREAKS[how](world.root)
    commit(world.root, "agent")
    kept = frozenset({UNIT})
    await world.sanitize(kept)  # (the normalisation stage runs here)
    assert pytest_basedir(world.root, UNIT) in await _import_roots(world.box, kept)


async def test_a_dangling_init_link_hides_the_real_basedir_from_the_scan(world: World):
    # tests/unit is a package, tests/ has a DANGLING __init__.py link: pytest says tests/ is not
    # a package, so `tests/` goes first on sys.path; a find that counted the link scanned `.`
    world.agent(
        {
            "tests/unit/__init__.py": "",
            "tests/unit/test_hidden.py": CHECK,
            "tests/wave.py": HARD_EXIT,
        }
    )
    dangling(world.root)
    commit(world.root, "agent")
    assert pytest_basedir(world.root, UNIT) == "tests"
    await rigged(world, UNIT)

    changed = await world.sanitize(frozenset({UNIT}))
    assert changed == ["tests/__init__.py", "tests/wave.py"]
    assert not (world.root / "tests/__init__.py").is_symlink()
    await fails_honestly(world, UNIT)


async def test_an_init_link_to_a_real_file_is_removed_and_the_roots_agree(world: World):
    world.agent({"tests/unit/__init__.py": "", UNIT: CHECK, "tests/wave.py": HARD_EXIT})
    to_file(world.root)
    commit(world.root, "agent")
    assert pytest_basedir(world.root, UNIT) == "."  # pytest follows the link: a package

    changed = await world.sanitize(frozenset({UNIT}))
    assert changed == ["tests/__init__.py", "tests/wave.py"]
    assert pytest_basedir(world.root, UNIT) == "tests"  # no longer a package
    assert "tests" in await _import_roots(world.box, frozenset({UNIT}))


async def test_a_base_init_replaced_by_a_link_is_restored(world: World):
    world.base = world.agent({"tests/__init__.py": ""})  # the base has an __init__.py
    (world.root / "tests/__init__.py").unlink()
    dangling(world.root)
    commit(world.root, "agent")

    assert await world.sanitize() == ["tests/__init__.py"]
    init = world.root / "tests/__init__.py"
    assert init.is_file() and not init.is_symlink()


async def test_a_non_identifier_package_directory_is_where_pytest_stops(world: World):
    # `my-pkg` has an __init__.py but is no importable name: pytest's package walk stops there and
    # puts `my-pkg` itself on sys.path
    check = "my-pkg/tests/test_hidden.py"
    world.agent(
        {
            "my-pkg/__init__.py": "",
            "my-pkg/tests/__init__.py": "",
            check: CHECK,
            "my-pkg/wave.py": HARD_EXIT,
        }
    )
    assert pytest_basedir(world.root, check) == "my-pkg"
    await rigged(world, check)

    changed = await world.sanitize(frozenset({check}))
    assert changed == ["my-pkg/wave.py"]
    await fails_honestly(world, check)


async def test_the_sanitizers_basedir_is_pytests_for_a_regular_layout(world: World):
    world.agent({"tests/unit/__init__.py": "", "tests/__init__.py": "", UNIT: "", "pkg/x.py": ""})
    kept = frozenset({UNIT})
    assert await sanitize_tree(world.box, world.base, kept) == []
    roots = await _import_roots(world.box, kept)
    assert pytest_basedir(world.root, UNIT) == "."
    assert roots == [".", "src", "tests/unit"]


# --- negative controls -------------------------------------------------------------------------


async def test_an_honest_repo_is_untouched(world: World):
    legit = {
        "helpers.py": HELPER,
        "app/__init__.py": "",
        "app/types.py": HELPER,  # nested: `app.types`, not `types`
        "app/json.py": HELPER,
        "app/tests/__init__.py": "",
        "app/tests/test_old.py": HELPER,
        "tests/__init__.py": "",
        "tests/unit/__init__.py": "",
        "tests/unit/helpers.py": HELPER,
        "docs/v1/index.md": "v1\n",
        "empty.py": "",
    }
    world.agent(legit)
    (world.root / "docs/latest").symlink_to("v1")  # a link away from every import-root path
    (world.root / "docs/v1/current.md").symlink_to("index.md")
    commit(world.root, "links")
    head = git(world.root, "rev-parse", "HEAD")

    kept = frozenset({"tests/unit/test_hidden.py", "app/tests/test_hidden.py"})
    assert await world.sanitize(kept) == []
    assert git(world.root, "rev-parse", "HEAD") == head
    assert git(world.root, "status", "--porcelain") == ""
    assert (world.root / "docs/latest").is_symlink()
    assert (world.root / "docs/v1/current.md").is_symlink()
    for rel, text in legit.items():
        assert (world.root / rel).read_text() == text, rel


async def test_an_honest_repo_with_an_unrelated_link_still_passes_its_check(world: World):
    world.agent({"docs/v1/a.md": "a\n"})
    (world.root / "docs/latest").symlink_to("v1")
    commit(world.root, "agent")
    assert await world.sanitize() == []
    await fails_honestly(world, "tests/test_hidden.py")
