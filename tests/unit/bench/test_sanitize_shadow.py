"""The hidden checks' sanitizer against import shadowing, with real git and real python processes.

`python -m pytest` puts the repo root, and the directory of each test file, at the FRONT of
``sys.path``. A committed top-level ``pluggy.py`` (or ``re.py``, a ``json/`` package, a compiled
``.so``...) is then imported in place of the real module. These tests commit such files, show the
attack makes a failing hidden check exit 0 where the interpreter lets it, and show the sanitizer
takes it away. They also pin what must NOT be touched: ordinary job files and nested modules.
"""

from __future__ import annotations

import py_compile
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from tests.fakes.host_sandbox import HostSandbox

from swfactory.bench.sanitize import sanitize_tree
from swfactory.sandbox.base import ExecResult
from swfactory.sandbox.paths import REPO

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]
EXIT_ZERO = "import sys\nsys.exit(0)\n"
HARD_EXIT = "import os\nos._exit(0)\n"  # pytest turns a SystemExit during collection into an error
APP = "def greet():\n    return 'hello'\n"  # the feature (shout) is not implemented
HELPER = "VALUE = 1\n"
HIDDEN = "import src.app as app\n\n\ndef test_feature():\n    assert app.shout('hi') == 'HI'\n"
CHECKS = {"tests/test_hidden.py": HIDDEN}
PYTEST_CMD = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "tests/test_hidden.py",
]
# Names an installed environment of the checks box owns although this machine may not have them
FAKE_INSTALLED = ("colorama", "exceptiongroup")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def put(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A", "-f")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


class PythonBox(HostSandbox):
    """A host box where `python3` is the interpreter running these tests."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[tuple[list[str], str]] = []

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        argv = [sys.executable if argv[0] == "python3" else argv[0], *argv[1:]]
        self.calls.append((list(argv), cwd))
        return await super().exec(argv, cwd=cwd, env=env, timeout_s=timeout_s, stdin=stdin)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.box = PythonBox(tmp_path / "box")
        self.root = tmp_path / "box" / "work" / "repo"
        self.root.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "main")
        put(
            self.root,
            {
                "src/__init__.py": "",
                "src/app.py": APP,
                "tests/conftest.py": "import pytest\n",
                "tests/helpers.py": HELPER,
            },
        )
        self.base = commit(self.root, "base")

    def agent(self, files: Mapping[str, str]) -> str:
        put(self.root, files)
        return commit(self.root, "agent")

    def venv(self, own: Mapping[str, str] | None = None) -> Path:
        """The repo's `.venv` (untracked, as set up by the platform) with a fake install in it.

        `own` maps a top-level name to the project: such a distribution is installed from the
        repo itself (a `direct_url.json` pointing here), like `uv sync` does for the project.
        """
        venv = self.root / ".venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        site = next((venv / "lib").glob("python*/site-packages"))
        for name in FAKE_INSTALLED:
            info = site / f"{name}-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
            (info / "top_level.txt").write_text(name + "\n")
            (site / name).mkdir()
            (site / name / "__init__.py").write_text("")
        for top, direct in (own or {}).items():
            info = site / f"proj_{top}-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: proj-{top}\nVersion: 1.0\n"
            )
            (info / "top_level.txt").write_text(top + "\n")
            if direct:
                (info / "direct_url.json").write_text(direct.replace("REPO", self.root.as_uri()))
        return site

    def write_checks(self) -> None:
        put(self.root, CHECKS)

    async def run(self, argv: list[str]) -> tuple[int, str]:
        res = await self.box.exec(argv, cwd=REPO, timeout_s=120)
        return res.exit_code, res.stdout + res.stderr

    async def sanitize(self, keep: frozenset[str] = frozenset(CHECKS)) -> list[str]:
        return await sanitize_tree(self.box, self.base, keep)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# attack files; each one alone lets the failing hidden check exit 0 when it is imported in place
# of the real module (the sys.exit(0) runs at import, the way a stub would crash into a pass)
EDITABLE = '{"url": "REPO", "dir_info": {"editable": true}}'
# name -> (files the agent commits, the paths the sanitizer reports)
ROOT_ATTACKS: dict[str, tuple[dict[str, str], list[str]]] = {
    "pluggy.py": ({"pluggy.py": EXIT_ZERO}, ["pluggy.py"]),
    "iniconfig.py": ({"iniconfig.py": EXIT_ZERO}, ["iniconfig.py"]),
    "packaging.py": ({"packaging.py": EXIT_ZERO}, ["packaging.py"]),
    "pluggy package": ({"pluggy/__init__.py": EXIT_ZERO}, ["pluggy"]),
    "namespace dir": ({"pluggy/nothing.txt": "x\n"}, ["pluggy"]),
    "re.py": ({"re.py": EXIT_ZERO}, ["re.py"]),
    "json.py": ({"json.py": EXIT_ZERO}, ["json.py"]),
    "typing_extensions.py": ({"typing_extensions.py": EXIT_ZERO}, ["typing_extensions.py"]),
    "colorama.py": ({"colorama.py": EXIT_ZERO}, ["colorama.py"]),
    "exceptiongroup.py": ({"exceptiongroup.py": EXIT_ZERO}, ["exceptiongroup.py"]),
    "tests/pluggy.py": ({"tests/pluggy.py": EXIT_ZERO}, ["tests/pluggy.py"]),
    "tests/pluggy package": ({"tests/pluggy/__init__.py": EXIT_ZERO}, ["tests/pluggy"]),
    "tests/re.py": ({"tests/re.py": EXIT_ZERO}, ["tests/re.py"]),
    "src/pluggy.py": ({"src/pluggy.py": EXIT_ZERO}, ["src/pluggy.py"]),
}


@pytest.mark.parametrize("name", list(ROOT_ATTACKS))
async def test_shadowing_module_is_taken_away_and_the_check_fails_honestly(world: World, name: str):
    files, expected = ROOT_ATTACKS[name]
    world.agent(files)
    world.venv()
    world.write_checks()

    assert await world.sanitize() == expected
    for rel in expected:
        assert not (world.root / rel).exists()
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "shout" in out, out  # fails for the right reason: the feature is missing


# Where the interpreter really lets the shadow decide the outcome (checked on the unsanitized
# tree). Some others (typing_extensions, colorama on linux) are simply never imported by pytest
# itself; for those the removal is what the test above asserts.
FEASIBLE = ["pluggy.py", "iniconfig.py", "packaging.py", "pluggy package"]


@pytest.mark.parametrize("name", FEASIBLE)
async def test_the_shadow_really_rigs_the_unsanitized_tree(world: World, name: str):
    world.agent(ROOT_ATTACKS[name][0])
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 0, f"the attack should work on the unsanitized tree:\n{out}"

    await world.sanitize()
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "shout" in out, out


@pytest.mark.parametrize("where", ["wave.py", "tests/wave.py", "wave/__init__.py"])
async def test_a_shadow_of_a_module_the_check_itself_imports_rigs_it(world: World, where: str):
    # pytest has imported its own modules before it puts the test directory first on sys.path,
    # but the hidden check's own imports come after that
    check = "import wave\n" + HIDDEN
    world.agent({where: HARD_EXIT})
    put(world.root, {"tests/test_hidden.py": check})
    code, out = await world.run(PYTEST_CMD)
    assert code == 0, f"the attack should work on the unsanitized tree:\n{out}"

    changed = await world.sanitize()
    assert changed == [where.split("/__init__")[0]]
    put(world.root, {"tests/test_hidden.py": check})
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "shout" in out, out


async def test_a_stdlib_shadow_never_lets_the_check_pass(world: World):
    world.agent({"re.py": EXIT_ZERO, "json.py": EXIT_ZERO, "types.py": EXIT_ZERO})
    world.write_checks()
    broken, _ = await world.run(PYTEST_CMD)  # whatever it does, it is not an honest failure

    assert await world.sanitize() == ["json.py", "re.py", "types.py"]
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "shout" in out, (broken, out)


async def test_sourceless_and_compiled_shadows_are_removed(world: World):
    (world.root / "stage.py").write_text(EXIT_ZERO)
    py_compile.compile(str(world.root / "stage.py"), cfile=str(world.root / "pluggy.pyc"))
    (world.root / "stage.py").unlink()
    world.agent(
        {
            "iniconfig.cpython-312-x86_64-linux-gnu.so": "\x7fELF",
            "packaging.abi3.so": "\x7fELF",
            "colorama.pyd": "MZ",
            "tests/pluggy.cpython-312-darwin.so": "x",
        }
    )
    world.venv()
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 0, out  # the sourceless module rigs it

    changed = await world.sanitize()
    assert changed == [
        "colorama.pyd",
        "iniconfig.cpython-312-x86_64-linux-gnu.so",
        "packaging.abi3.so",
        "pluggy.pyc",
        "tests/pluggy.cpython-312-darwin.so",
    ]
    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "shout" in out, out


async def test_a_link_named_like_a_module_is_removed_without_writing_through(world: World):
    outside = world.root.parent / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("keep\n")
    (world.root / "pluggy").symlink_to(outside)
    (world.root / "json.py").symlink_to(outside / "marker.txt")
    commit(world.root, "agent")
    assert await world.sanitize() == ["json.py", "pluggy"]
    assert not (world.root / "pluggy").is_symlink() and not (world.root / "json.py").is_symlink()
    assert (outside / "marker.txt").read_text() == "keep\n"


async def test_ordinary_job_files_are_left_alone(world: World):
    legit = {
        "helpers.py": HELPER,
        "utils.py": HELPER,
        "mymodule.py": HELPER,
        "fixtures/__init__.py": "",
        "fixtures/data.py": HELPER,
        "test/test_old.py": HELPER,  # `test` and `tests`/`docs`... are far too common to protect
        "docs/conf.py": HELPER,
        "src/app/__init__.py": "",
        "src/app/types.py": HELPER,  # nested: not importable as the top-level `types`
        "src/app/json.py": HELPER,
        "tests/helpers.py": "VALUE = 2\n",
        "tests/sub/json.py": HELPER,  # below the test file's directory
        "docs/re.py": HELPER,
        "pluggy.md": "notes\n",  # same stem, not importable
        "pluggy_helpers.py": HELPER,
        "README.md": "hi\n",
    }
    world.agent(legit)
    world.venv()
    head = git(world.root, "rev-parse", "HEAD")
    assert await world.sanitize() == []
    assert git(world.root, "rev-parse", "HEAD") == head
    for rel, text in legit.items():
        assert (world.root / rel).read_text() == text, rel


async def test_a_legit_module_named_like_an_installed_one_is_removed_and_recorded(world: World):
    # the trade-off: it would shadow the real module anyway, and the trial is not failed for it
    world.agent({"packaging.py": "def mine():\n    return 1\n", "helpers.py": HELPER})
    assert await world.sanitize() == ["packaging.py"]
    assert (world.root / "helpers.py").exists()


async def test_files_identical_to_the_base_are_not_reported_but_edits_are_undone(world: World):
    put(world.root, {"json.py": "A = 1\n", "iniconfig/__init__.py": "A = 1\n"})
    world.base = commit(world.root, "base with legit modules")
    world.agent({"src/more.py": HELPER})
    assert await world.sanitize() == []

    world.agent({"json.py": EXIT_ZERO, "iniconfig/__init__.py": "A = 2\n", "iniconfig/x.py": ""})
    assert await world.sanitize() == ["iniconfig", "json.py"]
    assert (world.root / "json.py").read_text() == "A = 1\n"
    assert (world.root / "iniconfig/__init__.py").read_text() == "A = 1\n"
    assert not (world.root / "iniconfig/x.py").exists()
    assert (world.root / "src/more.py").exists()


async def test_keep_paths_are_never_touched(world: World):
    world.agent({"tests/pluggy.py": "# the task's own\n", "pluggy.py": EXIT_ZERO})
    changed = await world.sanitize(frozenset({"tests/pluggy.py", "tests/test_hidden.py"}))
    assert changed == ["pluggy.py"]
    assert (world.root / "tests/pluggy.py").read_text() == "# the task's own\n"


async def test_a_directory_holding_keep_files_keeps_them_and_loses_the_rest(world: World):
    world.agent({"packaging/check_it.py": "# check\n", "packaging/evil.py": EXIT_ZERO})
    changed = await world.sanitize(frozenset({"packaging/check_it.py"}))
    assert changed == ["packaging"]
    assert (world.root / "packaging/check_it.py").exists()
    assert not (world.root / "packaging/evil.py").exists()


async def test_import_roots_follow_the_check_files(world: World):
    # `python path/to/check.py` puts the script's own directory first on sys.path; a package's
    # test directory is imported from the first directory above it without an __init__.py
    world.agent(
        {
            "pkg/__init__.py": "",
            "pkg/tests/__init__.py": "",
            "pkg/tests/test_x.py": "",
            "pkg/tests/pluggy.py": EXIT_ZERO,  # the check file's own directory
            "pkg/json.py": HELPER,  # inside the package: pkg.json, not `json`
            "pluggy.py": EXIT_ZERO,  # the first directory above without __init__.py
            "other/pluggy.py": EXIT_ZERO,  # not on the way to a check file
            "deep/er/pluggy.py": EXIT_ZERO,
            "deep/er/conftest.py": "",  # a conftest outside the checks' own path
            "x/conftest.py": "",
        }
    )
    changed = await world.sanitize(frozenset({"pkg/tests/test_x.py"}))
    assert changed == ["deep/er/conftest.py", "pkg/tests/pluggy.py", "pluggy.py", "x/conftest.py"]
    assert (world.root / "other/pluggy.py").exists() and (world.root / "pkg/json.py").exists()
    assert (world.root / "deep/er/pluggy.py").exists()


async def test_a_conftest_directory_on_the_way_to_a_check_is_an_import_root(world: World):
    # the base's own tests/conftest.py stays; pytest then puts `tests/` (no __init__.py) first
    world.agent({"tests/pluggy.py": EXIT_ZERO, "tests/unit/pluggy.py": EXIT_ZERO})
    changed = await world.sanitize(frozenset({"tests/unit/test_hidden.py"}))
    assert changed == ["tests/pluggy.py", "tests/unit/pluggy.py"]


async def test_the_projects_own_distribution_is_not_a_protected_name(world: World):
    # `uv sync` installs the project itself from the repo; its code is the agent's work
    put(world.root, {"mylib/__init__.py": "V = 1\n", "mylib/core.py": "V = 1\n"})
    world.base = commit(world.root, "base with the project")
    world.agent({"mylib/core.py": "V = 2\n", "mylib/new.py": "V = 3\n"})
    world.venv(own={"mylib": EDITABLE})
    assert await world.sanitize() == []
    assert (world.root / "mylib/core.py").read_text() == "V = 2\n"
    assert (world.root / "mylib/new.py").exists()


async def test_a_foreign_distribution_with_the_same_name_is_protected(world: World):
    put(world.root, {"mylib/__init__.py": "V = 1\n"})
    world.base = commit(world.root, "base")
    world.agent({"mylib/__init__.py": "V = 2\n", "mylib/new.py": "V = 3\n"})
    world.venv(own={"mylib": ""})  # installed, but not from this repo
    assert await world.sanitize() == ["mylib"]
    assert (world.root / "mylib/__init__.py").read_text() == "V = 1\n"
    assert not (world.root / "mylib/new.py").exists()


async def test_discovery_runs_isolated_outside_the_repo(world: World):
    # a committed importlib.py / re.py / sitecustomize.py must not be able to touch the discovery
    world.agent(
        {
            "importlib.py": "raise SystemExit(3)\n",
            "re.py": "raise SystemExit(3)\n",
            "sitecustomize.py": "raise SystemExit(3)\n",
        }
    )
    world.venv()
    assert await world.sanitize() == ["importlib.py", "re.py", "sitecustomize.py"]
    found = [(a, cwd) for a, cwd in world.box.calls if "-c" in a and "-I" in a]
    assert len(found) == 2  # the box's python3 and the repo's .venv/bin/python
    for argv, cwd in found:
        assert cwd == "/" and "-S" not in argv
        assert argv[1] == "-I"
    assert found[1][0][0].endswith("/.venv/bin/python")


async def test_without_a_venv_only_the_box_python_is_asked(world: World):
    assert await world.sanitize() == []
    found = [a for a, _ in world.box.calls if "-c" in a and "-I" in a]
    assert len(found) == 1


async def test_a_committed_venv_is_never_executed(world: World):
    world.agent({".venv/bin/python": "#!/bin/sh\nexit 0\n", "src/ok.py": ""})
    (world.root / ".venv/bin/python").chmod(0o755)
    with pytest.raises(RuntimeError, match="venv"):
        await world.sanitize()
    assert not any("/.venv/bin/python" in a[0] for a, _ in world.box.calls)


async def test_discovery_failure_fails_closed(world: World, monkeypatch: pytest.MonkeyPatch):
    real = world.box.exec

    async def broken(argv, **kw):
        if "-I" in argv:
            return ExecResult(1, "", "no python here", False, 0.0)
        return await real(argv, **kw)

    monkeypatch.setattr(world.box, "exec", broken)
    with pytest.raises(RuntimeError, match="protected|discover|python"):
        await world.sanitize()


async def test_garbled_discovery_output_fails_closed(world: World, monkeypatch: pytest.MonkeyPatch):
    real = world.box.exec

    async def garbled(argv, **kw):
        if "-I" in argv:
            return ExecResult(0, "not json", "", False, 0.0)
        return await real(argv, **kw)

    monkeypatch.setattr(world.box, "exec", garbled)
    with pytest.raises(RuntimeError):
        await world.sanitize()


async def test_a_check_path_outside_the_repo_fails_closed(world: World):
    with pytest.raises(RuntimeError):
        await world.sanitize(frozenset({"../outside/test_x.py"}))
