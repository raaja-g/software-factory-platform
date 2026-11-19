"""Load and validate a factory definition from a local path or a git URL."""

import hashlib
import importlib.resources
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from swfactory.definition.models import (
    SLUG_PATTERN,
    Definition,
    DefinitionPolicy,
    validate_definition,
)
from swfactory.domain.types import FactoryMode
from swfactory.settings import get_settings

MAX_DEFINITION_FILE_BYTES = 262_144
MAX_DEFINITION_FILES = 2_000
MAX_DEFINITION_BYTES = 16_777_216

_SLUG = re.compile(SLUG_PATTERN)
_SYMLINK_MSG = "not a regular file (symlinks are not allowed)"


@dataclass(frozen=True)
class DefinitionError:
    file: str
    line: int | None
    path: str
    message: str

    def __str__(self) -> str:
        head = f"{self.file}:{self.line}" if self.line is not None else self.file
        return f"{head}: {self.path}: {self.message}" if self.path else f"{head}: {self.message}"


@dataclass(frozen=True)
class LoadResult:
    sha: str
    source: str
    definition: Definition | None
    errors: tuple[DefinitionError, ...]


# --------------------------------------------------------------------------
# Reading files under the file rules
# --------------------------------------------------------------------------


class _Reader:
    """Reads the known definition paths from a root, never following symlinks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, bytes] = {}
        self.errors: list[DefinitionError] = []
        self.total = 0
        self.overflow = False

    def _err(self, rel: str, msg: str) -> None:
        self.errors.append(DefinitionError(rel, None, "", msg))

    def _lstat(self, rel: str) -> os.stat_result | None:
        try:
            return os.lstat(self.root / rel)
        except FileNotFoundError:
            return None
        except OSError as e:
            self._err(rel, f"cannot read: {e.strerror}")
            return None

    def listdir(self, rel: str) -> list[str]:
        """Names in a directory; a missing directory is empty, a non-directory is an error."""
        st = self._lstat(rel)
        if st is None:
            return []
        if not stat.S_ISDIR(st.st_mode):
            self._err(rel, _SYMLINK_MSG)
            return []
        try:
            return sorted(os.listdir(self.root / rel))
        except OSError as e:
            self._err(rel, f"cannot read: {e.strerror}")
            return []

    def read(self, rel: str, *, required: bool = False) -> bool:
        st = self._lstat(rel)
        if st is None:
            if required:
                self._err(rel, "file not found")
            return False
        if not stat.S_ISREG(st.st_mode):
            self._err(rel, _SYMLINK_MSG)
            return False
        if len(self.files) + 1 > MAX_DEFINITION_FILES:
            if not self.overflow:
                self.overflow = True
                self._err("", f"definition has more than {MAX_DEFINITION_FILES} files")
            return False
        try:
            fd = os.open(self.root / rel, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            self._err(rel, _SYMLINK_MSG)
            return False
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                self._err(rel, _SYMLINK_MSG)
                return False
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(MAX_DEFINITION_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_DEFINITION_FILE_BYTES:
            self._err(rel, "file is over 256 KB")
            return False
        self.total += len(data)
        if self.total > MAX_DEFINITION_BYTES:
            if not any(e.message == "definition is over 16 MB" for e in self.errors):
                self._err("", "definition is over 16 MB")
            return False
        self.files[rel] = data
        return True


def _read_tree(root: Path) -> _Reader:
    r = _Reader(root)
    r.read("factory.yaml", required=True)
    for sub, suffix in (
        ("apps", ".yaml"),
        ("profiles", ".yaml"),
        ("roles", ".md"),
        ("runners", ".yaml"),
        ("automations", ".yaml"),
        ("webhooks", ".yaml"),
        ("scorers", ".md"),
    ):
        for name in r.listdir(sub):
            if name.endswith(suffix):
                r.read(f"{sub}/{name}")
    for suite in r.listdir("benchmarks"):
        base = f"benchmarks/{suite}"
        st = r._lstat(base)
        if st is None or not stat.S_ISDIR(st.st_mode):
            if st is not None:
                r._err(base, _SYMLINK_MSG)
            continue
        r.read(f"{base}/suite.yaml")
        for name in r.listdir(f"{base}/tasks"):
            if name.endswith(".yaml"):
                r.read(f"{base}/tasks/{name}")
    return r


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class _Parsed:
    def __init__(self, data: Any, node: yaml.Node | None, line_offset: int = 0) -> None:
        self.data = data
        self.node = node
        self.line_offset = line_offset


def _parse_yaml(rel: str, text: str, errors: list[DefinitionError], line_offset: int = 0):
    """Parse YAML text, refusing aliases. Returns _Parsed or None after recording an error."""
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent):
                line = ev.start_mark.line + 1 + line_offset if ev.start_mark else None
                errors.append(DefinitionError(rel, line, "", "YAML aliases are not allowed"))
                return None
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 + line_offset if mark is not None else None
        problem = getattr(e, "problem", None) or "invalid YAML"
        errors.append(DefinitionError(rel, line, "", str(problem)))
        return None
    return _Parsed(data, node, line_offset)


def _split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Return (frontmatter yaml or None, body, body-less line offset of the frontmatter)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, text, 0
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :]), 1
    return None, text, 0


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


@dataclass
class _Source:
    file: str
    parsed: _Parsed | None  # node tree to walk for line numbers
    prefix: tuple[str, ...] = ()  # loc keys consumed by the container (not in the node tree)


def _stem(rel: str) -> str:
    return Path(rel).stem


def _assemble(reader: _Reader) -> tuple[dict[str, Any] | None, dict[str, _Source], list]:
    errors: list[DefinitionError] = []
    sources: dict[str, _Source] = {}  # key: loc-prefix joined by "/"
    files = reader.files

    def text_of(rel: str) -> str:
        return files[rel].decode("utf-8", errors="replace")

    def load_yaml(rel: str) -> _Parsed | None:
        return _parse_yaml(rel, text_of(rel), errors)

    def load_md(rel: str) -> tuple[_Parsed, str] | None:
        fm, body, off = _split_frontmatter(text_of(rel))
        if fm is None:
            return _Parsed({}, None), body
        p = _parse_yaml(rel, fm, errors, line_offset=off)
        if p is None:
            return None
        if p.data is None:
            p.data = {}
        return p, body

    def mapping(rel: str, p: _Parsed) -> dict[str, Any] | None:
        if p.data is None:
            return {}
        if not isinstance(p.data, dict):
            errors.append(DefinitionError(rel, 1, "", "expected a mapping at the top level"))
            return None
        return p.data

    out: dict[str, Any] = {}

    p = load_yaml("factory.yaml") if "factory.yaml" in files else None
    factory = mapping("factory.yaml", p) if p else None
    if p is not None:
        sources["factory"] = _Source("factory.yaml", p)
    if factory is not None:
        factory = dict(factory)
        out["factory"] = factory

    simple = (
        ("profiles", ".yaml"),
        ("runners", ".yaml"),
        ("automations", ".yaml"),
        ("webhooks", ".yaml"),
    )
    for section, _suffix in simple:
        for rel in sorted(f for f in files if f.startswith(section + "/")):
            sp = load_yaml(rel)
            if sp is None:
                continue
            data = mapping(rel, sp)
            if data is None:
                continue
            out.setdefault(section, {})[_stem(rel)] = data
            sources[f"{section}/{_stem(rel)}"] = _Source(rel, sp)

    for rel in sorted(f for f in files if f.startswith("roles/")):
        res = load_md(rel)
        if res is None:
            continue
        mp, body = res
        out.setdefault("roles", {})[_stem(rel)] = {"frontmatter": mp.data, "body": body}
        sources[f"roles/{_stem(rel)}"] = _Source(rel, mp, ("frontmatter",))

    for rel in sorted(f for f in files if f.startswith("scorers/")):
        res = load_md(rel)
        if res is None:
            continue
        mp, body = res
        data = mapping(rel, mp)
        if data is None:
            continue
        out.setdefault("scorers", {})[_stem(rel)] = {**data, "rubric": body}
        sources[f"scorers/{_stem(rel)}"] = _Source(rel, mp)

    suites = sorted({f.split("/")[1] for f in files if f.startswith("benchmarks/")})
    for suite in suites:
        rel = f"benchmarks/{suite}/suite.yaml"
        sdata: dict[str, Any] = {}
        if rel in files:
            sp = load_yaml(rel)
            if sp is not None:
                d = mapping(rel, sp)
                if d is not None:
                    sdata = dict(d)
                    sources[f"benchmarks/{suite}"] = _Source(rel, sp)
        tasks: dict[str, Any] = {}
        for trel in sorted(f for f in files if f.startswith(f"benchmarks/{suite}/tasks/")):
            tp = load_yaml(trel)
            if tp is None:
                continue
            td = mapping(trel, tp)
            if td is None:
                continue
            tasks[_stem(trel)] = td
            sources[f"benchmarks/{suite}/tasks/{_stem(trel)}"] = _Source(trel, tp)
        if tasks:
            sdata["tasks"] = tasks
        out.setdefault("benchmarks", {})[suite] = sdata

    # apps/*.yaml merge into factory.apps
    if factory is not None:
        apps = dict(factory.get("apps") or {}) if isinstance(factory.get("apps"), dict) else None
        if apps is not None or factory.get("apps") is None:
            apps = apps or {}
            for rel in sorted(f for f in files if f.startswith("apps/")):
                slug = _stem(rel)
                if not _SLUG.match(slug):
                    errors.append(
                        DefinitionError(rel, None, "", f"app file names must match {SLUG_PATTERN}")
                    )
                    continue
                ap = load_yaml(rel)
                if ap is None:
                    continue
                data = mapping(rel, ap)
                if data is None:
                    continue
                if slug in apps:
                    errors.append(
                        DefinitionError(
                            rel, None, "", f"app '{slug}' is also defined in factory.yaml"
                        )
                    )
                    continue
                apps[slug] = data
                sources[f"apps/{slug}"] = _Source(rel, ap)
            factory["apps"] = apps

    if errors:
        return None, sources, errors
    return out, sources, errors


# --------------------------------------------------------------------------
# Mapping issues to file and line
# --------------------------------------------------------------------------


def _walk(parsed: _Parsed | None, loc: tuple[Any, ...]) -> int | None:
    if parsed is None or parsed.node is None:
        return None
    node: yaml.Node = parsed.node
    line = node.start_mark.line
    for key in loc:
        if isinstance(node, yaml.MappingNode):
            want = getattr(key, "value", key)
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == str(want):
                    line, node = k.start_mark.line, v
                    break
            else:
                break
        elif isinstance(node, yaml.SequenceNode) and isinstance(key, int):
            if 0 <= key < len(node.value):
                node = node.value[key]
                line = node.start_mark.line
            else:
                break
        else:
            break
    return line + 1 + parsed.line_offset


def _locate(
    loc: tuple[str | int, ...], sources: dict[str, _Source]
) -> tuple[str, int | None, tuple[Any, ...]]:
    """Return (file, line, path-within-file) for an issue loc."""
    if not loc:
        return "factory.yaml", None, ()
    head = str(loc[0])
    if head == "factory":
        if len(loc) >= 3 and loc[1] == "apps" and f"apps/{loc[2]}" in sources:
            src = sources[f"apps/{loc[2]}"]
            rest = tuple(loc[3:])
            return src.file, _walk(src.parsed, rest), rest
        src = sources.get("factory")
        rest = tuple(loc[1:])
        return "factory.yaml", _walk(src.parsed if src else None, rest), rest
    if head == "benchmarks" and len(loc) >= 2:
        suite = str(loc[1])
        if len(loc) >= 4 and loc[2] == "tasks" and f"benchmarks/{suite}/tasks/{loc[3]}" in sources:
            src = sources[f"benchmarks/{suite}/tasks/{loc[3]}"]
            rest = tuple(loc[4:])
            return src.file, _walk(src.parsed, rest), rest
        src = sources.get(f"benchmarks/{suite}")
        rest = tuple(loc[2:])
        if src is not None:
            return src.file, _walk(src.parsed, rest), rest
        return f"benchmarks/{suite}/suite.yaml", None, rest
    if len(loc) >= 2:
        src = sources.get(f"{head}/{loc[1]}")
        rest = tuple(loc[2:])
        if src is not None:
            walk_rest = rest[len(src.prefix) :] if rest[: len(src.prefix)] == src.prefix else rest
            return src.file, _walk(src.parsed, walk_rest), rest
        ext = ".md" if head in ("roles", "scorers") else ".yaml"
        return f"{head}/{loc[1]}{ext}", None, rest
    return "factory.yaml", None, tuple(loc)


def _path_str(path: tuple[Any, ...]) -> str:
    return ".".join(str(getattr(p, "value", p)) for p in path)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _is_url(source: str) -> bool:
    return "://" in source or re.match(r"^[\w.-]+@[\w.-]+:", source) is not None


@dataclass(frozen=True)
class GitFetch:
    """Hardened git options for fetching a remote definition: ``-c`` args and an env.

    The env (which may hold ``GIT_TOKEN``) replaces the process env, so nothing else leaks in."""

    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


GIT_FETCH: ContextVar[GitFetch | None] = ContextVar("definition_git_fetch", default=None)


def _git_env() -> dict[str, str]:
    fetch = GIT_FETCH.get()
    if fetch is not None:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **fetch.env}
    else:
        env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "file:https:http:ssh:git"
    return env


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def redact_source(source: str) -> str:
    """The source with any user name or password in a URL removed (it is stored and logged)."""
    parts = urlsplit(source)
    if "://" in source and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return source


def _local_git_sha(root: Path, files: dict[str, bytes]) -> str | None:
    """HEAD sha if root is a clean git work tree and every file read is tracked, else None.

    A file that git ignores (so `status` does not show it) but the loader read would make the sha
    describe different content than was loaded, so it forces the content-hash sha instead.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
        return None
    if _git(root, "status", "--porcelain").stdout.strip():
        return None
    tracked = _git(root, "ls-files", "-z")
    if tracked.returncode != 0:
        return None
    if not set(files) <= {t for t in tracked.stdout.split("\0") if t}:
        return None
    head = _git(root, "rev-parse", "HEAD")
    return head.stdout.strip() if head.returncode == 0 else None


def _dirty_sha(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + files[rel] + b"\0")
    return "dirty-" + h.hexdigest()[:12]


def load_definition(
    source: str,
    *,
    ref: str | None = None,
    mode: FactoryMode = FactoryMode.PERSONAL,
    policy: DefinitionPolicy | None = None,
) -> LoadResult:
    if policy is None:
        policy = replace(
            DefinitionPolicy.from_settings(get_settings()),
            require_image_digest=mode is FactoryMode.SERVER,
        )
    shown = redact_source(source)
    if _is_url(source):
        tmp = Path(tempfile.mkdtemp(prefix="factory-def-"))
        try:
            env = _git_env()
            steps = (
                ("init", "-q"),
                ("remote", "add", "origin", source),
                ("fetch", "-q", "--depth", "1", "origin", ref or "main"),
                ("-c", "core.symlinks=true", "checkout", "-q", "FETCH_HEAD"),
            )
            fetch = GIT_FETCH.get()
            extra = fetch.args if fetch is not None else ()
            for step in steps:
                p = _git(tmp, *extra, *step, env=env)
                if p.returncode != 0:
                    msg = (p.stderr.strip().splitlines() or ["git failed"])[-1]
                    msg = msg.replace(source, shown)
                    err = DefinitionError(shown, None, "", msg)
                    return LoadResult("", shown, None, (err,))
            sha = _git(tmp, "rev-parse", "HEAD").stdout.strip()
            return _finish(shown, sha, _read_tree(tmp), policy, mode)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    root = Path(source).expanduser()
    if not root.is_dir():
        err = DefinitionError(source, None, "", "not a directory")
        return LoadResult("", source, None, (err,))
    root = root.resolve()
    reader = _read_tree(root)
    sha = _local_git_sha(root, reader.files) or _dirty_sha(reader.files)
    return _finish(shown, sha, reader, policy, mode)


def _check_role_overrides(defn: Definition) -> list[DefinitionError]:
    from swfactory.roles.library import (
        DEFAULT_ROLES_DIR,
        RoleFileError,
        check_override,
        parse_role_file,
        spec_from_role_file,
    )

    errors: list[DefinitionError] = []
    for role, rf in defn.roles.items():
        file = f"roles/{role.value}.md"
        try:
            override = spec_from_role_file(rf, role=role)
            default_path = DEFAULT_ROLES_DIR / f"{role.value}.md"
            default = parse_role_file(default_path.read_text(encoding="utf-8"), role=role)
            check_override(default, override)
        except RoleFileError as e:
            errors.append(DefinitionError(file, None, "", str(e)))
    return errors


def _finish(
    source: str, sha: str, reader: _Reader, policy: DefinitionPolicy, mode: FactoryMode
) -> LoadResult:
    if reader.errors:
        return LoadResult(sha, source, None, tuple(reader.errors))
    data, sources, errors = _assemble(reader)
    if data is None or errors:
        return LoadResult(sha, source, None, tuple(errors))
    defn, issues = validate_definition(data, policy)
    out: list[DefinitionError] = []
    for issue in issues:
        file, line, rest = _locate(issue.loc, sources)
        out.append(DefinitionError(file, line, _path_str(rest), issue.msg))
    if defn is not None:
        out.extend(_check_role_overrides(defn))
    if defn is not None and mode is FactoryMode.SERVER:
        for pname, prov in defn.factory.providers.items():
            if prov.subscription_backed:
                loc = ("factory", "providers", pname, "subscription_backed")
                file, line, rest = _locate(loc, sources)
                out.append(
                    DefinitionError(
                        file,
                        line,
                        _path_str(rest),
                        "subscription-backed providers are not allowed in server mode",
                    )
                )
    if out:
        return LoadResult(sha, source, None, tuple(out))
    return LoadResult(sha, source, defn, ())


# --------------------------------------------------------------------------
# Defaults and init
# --------------------------------------------------------------------------


def defaults_dir() -> Path:
    packaged = Path(str(importlib.resources.files("swfactory") / "_defaults"))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "defaults"


def init_definition_repo(target: Path) -> str:
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"{target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(defaults_dir(), target, dirs_exist_ok=True)
    env = _git_env()
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        (
            "-c",
            "user.name=factory-bot",
            "-c",
            "user.email=factory-bot@localhost",
            "commit",
            "-q",
            "-m",
            "Initial factory definition",
        ),
    ):
        p = _git(target, *args, env=env)
        if p.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()}")
    return _git(target, "rev-parse", "HEAD").stdout.strip()
