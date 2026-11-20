"""Build a benchmark task (with hidden checks) from a merged job."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from pydantic import ValidationError
from sqlalchemy import select

from swfactory.commands import format_job_ref
from swfactory.db.repo import JobRefError, resolve_job_ref
from swfactory.db.tables import App, JobEvent
from swfactory.definition.models import SLUG_PATTERN, AppDef, BenchTaskDef, Definition
from swfactory.definition.writer import BENCH_PATH_RE
from swfactory.domain.types import Stage
from swfactory.gitops.service import app_slug

if TYPE_CHECKING:
    from swfactory.artifacts import ArtifactStore

TEST_GLOBS = (
    "tests/**",
    "test/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*.test.[jt]s",
    "**/*.test.[jt]sx",
    "**/*.spec.[jt]s",
    "**/*.spec.[jt]sx",
    "**/__tests__/**",
)
MAX_CHECK_BYTES = 256 * 1024
DEFAULT_PY_COMMAND = "python3 -m pytest -q {file}"
_ACCEPT_HEAD = re.compile(r"^#{1,6}\s*acceptance\b.*$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$")


def _glob_match(path: str, pattern: str) -> bool:
    """`**` crosses directories (and may match none); `*` and `?` stay in one segment."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.index("]", i)
            out.append(pattern[i : j + 1])
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.fullmatch("".join(out), path) is not None


def is_test_path(path: str) -> bool:
    return any(_glob_match(path, g) for g in TEST_GLOBS)


def acceptance_items(spec_md: str) -> list[str]:
    """Checkbox items under the spec's "Acceptance criteria" heading."""
    items: list[str] = []
    inside = False
    for line in spec_md.splitlines():
        if line.lstrip().startswith("#"):
            inside = _ACCEPT_HEAD.match(line.strip()) is not None
            continue
        if inside and (m := _ITEM.match(line)):
            items.append(m.group(1))
    return items


def app_for_repo(defn: Definition, repo: str) -> AppDef:
    """The app a task's `repo` names: `local:<slug>` or `github:<owner>/<name>`."""
    kind, _, ident = repo.partition(":")
    apps = defn.factory.apps
    if kind == "local":
        if ident in apps:
            return apps[ident]
        for a in apps.values():
            if a.repo_kind == "local" and app_slug(a) == ident:
                return a
    elif kind == "github":
        for a in apps.values():
            if a.repo_kind == "github" and _github_slug(a.repo_url) == ident:
                return a
    raise KeyError(f"no app for repo {repo!r}")


_GH = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _github_slug(url: str) -> str | None:
    m = _GH.search(url)
    return f"{m[1]}/{m[2]}" if m else None


def repo_ref(slug: str, app: AppDef) -> str:
    if app.repo_kind == "github":
        gh = _github_slug(app.repo_url)
        if gh:
            return f"github:{gh}"
    return f"local:{slug}"


class MergedChanges(Protocol):
    async def changed(self, app: AppDef, merge_sha: str) -> list[tuple[str, str]]: ...

    async def read(self, app: AppDef, sha: str, path: str) -> str | None: ...


class LocalMergedChanges:
    def __init__(self, repos_dir: Path) -> None:
        self.repos_dir = Path(repos_dir)

    async def _git(self, app: AppDef, *args: str) -> bytes | None:
        bare = self.repos_dir / f"{app_slug(app)}.git"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(bare),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace')[:200]}")
        return out

    async def changed(self, app: AppDef, merge_sha: str) -> list[tuple[str, str]]:
        out = await self._git(
            app, "diff", "--name-status", "--no-renames", "-z", f"{merge_sha}^1", merge_sha
        )
        parts = (out or b"").decode(errors="replace").split("\0")
        rows: list[tuple[str, str]] = []
        for i in range(0, len(parts) - 1, 2):
            rows.append((parts[i][:1], parts[i + 1]))
        return rows

    async def read(self, app: AppDef, sha: str, path: str) -> str | None:
        try:
            out = await self._git(app, "show", f"{sha}:{path}")
        except RuntimeError:
            return None
        return None if out is None else out.decode(errors="replace")


_GH_STATUS = {"added": "A", "modified": "M", "changed": "M", "removed": "D", "renamed": "A"}


class GitHubMergedChanges:
    """Changes of a merge commit against its first parent, read through the GitHub API."""

    def __init__(self, client_factory: Callable[[str], Any]) -> None:
        self._factory = client_factory

    def _client(self, app: AppDef) -> Any:
        slug = _github_slug(app.repo_url)
        if slug is None:
            raise RuntimeError(f"not a GitHub repository: {app.repo_url}")
        return self._factory(slug)

    async def changed(self, app: AppDef, merge_sha: str) -> list[tuple[str, str]]:
        gh = self._client(app)
        commit = await gh.get_commit(merge_sha)
        parents = commit.get("parents") or []
        if not parents:
            return []
        files = await gh.compare_files(str(parents[0]["sha"]), merge_sha)
        return [(_GH_STATUS.get(str(f.get("status")), "M"), str(f["filename"])) for f in files]

    async def read(self, app: AppDef, sha: str, path: str) -> str | None:
        data = await self._client(app).get_file_content(path, sha)
        return None if data is None else base64.b64decode(data).decode(errors="replace")


@dataclass(frozen=True, slots=True)
class BenchTaskDraft:
    suite: str
    name: str
    files: Mapping[str, str]


class BenchTaskError(ValueError):
    """A job cannot become a benchmark task."""


def _task_slug(seq: int, slug: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-") or "job"
    return f"{seq:03d}-{base}"[:40].rstrip("-")


def _dump(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)


async def task_from_job(
    s: Any,
    job_ref: str,
    *,
    suite: str,
    name: str | None,
    scorers: Sequence[str],
    check_command: str | None = None,
    changes: MergedChanges,
    artifacts: ArtifactStore,
    defn: Definition,
) -> BenchTaskDraft:
    if not re.fullmatch(SLUG_PATTERN, suite):
        raise BenchTaskError(f"suite '{suite}' must be a lowercase slug")
    try:
        job = await resolve_job_ref(s, job_ref)
    except JobRefError as e:
        raise BenchTaskError(str(e)) from None
    if job.stage != Stage.MERGED.value or job.app_id is None:
        raise BenchTaskError("only merged jobs can become benchmark tasks")
    app_row = await s.get(App, job.app_id)
    app_def = defn.factory.apps.get(app_row.slug) if app_row else None
    if app_row is None or app_def is None:
        raise BenchTaskError("the job's app is not in the active definition")
    ev = await s.scalar(
        select(JobEvent.data)
        .where(JobEvent.job_id == job.id, JobEvent.kind == "delivery.merged")
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    merge_sha = str((ev or {}).get("merge_sha") or "")
    if not merge_sha:
        raise BenchTaskError("merge sha not recorded for this job")
    if not job.base_sha or not job.definition_sha:
        raise BenchTaskError("the job has no recorded base sha or definition sha")
    task = name or _task_slug(job.seq, job.slug)
    if not re.fullmatch(SLUG_PATTERN, task):
        raise BenchTaskError(f"task name '{task}' must be a lowercase slug")
    if task in defn.benchmarks.get(suite, _NO_SUITE).tasks:
        raise BenchTaskError(f"task '{task}' already exists in suite '{suite}'")
    unknown = [x for x in scorers if x not in defn.scorers]
    if unknown:
        raise BenchTaskError(f"unknown scorer: {', '.join(unknown)}")

    checks: dict[str, str] = {}
    for status, path in sorted(await changes.changed(app_def, merge_sha), key=lambda r: r[1]):
        if status not in ("A", "M") or not is_test_path(path):
            continue
        text = await changes.read(app_def, merge_sha, path)
        if text is None or len(text.encode()) > MAX_CHECK_BYTES:
            continue
        checks[path] = text
    if not checks:
        raise BenchTaskError("the merge added or changed no test files")
    hidden = sorted(checks)
    command = check_command
    if command is None:
        bad = sorted({PurePosixPath(p).suffix or p for p in hidden if not p.endswith(".py")})
        if bad:
            raise BenchTaskError(f"set check_command: no default for {', '.join(bad)} checks")
        command = DEFAULT_PY_COMMAND

    spec = await artifacts.get_text(job.id, "spec.md")
    criteria = acceptance_items(spec or "") or [job.feature.strip().splitlines()[0]]
    task_doc: dict[str, Any] = {
        "title": job.title,
        "feature": job.feature,
        "success_criteria": criteria,
        "repo": repo_ref(app_row.slug, app_def),
        "base_sha": job.base_sha,
        "definition_sha": job.definition_sha,
        "source_job": format_job_ref(app_row.slug, job.seq),
        "merge_sha": merge_sha,
        "hidden_checks": hidden,
        "check_command": command,
        "scorers": list(scorers),
    }
    try:
        BenchTaskDef.model_validate(task_doc)
    except ValidationError as e:
        raise BenchTaskError(str(e.errors()[0]["msg"])) from None
    files: dict[str, str] = {}
    if suite not in defn.benchmarks:
        profile = job.profile_name or app_def.profile
        if not profile:
            raise BenchTaskError("the job has no profile name; cannot create the suite")
        files[f"benchmarks/{suite}/suite.yaml"] = _dump(
            {"configurations": [{"name": "current", "profile": profile}], "budget_usd": 40}
        )
    files[f"benchmarks/{suite}/tasks/{task}.yaml"] = _dump(task_doc)
    for path, text in checks.items():
        stored = f"benchmarks/{suite}/checks/{task}/{path}"
        if not BENCH_PATH_RE.fullmatch(stored):
            raise BenchTaskError(f"test file '{path}' has a name a benchmark cannot store")
        files[stored] = text
    return BenchTaskDraft(suite, task, files)


class _EmptySuite:
    tasks: dict[str, Any] = {}


_NO_SUITE = _EmptySuite()


# --------------------------------------------------------------------------
# The shipped v1-parity suite, built once from v1's own history
# --------------------------------------------------------------------------

V1_BOT = "factory-bot <factory-bot@localhost>"
V1_DATE = "2026-11-10T00:00:00Z"
V1_CHECK_COMMAND = "python3 -m unittest -q {file}"
V1_SCORERS = ("merge-readiness", "spec-adherence")
V1_SUITE_YAML = """\
description: Replay of v1's four features (bootstrap, todo CLI, todo web page, Origin null fix).
budget_usd: 40
max_parallel: 1
configurations:
  - { name: gemini, profile: team-gemini }
  - { name: claude, profile: claude-vertex }
  - { name: economy, profile: mixed-economy }
"""


@dataclass(frozen=True, slots=True)
class V1Feature:
    task: str
    title: str
    base: str  # the v1 commit the job started from
    merge: str  # the v1 merge commit
    jobs_at: str  # the v1 commit that holds factory/jobs/<task>/


V1_FEATURES = (
    V1Feature(
        "001-bootstrap-python", "Bootstrap a Python project", "63fc255", "9ad61a7", "d3f8077"
    ),
    V1Feature("002-todo-cli", "Add a todo command line", "d3f8077", "04aa2e6", "282ed6e"),
    V1Feature("003-todo-web", "Add a local todo web page", "282ed6e", "1ff7785", "8adba22"),
    V1Feature(
        "004-fix-origin-null-403",
        "Fix 403 on browser form posts (Origin: null)",
        "8adba22",
        "adf9d7b",
        "481a1bf",
    ),
)


def _git(
    cwd: Path, *args: str, env: Mapping[str, str] | None = None, input: str | None = None
) -> str:
    import os
    import subprocess

    full_env = {**os.environ, **(env or {})}
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=full_env,
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def _tree_without_factory(repo: Path, commit: str, index: Path) -> str:
    env = {"GIT_INDEX_FILE": str(index)}
    index.unlink(missing_ok=True)
    _git(repo, "read-tree", commit, env=env)
    _git(repo, "rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", "factory", env=env)
    return _git(repo, "write-tree", env=env).strip()


def _commit(repo: Path, tree: str, parent: str | None, message: str) -> str:
    name, _, rest = V1_BOT.partition(" <")
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": rest.rstrip(">"),
        "GIT_AUTHOR_DATE": V1_DATE,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": rest.rstrip(">"),
        "GIT_COMMITTER_DATE": V1_DATE,
    }
    args = ["commit-tree", tree, *(["-p", parent] if parent else [])]
    return _git(repo, *args, env=env, input=message + "\n").strip()


def _full_criteria(spec_md: str) -> list[str]:
    """Acceptance items with their indented continuation lines joined (v1 wraps long items)."""
    out: list[str] = []
    inside = False
    for line in spec_md.splitlines():
        if line.lstrip().startswith("#"):
            inside = _ACCEPT_HEAD.match(line.strip()) is not None
        elif inside and (m := _ITEM.match(line)):
            out.append(m.group(1))
        elif inside and out and line.startswith((" ", "\t")) and line.strip():
            out[-1] = f"{out[-1]} {line.strip()}"
    return out


def build_v1_parity(v1_repo: Path, out: Path) -> dict[str, str]:
    """Write the v1-parity suite into `out`; returns task -> rewritten base sha."""
    import subprocess
    import tempfile

    v1_repo, out = Path(v1_repo).resolve(), Path(out).resolve()
    bases: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(v1_repo), str(work)],
            check=True,
            capture_output=True,
        )
        index = Path(td) / "index"
        parent: str | None = None
        merges: dict[str, str] = {}
        for i, f in enumerate(V1_FEATURES, start=1):
            base_full = _git(work, "rev-parse", f"{f.base}^{{commit}}").strip()
            merge_full = _git(work, "rev-parse", f"{f.merge}^{{commit}}").strip()
            base = _commit(
                work, _tree_without_factory(work, base_full, index), parent, f"v1 base {f.task}"
            )
            merge = _commit(
                work, _tree_without_factory(work, merge_full, index), base, f"v1 merge {f.task}"
            )
            _git(work, "update-ref", f"refs/heads/base-{i:03d}", base)
            _git(work, "update-ref", f"refs/heads/merged-{i:03d}", merge)
            bases[f.task], merges[f.task], parent = base, merge, base
        _git(work, "update-ref", "refs/heads/main", parent or "")
        refs = [f"base-{i:03d}" for i in range(1, 5)] + [f"merged-{i:03d}" for i in range(1, 5)]
        out.mkdir(parents=True, exist_ok=True)
        bundle = out / "repo.bundle"
        bundle.unlink(missing_ok=True)
        _git(work, "bundle", "create", str(bundle), "main", *refs)

        (out / "suite.yaml").write_text(V1_SUITE_YAML)
        for f in V1_FEATURES:
            full_merge = _git(work, "rev-parse", f"{f.merge}^{{commit}}").strip()
            changed = _git(
                work,
                "diff",
                "--name-only",
                "--no-renames",
                "--diff-filter=AM",
                f"{full_merge}^1",
                full_merge,
            ).splitlines()
            hidden = sorted(p for p in changed if is_test_path(p))
            if not hidden:
                raise BenchTaskError(f"{f.task}: the merge changed no test files")
            for p in hidden:
                dest = out / "checks" / f.task / p
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(_git(work, "show", f"{full_merge}:{p}"))
            feature = _git(work, "show", f"{f.jobs_at}:factory/jobs/{f.task}/feature.md").strip()
            spec = _git(work, "show", f"{f.jobs_at}:factory/jobs/{f.task}/spec.md")
            doc: dict[str, Any] = {
                "title": f.title,
                "feature": feature,
                "success_criteria": _full_criteria(spec) or [feature.splitlines()[0]],
                "repo": "local:v1-parity",
                "base_sha": bases[f.task],
                "definition_sha": _git(work, "rev-parse", f"{f.base}^{{commit}}").strip(),
                "source_job": f"v1:{f.task.split('-')[0]}",
                "merge_sha": merges[f.task],
                "hidden_checks": hidden,
                "check_command": V1_CHECK_COMMAND,
                "scorers": list(V1_SCORERS),
            }
            BenchTaskDef.model_validate(doc)
            (out / "tasks").mkdir(exist_ok=True)
            (out / "tasks" / f"{f.task}.yaml").write_text(_dump(doc))
    return bases


def seed_bench_repo(slug: str, bundle: Path, repos_dir: Path) -> Path:
    """Import a suite's repo.bundle into repos_dir/<slug>.git; running it again changes nothing."""
    import subprocess

    from swfactory.gitops.local import LocalGit
    from swfactory.gitops.service import SAFE_GIT_ENV, safe_git

    if not Path(bundle).is_file():
        raise FileNotFoundError(f"no repo.bundle at {bundle}")
    bare = LocalGit(Path(repos_dir)).init_bare(slug)
    import os

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"), **SAFE_GIT_ENV}
    p = subprocess.run(
        safe_git("-C", str(bare), "fetch", "-q", str(bundle), "refs/heads/*:refs/heads/*"),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git fetch failed: {p.stderr.strip()[:300]}")
    return bare


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "v1-parity":
        print("usage: python -m swfactory.bench.tasks v1-parity <v1-repo> <out-dir>")
        return 2
    for task, sha in build_v1_parity(Path(args[1]).expanduser(), Path(args[2])).items():
        print(f"{task} {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
