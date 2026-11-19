import subprocess
from pathlib import Path

import pytest
import yaml

import swfactory.apps as apps_mod
from swfactory.apps import (
    RegisterDeps,
    RegisterError,
    RegistrationPending,
    RepoRef,
    find_registered,
    parse_repo,
    register_repo,
    slug_for,
)
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import AppDef, MergePolicy
from swfactory.definition.writer import DefinitionWriteError, SaveResult
from swfactory.gitops.folder import FolderRepo
from swfactory.settings import FactoryMode, Settings


class FakeWriter:
    def __init__(self, pr=False, lock_once=False):
        self.saved: list[tuple[str, str, str]] = []
        self.pr = pr
        self.lock_once = lock_once
        self.attempts = 0

    async def save_file(self, path, content, *, message, author):
        self.attempts += 1
        if self.lock_once and self.attempts == 1:
            raise DefinitionWriteError("busy")
        self.saved.append((path, content, author))
        if self.pr:
            return SaveResult("pr", None, "https://github.com/org/def/pull/3")
        return SaveResult("commit", "a" * 40, None)


class Env:
    """Holds the in-memory active definition; stands in for load_active and the sync."""

    def __init__(self):
        res = load_definition(str(defaults_dir()))
        assert res.definition is not None
        self.defn = res.definition
        self.sync_calls = 0
        self.events: list[tuple[str, dict]] = []
        self.writer: FakeWriter = FakeWriter()

    async def sync(self, s):
        self.sync_calls += 1
        path, content, _ = self.writer.saved[-1]
        slug = Path(path).stem
        app = AppDef.model_validate(yaml.safe_load(content))
        apps = {**self.defn.factory.apps, slug: app}
        self.defn = self.defn.model_copy(
            update={"factory": self.defn.factory.model_copy(update={"apps": apps})}
        )
        return self.defn


@pytest.fixture
def env(monkeypatch):
    e = Env()

    async def fake_load(s):
        return "sha", e.defn

    async def fake_record(s, job_id, kind, data):
        e.events.append((kind, dict(data)))
        return 1

    monkeypatch.setattr(apps_mod, "load_active", fake_load)
    monkeypatch.setattr(apps_mod, "record_event", fake_record)
    monkeypatch.setattr(apps_mod, "_RETRY_DELAY_S", 0.0)
    return e


def make_deps(env, tmp_path, writer=None, root=True):
    env.writer = writer or FakeWriter()
    assert isinstance(env.writer, FakeWriter)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    settings = Settings(
        FACTORY_MODE=FactoryMode.PERSONAL,
        FACTORY_HOST_REPOS_ROOT=str(work) if root else None,
        FACTORY_HOST_HOME=str(tmp_path),
    )
    folders = FolderRepo(str(work), str(work), bare_dir=tmp_path / "repos") if root else None
    return FakeDeps(env.writer, folders, settings, None, env.sync)


class FakeDeps(RegisterDeps):
    writer: FakeWriter  # type: ignore[assignment]


@pytest.fixture
def deps(env, tmp_path):
    return make_deps(env, tmp_path)


@pytest.fixture
def session():
    return object()


@pytest.fixture
def repo_dir(tmp_path):
    def make(name, under=None):
        d = tmp_path / "work" / (under or "") / name
        d.mkdir(parents=True, exist_ok=True)
        if not (d / ".git").exists():
            ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
            (d / "a.txt").write_text("x")
            subprocess.run(["git", "-C", str(d), "add", "."], check=True)
            subprocess.run(["git", *ident, "-C", str(d), "commit", "-qm", "i"], check=True)
        return d

    return make


@pytest.mark.parametrize(
    "text, ref",
    [
        ("/Users/me/work/app", RepoRef("local", "/Users/me/work/app", None, None)),
        ("~/work/app", RepoRef("local", "/Users/me/work/app", None, None)),
        ("acme/web", RepoRef("github", None, "acme/web", "https://github.com/acme/web")),
        (
            "https://github.com/acme/web.git",
            RepoRef("github", None, "acme/web", "https://github.com/acme/web"),
        ),
        (
            "https://github.com/Acme/Web/",
            RepoRef("github", None, "Acme/Web", "https://github.com/Acme/Web"),
        ),
    ],
)
def test_parse_repo(text, ref):
    assert parse_repo(text, host_home="/Users/me") == ref


@pytest.mark.parametrize(
    "text",
    [
        "",
        "work/app",
        "../app",
        "https://gitlab.com/a/b",
        "acme/web/extra",
        "/Users/me/app\n",
        "~me/app",
        "/Users/me/../root",
    ],
)
def test_parse_repo_refuses(text):
    with pytest.raises(RegisterError):
        parse_repo(text, host_home="/Users/me")


def test_parse_repo_tilde_needs_a_home():
    with pytest.raises(RegisterError, match="FACTORY_HOST_HOME"):
        parse_repo("~/x", host_home=None)


@pytest.mark.parametrize(
    "name, taken, slug",
    [
        ("My_App", (), "my-app"),
        ("todo", ("todo",), "todo-2"),
        ("todo", ("todo", "todo-2"), "todo-3"),
        ("!!!", (), "app"),
        ("a" * 60, (), "a" * 40),
        ("a" * 40, ("a" * 40,), "a" * 38 + "-2"),
    ],
)
def test_slug_for(name, taken, slug):
    assert slug_for(name, taken) == slug


def test_find_registered_matches_github_loosely(env):
    app = AppDef(repo_kind="github", repo_url="https://github.com/Acme/Web.git")
    env.defn = env.defn.model_copy(
        update={
            "factory": env.defn.factory.model_copy(
                update={"apps": {**env.defn.factory.apps, "web": app}}
            )
        }
    )
    ref = parse_repo("acme/web", host_home=None)
    assert find_registered(env.defn, ref) == ("web", app)


async def test_register_folder_writes_the_app_file(deps, env, session, repo_dir):
    folder = repo_dir("My_App")
    app = await register_repo(session, repo=str(folder), requested_by="ana@x.io", deps=deps)
    path, content, author = deps.writer.saved[0]
    assert (path, author) == ("apps/my-app.yaml", "ana@x.io")
    assert yaml.safe_load(content) == {
        "repo_kind": "local",
        "repo_url": "my-app",
        "local_path": str(folder),
        "default_branch": "main",
        "merge_policy": "local_merge",
        "profile": "team-gemini",
        "runner": "standard",
    }
    assert app.local_path == str(folder) and app.merge_policy == MergePolicy.LOCAL_MERGE
    assert (folder.parent.parent / "repos" / "my-app.git").is_dir()
    assert env.events == [
        ("admin.changed", {"what": "app.register", "target": "my-app", "by": "ana@x.io"})
    ]


async def test_second_run_reuses_the_registration(deps, session, repo_dir):
    first = await register_repo(session, repo=str(repo_dir("app")), requested_by="a", deps=deps)
    again = await register_repo(session, repo=str(repo_dir("app")), requested_by="b", deps=deps)
    assert again == first and len(deps.writer.saved) == 1


async def test_slug_clash_gets_a_suffix(deps, session, repo_dir):
    await register_repo(
        session, repo=str(repo_dir("app", under="other")), requested_by="a", deps=deps
    )
    await register_repo(session, repo=str(repo_dir("app")), requested_by="a", deps=deps)
    assert [p for p, _, _ in deps.writer.saved] == ["apps/app.yaml", "apps/app-2.yaml"]


async def test_folder_outside_the_root_is_refused(deps, session, tmp_path):
    with pytest.raises(RegisterError, match="not inside FACTORY_HOST_REPOS_ROOT"):
        await register_repo(session, repo=str(tmp_path / "elsewhere"), requested_by="a", deps=deps)
    assert deps.writer.saved == []


async def test_folders_need_the_root_setting(env, tmp_path, session):
    deps_no_root = make_deps(env, tmp_path, root=False)
    with pytest.raises(RegisterError, match="FACTORY_HOST_REPOS_ROOT"):
        await register_repo(session, repo="/Users/me/work/app", requested_by="a", deps=deps_no_root)


async def test_not_a_repo_is_refused(deps, session, tmp_path):
    d = tmp_path / "work" / "docs"
    d.mkdir()
    (d / "a.txt").write_text("x")
    with pytest.raises(RegisterError, match="not a git repository"):
        await register_repo(session, repo=str(d), requested_by="a", deps=deps)


async def test_ci_safe_is_for_github_only(deps, session, repo_dir):
    with pytest.raises(RegisterError, match="--ci-safe applies to GitHub repos"):
        await register_repo(
            session, repo=str(repo_dir("app")), ci_safe=True, requested_by="a", deps=deps
        )


async def test_github_repos_need_the_github_app(deps, session):
    with pytest.raises(RegisterError, match="GitHub repos need the GitHub App"):
        await register_repo(session, repo="acme/web", requested_by="a", deps=deps)


async def test_team_instance_opens_a_pr_and_waits(env, tmp_path, session, repo_dir):
    deps = make_deps(env, tmp_path, FakeWriter(pr=True))
    with pytest.raises(RegistrationPending) as ei:
        await register_repo(session, repo=str(repo_dir("app")), requested_by="a", deps=deps)
    assert (ei.value.slug, ei.value.pr_url) == ("app", "https://github.com/org/def/pull/3")
    assert env.sync_calls == 0


async def test_a_busy_definition_repo_is_retried(env, tmp_path, session, repo_dir):
    deps = make_deps(env, tmp_path, FakeWriter(lock_once=True))
    await register_repo(session, repo=str(repo_dir("app")), requested_by="a", deps=deps)
    assert len(deps.writer.saved) == 1 and deps.writer.attempts == 2


# -- GitHub repos (M8.7b) ----------------------------------------------------------------


@pytest.fixture
def fake_gh():
    from tests.fakes.github_api import FakeGhApi, mock_github

    api = FakeGhApi()
    with mock_github(api):
        yield api


@pytest.fixture
def deps_gh(env, tmp_path, fake_gh):
    from tests.fakes.github_api import StubAuth

    from swfactory.apps import GitHubRepoInspector

    d = make_deps(env, tmp_path)
    d.inspector = GitHubRepoInspector(StubAuth())
    return d


async def test_github_repo_defaults(deps_gh, session):
    app = await register_repo(session, repo="acme/web", requested_by="ana@x.io", deps=deps_gh)
    path, content, _ = deps_gh.writer.saved[0]
    assert path == "apps/web.yaml"
    assert yaml.safe_load(content) == {
        "repo_kind": "github",
        "repo_url": "https://github.com/acme/web",
        "default_branch": "trunk",
        "merge_policy": "pr_auto_merge",
        "profile": "team-gemini",
        "runner": "standard",
        "github": {"ci_safe": False},
    }
    assert app.merge_policy == MergePolicy.PR_AUTO_MERGE


async def test_ci_safe_flag_is_recorded(deps_gh, session):
    await register_repo(
        session, repo="https://github.com/acme/web", ci_safe=True, requested_by="a", deps=deps_gh
    )
    assert yaml.safe_load(deps_gh.writer.saved[0][1])["github"] == {"ci_safe": True}


async def test_default_registration_waits_for_approval_before_any_push(deps_gh, session):
    from swfactory.domain.types import JobKind
    from swfactory.queue import ci_requires_approval

    app = await register_repo(session, repo="acme/web", requested_by="a", deps=deps_gh)
    assert ci_requires_approval(app, JobKind.NORMAL) is True


async def test_unreachable_repo_is_refused(deps_gh, fake_gh, session):
    fake_gh.installed = False
    with pytest.raises(RegisterError, match="add acme/web to the app installation"):
        await register_repo(session, repo="acme/web", requested_by="a", deps=deps_gh)
    assert deps_gh.writer.saved == []


async def test_empty_github_repo_is_refused(deps_gh, fake_gh, session):
    fake_gh.empty = True
    with pytest.raises(
        RegisterError, match="acme/web has no commits; create the first commit and run again"
    ):
        await register_repo(session, repo="acme/web", requested_by="a", deps=deps_gh)
    assert deps_gh.writer.saved == []


async def test_second_run_matches_the_url_ignoring_case(deps_gh, session):
    first = await register_repo(session, repo="acme/web", requested_by="a", deps=deps_gh)
    again = await register_repo(
        session, repo="https://github.com/ACME/Web.git", requested_by="b", deps=deps_gh
    )
    assert again == first and len(deps_gh.writer.saved) == 1
