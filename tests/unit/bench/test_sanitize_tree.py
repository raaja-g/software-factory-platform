"""The hidden checks' tree sanitizer, against a real git repository and real python processes.

The agent under test commits whatever it likes. These tests build such a commit (every takeover
file the sanitizer knows of), show the attack really does make a failing hidden check pass on the
unsanitized tree, and show the sanitizer takes it away again.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tests.fakes.host_sandbox import HostSandbox

from swfactory.bench.sanitize import sanitize_tree
from swfactory.sandbox.paths import REPO

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]
LEGIT_CONFTEST = (
    "import pytest\n\n\n@pytest.fixture\ndef word():\n    return 'hi'\n"  # the tests' own fixture
)
PASS_EVERYTHING = (
    "import pytest\n\n\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    report = outcome.get_result()\n"
    "    report.outcome = 'passed'\n"
    "    report.longrepr = None\n"
)
EXIT_ZERO = "import sys\nsys.exit(0)\n"
APP = "def greet():\n    return 'hello'\n"  # the feature (shout) is not implemented
HELPER = "VALUE = 1\n"
HIDDEN_PYTEST = (
    "import src.app as app\n\n\ndef test_feature(word):\n    assert app.shout(word) == 'HI'\n"
)
HIDDEN_UNITTEST = (
    "import unittest\nimport src.app as app\n\n\nclass T(unittest.TestCase):\n"
    "    def test_feature(self):\n        self.assertEqual(app.shout('hi'), 'HI')\n"
)
PYTEST_CMD = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "tests/test_hidden.py",
]
UNITTEST_CMD = [sys.executable, "-m", "unittest", "-q", "tests/test_hidden_ut.py"]
CHECKS = {"tests/test_hidden.py": HIDDEN_PYTEST, "tests/test_hidden_ut.py": HIDDEN_UNITTEST}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def put(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A", "-f")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


class World:
    """A box whose /work/repo is the agent's commit; `base` is the task's base commit."""

    def __init__(self, tmp_path: Path) -> None:
        self.box = HostSandbox(tmp_path / "box")
        self.root = tmp_path / "box" / "work" / "repo"
        self.root.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "main")
        put(
            self.root,
            {
                "src/__init__.py": "",
                "src/app.py": APP,
                "src/helper.py": HELPER,
                "tests/conftest.py": LEGIT_CONFTEST,
                "tests/helpers.py": HELPER,
            },
        )
        self.base = commit(self.root, "base")

    def agent(self, files: dict[str, str]) -> str:
        put(self.root, files)
        return commit(self.root, "agent")

    def write_checks(self) -> None:
        put(self.root, CHECKS)

    async def run(self, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        res = await self.box.exec(argv, cwd=REPO, env=env, timeout_s=120)
        return res.exit_code, res.stdout + res.stderr

    async def sanitize(self, keep: frozenset[str] = frozenset(CHECKS)) -> list[str]:
        return await sanitize_tree(self.box, self.base, keep)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


ALL_ATTACKS = {
    "conftest.py": PASS_EVERYTHING,
    "a/b/conftest.py": PASS_EVERYTHING,
    "sitecustomize.py": "import atexit, os\natexit.register(os._exit, 0)\n",
    "usercustomize.py": EXIT_ZERO,
    "evil.pth": "import sys\n",
    "pytest.py": EXIT_ZERO,
    "unittest.py": EXIT_ZERO,
    "pytest.ini": "[pytest]\naddopts = --collect-only\n",
    ".pytest.ini": "[pytest]\naddopts = --collect-only\n",
    "tox.ini": "[pytest]\naddopts = --collect-only\n",
    "tests/conftest.py": LEGIT_CONFTEST + "\n\n" + PASS_EVERYTHING,  # edits the legit one
}

# Each of these alone lets a failing hidden check pass: (files, command, env)
VECTORS = {
    "root conftest": ({"conftest.py": PASS_EVERYTHING}, PYTEST_CMD, None),
    "edited legit conftest": (
        {"tests/conftest.py": LEGIT_CONFTEST + "\n\n" + PASS_EVERYTHING},
        PYTEST_CMD,
        None,
    ),
    "pytest.ini": ({"pytest.ini": "[pytest]\naddopts = --collect-only\n"}, PYTEST_CMD, None),
    ".pytest.ini": ({".pytest.ini": "[pytest]\naddopts = --collect-only\n"}, PYTEST_CMD, None),
    "tox.ini": ({"tox.ini": "[pytest]\naddopts = --collect-only\n"}, PYTEST_CMD, None),
    "pytest.py shadow": ({"pytest.py": EXIT_ZERO}, PYTEST_CMD, None),
    "pytest package shadow": (
        {"pytest/__init__.py": "", "pytest/__main__.py": EXIT_ZERO},
        PYTEST_CMD,
        None,
    ),
    "unittest.py shadow": ({"unittest.py": EXIT_ZERO}, UNITTEST_CMD, None),
    "sitecustomize": (
        {"sitecustomize.py": "import atexit, os\natexit.register(os._exit, 0)\n"},
        PYTEST_CMD,
        {"PYTHONPATH": "."},
    ),
}


@pytest.mark.parametrize("name", list(VECTORS))
async def test_each_takeover_makes_a_failing_check_pass_until_sanitized(world: World, name: str):
    files, cmd, env = VECTORS[name]
    world.agent(files)
    world.write_checks()
    code, out = await world.run(cmd, env)
    assert code == 0, f"the attack should work on the unsanitized tree:\n{out}"

    changed = await world.sanitize()
    world.write_checks()
    assert changed  # something was taken away
    code, out = await world.run(cmd, env)
    assert code != 0, f"the check must fail on the sanitized tree:\n{out}"
    assert "shout" in out  # it failed for the right reason: the feature is not there


async def test_sanitizer_removes_every_takeover_and_keeps_the_rest(world: World):
    put(world.root, {"extra/conftest.py": "# the task's own\n"})  # a keep path, even if tracked
    world.agent(
        {**ALL_ATTACKS, "extra/conftest.py": "# the task's own\n", "src/mine.py": "X = 2\n"}
    )
    world.write_checks()
    code, _ = await world.run(PYTEST_CMD)
    assert code == 0  # the attack works on the old code

    changed = await world.sanitize(frozenset({*CHECKS, "extra/conftest.py"}))

    assert changed == sorted(
        [
            ".pytest.ini",
            "a/b/conftest.py",
            "conftest.py",
            "evil.pth",
            "pytest.ini",
            "pytest.py",
            "sitecustomize.py",
            "tests/conftest.py",
            "tox.ini",
            "unittest.py",
            "usercustomize.py",
        ]
    )
    root = world.root
    for gone in changed:
        if gone != "tests/conftest.py":
            assert not (root / gone).exists(), gone
    assert (root / "tests/conftest.py").read_text() == LEGIT_CONFTEST  # restored from the base
    assert (root / "src/helper.py").read_text() == HELPER  # untouched
    assert (root / "src/mine.py").read_text() == "X = 2\n"
    assert (root / "tests/helpers.py").read_text() == HELPER
    assert (root / "extra/conftest.py").read_text() == "# the task's own\n"  # the keep path
    assert (root / "tests/test_hidden.py").read_text() == HIDDEN_PYTEST

    world.write_checks()
    code, out = await world.run(PYTEST_CMD)
    assert code == 1 and "AttributeError" in out  # fails: the hidden check is honest again
    code, out = await world.run(UNITTEST_CMD)
    assert code == 1 and "shout" in out


async def test_clean_tree_changes_nothing(world: World):
    world.agent({"src/feature.py": "Y = 1\n"})
    head = git(world.root, "rev-parse", "HEAD")
    assert await world.sanitize() == []
    assert git(world.root, "status", "--porcelain") == ""
    assert git(world.root, "rev-parse", "HEAD") == head


async def test_keep_paths_are_never_touched_even_when_denylisted(world: World):
    world.agent({"checks/conftest.py": "# platform\n", "conftest.py": PASS_EVERYTHING})
    changed = await world.sanitize(frozenset({"checks/conftest.py"}))
    assert changed == ["conftest.py"]
    assert (world.root / "checks/conftest.py").read_text() == "# platform\n"


async def test_restores_a_denylisted_directory_and_deletes_its_extras(world: World):
    put(world.root, {"vendor/unittest/real.py": "REAL = 1\n"})
    world.base = commit(world.root, "base with vendored unittest")
    world.agent(
        {"vendor/unittest/real.py": "REAL = 666\n", "vendor/unittest/__init__.py": EXIT_ZERO}
    )
    changed = await world.sanitize()
    assert changed == ["vendor/unittest"]
    assert (world.root / "vendor/unittest/real.py").read_text() == "REAL = 1\n"
    assert not (world.root / "vendor/unittest/__init__.py").exists()


async def test_a_directory_absent_from_the_base_is_removed_whole(world: World):
    world.agent({"_pytest/__init__.py": "", "_pytest/deep/x.py": "", "src/ok.py": ""})
    assert await world.sanitize() == ["_pytest"]
    assert not (world.root / "_pytest").exists() and (world.root / "src/ok.py").exists()


async def test_symlinks_are_replaced_by_the_base_or_removed(world: World):
    outside = world.root.parent / "outside.py"
    outside.write_text(PASS_EVERYTHING)
    (world.root / "tests/conftest.py").unlink()
    (world.root / "tests/conftest.py").symlink_to(outside)
    (world.root / "sitecustomize.py").symlink_to(outside)
    (world.root / "pytest").symlink_to(world.root / "src")  # importable as a package
    commit(world.root, "agent")
    assert await world.sanitize() == ["pytest", "sitecustomize.py", "tests/conftest.py"]
    conftest = world.root / "tests/conftest.py"
    assert not conftest.is_symlink() and conftest.read_text() == LEGIT_CONFTEST
    assert not (world.root / "sitecustomize.py").is_symlink()
    assert not (world.root / "sitecustomize.py").exists() and not (world.root / "pytest").exists()
    assert outside.read_text() == PASS_EVERYTHING  # the link target is not written through


async def test_odd_file_names_are_not_interpreted(world: World):
    world.agent(
        {
            "sp ace/conftest.py": "",
            "-dash/conftest.py": "",
            "q'uote/sitecustomize.py": "",
            "nl\nname/pytest.ini": "",
            "[br]/conftest.py": "",
        }
    )
    changed = await world.sanitize()
    assert changed == sorted(
        [
            "-dash/conftest.py",
            "[br]/conftest.py",
            "nl\nname/pytest.ini",
            "q'uote/sitecustomize.py",
            "sp ace/conftest.py",
        ]
    )
    assert not any((world.root / p).exists() for p in changed)


async def test_installed_environment_is_left_alone_but_committed_ones_are_not(world: World):
    world.agent({"node_modules/evil/sitecustomize.py": EXIT_ZERO})  # committed: a takeover
    env = world.root / ".venv/lib/python3.12/site-packages"
    (env / "_pytest").mkdir(parents=True)
    (env / "_pytest/x.py").write_text("")
    (env / "__editable__.app.pth").write_text("/work/repo/src\n")
    (env / "conftest.py").write_text("")
    changed = await world.sanitize()
    assert changed == ["node_modules/evil/sitecustomize.py"]
    assert (env / "_pytest/x.py").exists() and (env / "__editable__.app.pth").exists()
    assert (env / "conftest.py").exists()


async def test_git_dir_is_never_scanned(world: World):
    (world.root / ".git/info").mkdir(exist_ok=True)
    (world.root / ".git/info/conftest.py").write_text("")
    assert await world.sanitize() == []


async def test_unknown_base_fails_closed(world: World):
    world.agent({"conftest.py": PASS_EVERYTHING})
    with pytest.raises(RuntimeError):
        await sanitize_tree(world.box, "f" * 40, frozenset())
