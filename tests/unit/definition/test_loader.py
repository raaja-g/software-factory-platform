import subprocess
from pathlib import Path

import yaml

from swfactory.definition import loader
from swfactory.definition.loader import (
    MAX_DEFINITION_FILE_BYTES,
    init_definition_repo,
    load_definition,
)
from swfactory.domain.types import FactoryMode, MergePolicy
from swfactory.settings import get_settings

IMAGE_LINE = 1


def _git(d: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def set_yaml(repo: Path, rel: str, keys: tuple[str, ...], value) -> None:
    p = repo / rel
    data = yaml.safe_load(p.read_text())
    node = data
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    p.write_text(yaml.safe_dump(data, sort_keys=False))


def break_merge_policy(d: Path) -> Path:
    """Init a repo whose factory.yaml has the typo `merge_polcy: x` on line 14."""
    init_definition_repo(d)
    text = (d / "factory.yaml").read_text()
    before_apps, after = text.split("\napps:\n")
    rest = after.split("\ntriage:\n", 1)[1]
    first_two = before_apps.splitlines()[:2]
    # lines 1-2 header, 3-9 padding, 10 apps:, 11 demo:, 12 repo_kind, 13 repo_url, 14 typo
    body = "\n".join(first_two) + "\n" + "\n".join(f"# pad {i}" for i in range(7)) + "\n"
    body += "apps:\n  demo:\n    repo_kind: local\n    repo_url: demo\n    merge_polcy: x\n"
    body += "\n" + "\n".join(before_apps.splitlines()[2:]) + "\ntriage:\n" + rest
    (d / "factory.yaml").write_text(body)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "break")
    return d


def push_branch(bare: Path, name: str) -> str:
    work = bare.parent / f"work-{name}"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(work, "checkout", "-qb", name)
    (work / "factory.yaml").write_text((work / "factory.yaml").read_text() + "\n# next\n")
    _git(work, "commit", "-qam", "edit")
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(work, "push", "-q", "origin", name)
    return sha


def test_load_local_git_repo(tmp_path):
    sha = init_definition_repo(tmp_path / "d")
    r = load_definition(str(tmp_path / "d"))
    assert r.errors == () and r.sha == sha and len(sha) == 40
    assert r.definition is not None and r.definition.app("demo").runner == "standard"


def test_load_git_url_at_ref(tmp_path):
    init_definition_repo(tmp_path / "d")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(tmp_path / "d"), str(tmp_path / "b.git")], check=True
    )
    next_sha = push_branch(tmp_path / "b.git", "next")
    assert load_definition(f"file://{tmp_path / 'b.git'}", ref="next").sha == next_sha


def test_error_has_file_and_line(tmp_path):
    r = load_definition(str(break_merge_policy(tmp_path / "d")))
    e = r.errors[0]
    assert (e.file, e.line, e.path) == ("factory.yaml", 14, "apps.demo.merge_polcy")
    assert r.definition is None
    assert str(e).startswith("factory.yaml:14: apps.demo.merge_polcy: ")


def test_profile_error_points_at_profile_file(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    set_yaml(d, "profiles/team-gemini.yaml", ("roles", "builder", "engine"), "nope")
    assert [e.file for e in load_definition(str(d)).errors] == ["profiles/team-gemini.yaml"]


def test_dirty_tree_sha(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "factory.yaml").write_text((d / "factory.yaml").read_text() + "\n")
    sha = load_definition(str(d)).sha
    assert sha.startswith("dirty-") and len(sha) == 18


def test_symlink_never_followed(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "roles").mkdir(exist_ok=True)
    (d / "roles" / "builder.md").unlink(missing_ok=True)
    (d / "roles" / "builder.md").symlink_to("/dev/zero")
    r = load_definition(str(d))
    assert r.definition is None
    assert [(e.file, e.message) for e in r.errors] == [
        ("roles/builder.md", "not a regular file (symlinks are not allowed)")
    ]


def test_committed_symlink_refused_from_url(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "profiles" / "evil.yaml").symlink_to("/proc/self/environ")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "link")
    subprocess.run(["git", "clone", "-q", "--bare", str(d), str(tmp_path / "b.git")], check=True)
    errs = load_definition(f"file://{tmp_path / 'b.git'}").errors
    assert [(e.file, e.message) for e in errs] == [
        ("profiles/evil.yaml", "not a regular file (symlinks are not allowed)")
    ]


def test_oversized_file_refused(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "roles").mkdir(exist_ok=True)
    (d / "roles" / "builder.md").write_text("---\n---\n" + "x" * MAX_DEFINITION_FILE_BYTES)
    assert [(e.file, e.message) for e in load_definition(str(d)).errors] == [
        ("roles/builder.md", "file is over 256 KB")
    ]


def test_file_count_capped(tmp_path, monkeypatch):
    d = tmp_path / "d"
    init_definition_repo(d)
    monkeypatch.setattr(loader, "MAX_DEFINITION_FILES", 3)
    assert [e.message for e in load_definition(str(d)).errors] == [
        "definition has more than 3 files"
    ]


def test_total_bytes_capped(tmp_path, monkeypatch):
    d = tmp_path / "d"
    init_definition_repo(d)
    monkeypatch.setattr(loader, "MAX_DEFINITION_BYTES", 100)
    assert [e.message for e in load_definition(str(d)).errors] == ["definition is over 16 MB"]


def test_yaml_aliases_refused(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "profiles" / "bomb.yaml").write_text('a: &a ["x", "x"]\nb: &b [*a, *a]\nc: [*b, *b]\n')
    e = load_definition(str(d)).errors[0]
    assert (e.file, e.line, e.message) == ("profiles/bomb.yaml", 2, "YAML aliases are not allowed")


def test_yaml_syntax_error_has_line(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "profiles" / "bad.yaml").write_text("a: 1\nb: [1, 2\n")
    e = load_definition(str(d)).errors[0]
    assert e.file == "profiles/bad.yaml" and e.line is not None


def test_server_mode_refuses_subscription(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    set_yaml(d, "factory.yaml", ("providers", "vertex", "subscription_backed"), True)
    assert load_definition(str(d), mode=FactoryMode.PERSONAL).errors == ()
    errs = load_definition(str(d), mode=FactoryMode.SERVER).errors
    assert [(e.file, e.path) for e in errs if e.file == "factory.yaml"] == [
        ("factory.yaml", "providers.vertex.subscription_backed")
    ]


def test_policy_issue_has_file_and_line(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    set_yaml(d, "runners/standard.yaml", ("image",), "docker.io/x/y:latest")
    e = load_definition(str(d)).errors[0]
    assert (e.file, e.line, e.path) == ("runners/standard.yaml", IMAGE_LINE, "image")


def test_default_policy_comes_from_settings(tmp_path, monkeypatch):
    d = tmp_path / "d"
    init_definition_repo(d)
    reg = "europe-docker.pkg.dev/acme/factory/"
    set_yaml(d, "runners/standard.yaml", ("image",), reg + "runner@sha256:" + "a" * 64)
    assert "not under an allowed image prefix" in load_definition(str(d)).errors[0].message
    monkeypatch.setenv("FACTORY_ALLOWED_IMAGE_PREFIXES", f"{reg},swfactory/")
    get_settings.cache_clear()
    assert load_definition(str(d)).errors == ()
    set_yaml(
        d, "factory.yaml", ("providers", "openrouter", "base_url"), "https://openrouter.ai/api/v1"
    )
    monkeypatch.setenv(
        "FACTORY_ALLOWED_PROVIDER_HOSTS", "generativelanguage.googleapis.com,api.groq.com"
    )
    get_settings.cache_clear()
    assert [e.path for e in load_definition(str(d)).errors] == ["providers.openrouter.base_url"]


def test_apps_dir_merges_into_apps(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "apps").mkdir()
    (d / "apps" / "todo-app.yaml").write_text(
        "repo_kind: local\nrepo_url: todo-app\nlocal_path: /Users/me/work/todo-app\n"
    )
    r = load_definition(str(d))
    assert r.errors == () and r.definition is not None
    assert set(r.definition.factory.apps) == {"demo", "todo-app"}
    assert r.definition.app("todo-app").local_path == "/Users/me/work/todo-app"
    assert r.definition.app("todo-app").merge_policy == MergePolicy.LOCAL_MERGE


def test_app_in_both_places_is_an_error(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "apps").mkdir()
    (d / "apps" / "demo.yaml").write_text("repo_kind: local\nrepo_url: demo\n")
    r = load_definition(str(d))
    assert r.definition is None
    assert [(e.file, e.message) for e in r.errors] == [
        ("apps/demo.yaml", "app 'demo' is also defined in factory.yaml")
    ]


def test_apps_file_error_points_at_its_file(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "apps").mkdir()
    (d / "apps" / "todo-app.yaml").write_text(
        "repo_kind: local\nrepo_url: todo-app\nmerge_polcy: x\n"
    )
    e = load_definition(str(d)).errors[0]
    assert (e.file, e.line, e.path) == ("apps/todo-app.yaml", 3, "merge_polcy")


def test_apps_file_name_must_be_a_slug(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "apps").mkdir()
    (d / "apps" / "Todo App.yaml").write_text("repo_kind: local\nrepo_url: x\n")
    assert [(e.file, e.message) for e in load_definition(str(d)).errors] == [
        ("apps/Todo App.yaml", "app file names must match ^[a-z0-9][a-z0-9-]{0,39}$")
    ]


def test_apps_symlink_never_followed(tmp_path):
    d = tmp_path / "d"
    init_definition_repo(d)
    (d / "apps").mkdir()
    (d / "apps" / "x.yaml").symlink_to("/proc/self/environ")
    assert [(e.file, e.message) for e in load_definition(str(d)).errors] == [
        ("apps/x.yaml", "not a regular file (symlinks are not allowed)")
    ]


def test_init_refuses_non_empty_dir(tmp_path):
    (tmp_path / "x").write_text("x")
    try:
        init_definition_repo(tmp_path)
    except ValueError as e:
        assert "not empty" in str(e)
    else:
        raise AssertionError("expected ValueError")
