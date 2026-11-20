import asyncio
import subprocess
from pathlib import Path

import pytest

from swfactory.definition.writer import (
    DefinitionWriteError,
    GitHubDefinitionWriter,
    LocalGitDefinitionWriter,
)


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def git_log_1(repo):
    return _git(repo, "log", "-1", "--format=%s")


def git_head(repo):
    return _git(repo, "rev-parse", "HEAD")


def git_author_1(repo):
    return _git(repo, "log", "-1", "--format=%an <%ae>")


def _init(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _git(path, "config", k, v)
    (path / "profiles").mkdir()
    (path / "profiles/x.yaml").write_text("a: 0\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def tmp_git_repo(tmp_path):
    return _init(tmp_path / "def")


@pytest.fixture
def tmp_git_repo_dirty(tmp_git_repo):
    (tmp_git_repo / "profiles/x.yaml").write_text("a: dirty\n")
    return tmp_git_repo


class FakeGitHub:
    pr_url = "https://github.com/org/def/pull/1"

    def __init__(self, clock=None):
        self.calls: list[str] = []
        self.mutation_times: list[float] = []
        self.clock = clock

    def _m(self, name):
        self.calls.append(name)
        if self.clock:
            self.mutation_times.append(self.clock())

    async def get_branch_sha(self, repo, branch):
        return "base"

    async def get_file_sha(self, repo, path, ref):
        return None

    async def create_ref(self, repo, ref, sha):
        self._m("create_ref")

    async def create_or_update_file_contents(self, repo, path, **kw):
        self._m("create_or_update_file_contents")

    async def create_pull(self, repo, **kw):
        self._m("create_pull")
        return self.pr_url


@pytest.fixture
def fake_github():
    gh = FakeGitHub()

    async def factory():
        return gh

    factory.calls = gh.calls  # type: ignore[attr-defined]
    factory.pr_url = gh.pr_url  # type: ignore[attr-defined]
    return factory


def test_personal_save_commits(tmp_git_repo):
    r = asyncio.run(
        LocalGitDefinitionWriter(tmp_git_repo).save_file(
            "profiles/x.yaml", "a: 1\n", message="profile: x", author="local"
        )
    )
    assert (
        r.mode == "commit"
        and git_log_1(tmp_git_repo) == "profile: x"
        and r.sha == git_head(tmp_git_repo)
    )


def test_personal_save_refuses_dirty_file(tmp_git_repo_dirty):
    with pytest.raises(DefinitionWriteError):
        asyncio.run(
            LocalGitDefinitionWriter(tmp_git_repo_dirty).save_file(
                "profiles/x.yaml", "a: 2\n", message="m", author="local"
            )
        )


def test_personal_save_refuses_path_escape(tmp_git_repo):
    with pytest.raises(DefinitionWriteError):
        asyncio.run(
            LocalGitDefinitionWriter(tmp_git_repo).save_file(
                "../x.yaml", "a: 1\n", message="m", author="local"
            )
        )


@pytest.mark.parametrize(
    "path",
    [
        "apps/X.yaml",
        "apps/sub/x.yaml",
        "apps/../factory.yaml",
        "apps/x.yml",
        "factory.yaml",
        "runners/standard.yaml",
        "profiles/../factory.yaml",
        "profiles/sub/x.yaml",
        "profiles/X.yaml",
    ],
)
async def test_writers_only_write_profiles_and_apps(tmp_git_repo, fake_github, path):
    with pytest.raises(DefinitionWriteError):
        await LocalGitDefinitionWriter(tmp_git_repo).save_file(
            path, "a: 1\n", message="m", author="local"
        )
    with pytest.raises(DefinitionWriteError):
        await GitHubDefinitionWriter(fake_github, "org/def", "main").save_file(
            path, "a: 1\n", message="m", author="a"
        )
    assert fake_github.calls == []


async def test_writers_accept_an_app_file(tmp_git_repo, fake_github):
    body = "repo_kind: local\nrepo_url: todo-app\n"
    r = await LocalGitDefinitionWriter(tmp_git_repo).save_file(
        "apps/todo-app.yaml", body, message="app: todo-app", author="local"
    )
    assert r.mode == "commit" and git_log_1(tmp_git_repo) == "app: todo-app"
    assert (tmp_git_repo / "apps/todo-app.yaml").exists()
    r = await GitHubDefinitionWriter(fake_github, "org/def", "main").save_file(
        "apps/todo-app.yaml", body, message="app: todo-app", author="a@x.io"
    )
    assert r.mode == "pr" and fake_github.calls == [
        "create_ref",
        "create_or_update_file_contents",
        "create_pull",
    ]


def test_author_is_sanitized(tmp_git_repo):
    asyncio.run(
        LocalGitDefinitionWriter(tmp_git_repo).save_file(
            "profiles/x.yaml", "a: 1\n", message="m", author="ana <x>\nevil"
        )
    )
    assert git_author_1(tmp_git_repo) == "ana xevil <ana xevil>"


async def test_github_writer_opens_pr(fake_github):
    r = await GitHubDefinitionWriter(fake_github, "org/def", "main").save_file(
        "profiles/x.yaml", "a: 1\n", message="profile: x", author="ana@x.io"
    )
    assert r.mode == "pr" and r.pr_url == fake_github.pr_url
    assert fake_github.calls == ["create_ref", "create_or_update_file_contents", "create_pull"]


async def test_github_writer_serial_and_spaced():
    now = [0.0]

    async def sleep(s):
        now[0] += s

    gh = FakeGitHub(clock=lambda: now[0])

    async def factory():
        return gh

    writer = GitHubDefinitionWriter(factory, "org/def", "main", clock=lambda: now[0], sleep=sleep)
    await asyncio.gather(
        *(
            writer.save_file(f"profiles/{i}.yaml", "a: 1\n", message="m", author="a")
            for i in range(2)
        )
    )
    t = gh.mutation_times
    assert len(t) == 6 and min(b - a for a, b in zip(t, t[1:], strict=False)) >= 1.0


BENCH_FILES = {
    "benchmarks/v1/suite.yaml": "a: 1\n",
    "benchmarks/v1/tasks/003-todo-web.yaml": "b: 1\n",
    "benchmarks/v1/checks/003-todo-web/tests/test_web.py": "x\n",
}


def commits_added(repo) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD")) - 1


async def test_save_files_one_commit(tmp_git_repo):
    r = await LocalGitDefinitionWriter(tmp_git_repo).save_files(
        BENCH_FILES, message="bench: add", author="local"
    )
    assert (r.mode, commits_added(tmp_git_repo)) == ("commit", 1)
    assert (tmp_git_repo / "benchmarks/v1/checks/003-todo-web/tests/test_web.py").exists()


async def test_save_files_one_pr(fake_github):
    r = await GitHubDefinitionWriter(fake_github, "org/def", "main").save_files(
        BENCH_FILES, message="bench: add", author="a"
    )
    assert r.mode == "pr"
    assert fake_github.calls == ["create_ref"] + ["create_or_update_file_contents"] * 3 + [
        "create_pull"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "factory.yaml",
        "runners/standard.yaml",
        "profiles/x.yaml",
        ".github/workflows/x.yml",
        "benchmarks/v1/../../factory.yaml",
        "benchmarks/v1/checks/t/../../../../runners/x.yaml",
        "benchmarks/v1/checks/t/./a.py",
        "/benchmarks/v1/suite.yaml",
        "benchmarks/V1/suite.yaml",
        "benchmarks/v1/tasks/sub/x.yaml",
        "benchmarks/v1/checks/t/.git/config",
    ],
)
async def test_save_files_writes_only_benchmarks(tmp_git_repo, fake_github, path):
    files = {**BENCH_FILES, path: "x\n"}
    with pytest.raises(DefinitionWriteError):
        await LocalGitDefinitionWriter(tmp_git_repo).save_files(files, message="m", author="local")
    with pytest.raises(DefinitionWriteError):
        await GitHubDefinitionWriter(fake_github, "org/def", "main").save_files(
            files, message="m", author="a"
        )
    assert commits_added(tmp_git_repo) == 0 and fake_github.calls == []


async def test_save_file_still_refuses_benchmarks(tmp_git_repo):
    with pytest.raises(DefinitionWriteError):
        await LocalGitDefinitionWriter(tmp_git_repo).save_file(
            "benchmarks/v1/suite.yaml", "a: 1\n", message="m", author="local"
        )
