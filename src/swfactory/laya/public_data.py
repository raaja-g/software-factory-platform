"""Build Laya training rows from public datasets (kind, size, touches_ui).

    uv run python -m swfactory.laya.public_data --question all --built-on 2026-11-20

Every row is ``(state, label)`` for one question, in the CSV format ``factory laya import`` accepts.
The ``state`` is produced by the repo's own ``triage_state`` so its keys and its 512-token trim are
those of a real job. Labels come only from rules written down in ``DATASETS`` and
``docs/laya-public-data.md``; ``risk`` and ``tier.*`` have no honest public source and are refused.
Nothing here reads the clock: the build date is an argument.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import httpx

from swfactory.laya.fit import MIN_ROWS_PER_CLASS
from swfactory.laya.import_rows import ImportRefused, read_import_file
from swfactory.logging import redact
from swfactory.models.triage import TOUCHES_UI_LABELS, TRIAGE_QUESTIONS, triage_state

HUB = "https://huggingface.co"
ROWS_API = "https://datasets-server.huggingface.co"
STATE_MAX_TOKENS = 512
DEFAULT_LIMIT = 5000
DEFAULT_MAX_RATIO = 3
NEAR_DUPLICATE_CAP = 3
QUESTIONS = ("kind", "size", "touches_ui")
NO_PUBLIC_SOURCE = "risk"
# A copy of AppDef.ui_globs' default: a real job's state carries the app's globs (a test checks it).
DEFAULT_UI_GLOBS: tuple[str, ...] = (
    "**/*.html",
    "**/*.htm",
    "**/*.css",
    "**/*.scss",
    "**/*.js",
    "**/*.jsx",
    "**/*.ts",
    "**/*.tsx",
    "**/*.vue",
    "**/*.svelte",
    "**/templates/**",
    "**/static/**",
)
# Licences read from a dataset card that permit this use, keyed by the card's lower-case value.
ALLOWED_LICENCES: Mapping[str, str] = {
    "cc-by-4.0": "CC-BY-4.0",
    "mit": "MIT",
    "apache-2.0": "Apache-2.0",
    "odc-by": "ODC-By-1.0",
    "cc0-1.0": "CC0-1.0",
}

# ---------------------------------------------------------------- label derivation rules

KIND_PHRASES: Mapping[str, str] = {
    "bug": "bugfix",
    "bugs": "bugfix",
    "bug report": "bugfix",
    "bugfix": "bugfix",
    "bug fix": "bugfix",
    "defect": "bugfix",
    "regression": "bugfix",
    "crash": "bugfix",
    "feature": "feature",
    "feature request": "feature",
    "new feature": "feature",
    "feat": "feature",
    "enhancement": "feature",
    "improvement": "feature",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "refactor": "refactor",
    "refactoring": "refactor",
    "cleanup": "refactor",
    "clean up": "refactor",
    "code cleanup": "refactor",
    "tech debt": "refactor",
    "technical debt": "refactor",
    "code quality": "refactor",
    "ci": "infra",
    "ci cd": "infra",
    "cicd": "infra",
    "build": "infra",
    "build system": "infra",
    "dependencies": "infra",
    "dependency": "infra",
    "deps": "infra",
    "infra": "infra",
    "infrastructure": "infra",
    "devops": "infra",
    "packaging": "infra",
    "github actions": "infra",
    "docker": "infra",
}
_LABEL_PREFIXES = frozenset({"type", "kind", "c", "t", "category", "area", "a", "pr", "issue type"})
_SIZE_LABEL = re.compile(r"^size\s*[/:-]\s*(xxs|xs|s|m|l|xl|xxl)$", re.IGNORECASE)
SIZE_PR_LABELS: Mapping[str, str] = {
    "xxs": "S",
    "xs": "S",
    "s": "S",
    "m": "M",
    "l": "L",
    "xl": "L",
    "xxl": "L",
}
UI_EXTENSIONS = (
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".jsx",
    ".tsx",
    ".vue",
    ".svelte",
)
UI_DIRS = frozenset({"templates", "static"})
NON_UI_DIRS = frozenset({"test", "tests", "__tests__", "doc", "docs", "documentation"})
SIZE_S_MAX_LINES = 20
SIZE_L_MIN_FILES = 6
SIZE_L_MIN_LINES = 300


def normalise_label(name: str) -> str:
    """Lower-case, separators to spaces, one leading prefix word (``kind/``, ``C-``) removed."""
    words = re.sub(r"[\s/:_-]+", " ", name.strip().lower()).strip()
    head, _, rest = words.partition(" ")
    return rest if head in _LABEL_PREFIXES and rest else words


def kind_from_labels(labels: Sequence[str]) -> tuple[str | None, str]:
    """(kind, reason). Exact phrases only (``Question/Not a bug`` is not a bug). Unknown labels are
    ignored; no known label gives ``no_label``; two different kinds give ``ambiguous_label``."""
    kinds = {KIND_PHRASES[n] for n in map(normalise_label, labels) if n in KIND_PHRASES}
    if not kinds:
        return None, "no_label"
    if len(kinds) > 1:
        return None, "ambiguous_label"
    return next(iter(kinds)), "ok"


def size_from_pr_labels(labels: Sequence[str]) -> tuple[str | None, str]:
    """A bot's ``size/XS..XXL`` label (lines changed): XXS,XS,S -> S; M -> M; L,XL,XXL -> L."""
    sizes = {
        SIZE_PR_LABELS[m.group(1).lower()] for lb in labels if (m := _SIZE_LABEL.match(lb.strip()))
    }
    if not sizes:
        return None, "no_label"
    if len(sizes) > 1:
        return None, "ambiguous_label"
    return next(iter(sizes)), "ok"


@dataclass(frozen=True)
class PatchStats:
    files: tuple[str, ...]
    lines: int


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def patch_stats(patch: str) -> PatchStats:
    """Paths and added+removed lines of a unified diff (file headers are not counted)."""
    files: list[str] = []
    lines = 0
    in_hunk = False
    for line in patch.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            files.append(m.group(2))
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in "+-":
            lines += 1
    return PatchStats(tuple(files), lines)


def size_from_patch(patch: str) -> str | None:
    """S: one file and at most 20 changed lines; L: 6 or more files or 300 or more lines; else M."""
    st = patch_stats(patch)
    if not st.files:
        return None
    if len(st.files) >= SIZE_L_MIN_FILES or st.lines >= SIZE_L_MIN_LINES:
        return "L"
    if len(st.files) == 1 and st.lines <= SIZE_S_MAX_LINES:
        return "S"
    return "M"


def is_ui_path(path: str) -> bool:
    parts = path.lower().split("/")
    if NON_UI_DIRS & set(parts[:-1]):
        return False
    return parts[-1].endswith(UI_EXTENSIONS) or bool(UI_DIRS & set(parts[:-1]))


def touches_ui_from_patch(patch: str) -> str | None:
    """A when any changed non-test, non-docs file is a UI file (see ``is_ui_path``), else B."""
    files = patch_stats(patch).files
    if not files:
        return None
    return "A" if any(is_ui_path(f) for f in files) else "B"


# ---------------------------------------------------------------- text hygiene

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_EXTRA_SECRETS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b[\"']?"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{12,}"
    ),
]
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def contains_secret(text: str) -> bool:
    """True when the text holds an email address or anything that looks like a key or token."""
    return (
        bool(_EMAIL.search(text))
        or redact(text) != text
        or any(p.search(text) for p in _EXTRA_SECRETS)
    )


def clean_body(text: str) -> str:
    text = _HTML_COMMENT.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_state(title: str, body: str, repo: str) -> str:
    """The JSON a real job with this title/feature on app ``repo`` would show Laya.

    ``repo`` empty gives ``app: unknown`` (what a job with no repo url shows). ``stack`` is
    ``unknown``, ``labels`` is empty (the source labels are the answer and must not leak) and
    ``ui_globs`` is the app default."""
    job = SimpleNamespace(title=title, feature=body, triage={"stack": "unknown", "labels": []})
    app = SimpleNamespace(repo_url=repo, ui_globs=DEFAULT_UI_GLOBS)
    state = triage_state(job, app, max_tokens=STATE_MAX_TOKENS)
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def repo_name(value: Any) -> str:
    text = str(value or "").rstrip("/").removesuffix(".git")
    return text.rsplit("/", 1)[-1] or "unknown"


# ---------------------------------------------------------------- dataset mappers


@dataclass(frozen=True)
class Draft:
    title: str
    body: str
    repo: str
    label: str


Mapped = tuple[Draft | None, str]  # (draft, drop reason)


def _label_names(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name:
            out.append(name)
    return out


def map_requests_issues(row: Mapping[str, Any], question: str) -> Mapped:
    label, why = kind_from_labels(_label_names(row.get("labels")))
    if label is None:
        return None, why
    return Draft(
        str(row.get("title") or ""),
        str(row.get("body") or ""),
        repo_name(row.get("repository_url")),
        label,
    ), "ok"


def map_open_issues(row: Mapping[str, Any], question: str) -> Mapped:
    labels = _label_names(row.get("labels"))
    if question == "size":
        if not row.get("is_pull_request"):
            return None, "not_a_pull_request"
        label, why = size_from_pr_labels(labels)
    else:
        label, why = kind_from_labels(labels)
    if label is None:
        return None, why
    return Draft(
        str(row.get("title") or ""), str(row.get("body") or ""), repo_name(row.get("repo")), label
    ), "ok"


def map_swe_patch(row: Mapping[str, Any], question: str) -> Mapped:
    patch = str(row.get("patch") or "")
    label = size_from_patch(patch) if question == "size" else touches_ui_from_patch(patch)
    if label is None:
        return None, "no_patch_files"
    statement = clean_body(str(row.get("problem_statement") or ""))
    title, _, body = statement.partition("\n")
    return Draft(title[:200], body, repo_name(row.get("repo")), label), "ok"


Mapper = Callable[[Mapping[str, Any], str], Mapped]


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    source: str  # importer --source name
    questions: tuple[str, ...]
    mapper: Mapper
    config: str = "default"
    split: str = "train"
    window: int = 100  # rows per /rows request
    needed: tuple[str, ...] = ()  # columns a truncated cell would make unusable
    synthetic: bool = False
    rules: Mapping[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{HUB}/datasets/{self.dataset}"


_SWE_SIZE = (
    "size from the gold patch: S = one file and at most 20 added+removed lines; "
    "L = 6 or more files or 300 or more lines; M = anything else"
)
_SWE_UI = (
    "touches_ui from the gold patch: A when a changed file outside test/doc(s) dirs ends in "
    ".css .scss .sass .less .html .htm .jsx .tsx .vue .svelte or sits under a templates/ or "
    "static/ dir, else B (a weak proxy)"
)
DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        "Raibek/requests-github-issues",
        "pub-requests-issues",
        ("kind",),
        map_requests_issues,
        needed=("title", "labels"),
        rules={
            "kind": "GitHub labels of psf/requests issues and PRs, exact phrases "
            "(bug->bugfix, feature request/enhancement->feature, documentation->docs, "
            "refactor/cleanup->refactor, ci/build/dependencies/github_actions->infra); "
            "no known label or two different kinds -> row dropped"
        },
    ),
    DatasetSpec(
        "open-index/open-github-issues",
        "pub-open-github-issues",
        ("kind", "size"),
        map_open_issues,
        config="issues",
        needed=("title", "labels"),
        rules={
            "kind": "GitHub labels of issues and PRs, exact phrases after dropping a "
            "kind/type/C-/area prefix (same table as requests-issues); no known label or two "
            "different kinds -> row dropped",
            "size": "pull requests only: bot label size/XXS,XS,S -> S; size/M -> M; "
            "size/L,XL,XXL -> L (Kubernetes-style, lines changed: S under 30, M 30-99, L 100+)",
        },
    ),
    DatasetSpec(
        "SWE-Gym/SWE-Gym",
        "pub-swe-gym",
        ("size", "touches_ui"),
        map_swe_patch,
        window=50,
        needed=("patch", "problem_statement"),
        rules={"size": _SWE_SIZE, "touches_ui": _SWE_UI},
    ),
    DatasetSpec(
        "SWE-bench/SWE-bench_Multilingual",
        "pub-swe-bench-multilingual",
        ("size", "touches_ui"),
        map_swe_patch,
        split="test",
        window=50,
        needed=("patch", "problem_statement"),
        rules={"size": _SWE_SIZE, "touches_ui": _SWE_UI},
    ),
    DatasetSpec(
        "nebius/SWE-rebench",
        "pub-swe-rebench",
        ("size", "touches_ui"),
        map_swe_patch,
        split="test",
        window=50,
        needed=("patch", "problem_statement"),
        rules={"size": _SWE_SIZE, "touches_ui": _SWE_UI},
    ),
    DatasetSpec(
        "SWE-bench/SWE-smith",
        "pub-swe-smith-synthetic",
        ("size", "touches_ui"),
        map_swe_patch,
        window=10,
        needed=("patch", "problem_statement"),
        synthetic=True,
        rules={
            "size": _SWE_SIZE + " (SYNTHETIC: LLM-written issues for injected bugs)",
            "touches_ui": _SWE_UI + " (SYNTHETIC: LLM-written issues for injected bugs)",
        },
    ),
)
SKIPPED_BY_DESIGN: Mapping[str, str] = {
    "princeton-nlp/SWE-bench_Verified": "its dataset card names no licence (cardData.license is "
    "empty), so it is not used even though it is the only source of human difficulty buckets",
}


# ---------------------------------------------------------------- sources of rows


@dataclass(frozen=True)
class DatasetMeta:
    url: str
    licence: str | None  # canonical, from ALLOWED_LICENCES, or None when missing/not allowed
    licence_raw: str | None
    revision: str | None


@dataclass(frozen=True)
class SourceRow:
    row: Mapping[str, Any]
    truncated: frozenset[str] = frozenset()


class RowSource(Protocol):
    def meta(self, spec: DatasetSpec) -> DatasetMeta: ...

    def rows(self, spec: DatasetSpec, limit: int, seed: int) -> Iterator[SourceRow]: ...


def canonical_licence(raw: str | None) -> str | None:
    return ALLOWED_LICENCES.get((raw or "").strip().lower())


class FixtureSource:
    """Rows from ``<dir>/<owner>__<name>.json``: ``{"meta": {...}, "rows": [...]}``; no network."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _load(self, spec: DatasetSpec) -> Mapping[str, Any]:
        path = self.directory / f"{spec.dataset.replace('/', '__')}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no fixture {path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def meta(self, spec: DatasetSpec) -> DatasetMeta:
        m = self._load(spec).get("meta") or {}
        raw = m.get("licence")
        return DatasetMeta(spec.url, canonical_licence(raw), raw, m.get("revision"))

    def rows(self, spec: DatasetSpec, limit: int, seed: int) -> Iterator[SourceRow]:
        for row in list(self._load(spec).get("rows") or [])[:limit]:
            yield SourceRow(row)


class HubSource:
    """Hugging Face hub API and datasets-server ``/rows``: plain httpx, no token, rate limited."""

    def __init__(self, client: httpx.Client | None = None, min_interval: float = 1.0) -> None:
        self.client = client or httpx.Client(
            timeout=120, headers={"User-Agent": "laya-public-data-builder"}
        )
        self.min_interval = min_interval

    def _get(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        last = "no attempt"
        for attempt in range(7):
            time.sleep(self.min_interval)
            try:
                r = self.client.get(url, params=params)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
                retry = r.headers.get("retry-after", "")
                time.sleep(float(retry) if retry.isdigit() else 15.0 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")

    def meta(self, spec: DatasetSpec) -> DatasetMeta:
        info = self._get(f"{HUB}/api/datasets/{spec.dataset}")
        card = info.get("cardData") or {}
        raw = card.get("license")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not raw:
            raw = next(
                (t.split(":", 1)[1] for t in info.get("tags") or [] if t.startswith("license:")),
                None,
            )
        return DatasetMeta(spec.url, canonical_licence(raw), raw, info.get("sha"))

    def _page(self, spec: DatasetSpec, offset: int, length: int) -> Mapping[str, Any]:
        return self._get(
            f"{ROWS_API}/rows",
            {
                "dataset": spec.dataset,
                "config": spec.config,
                "split": spec.split,
                "offset": offset,
                "length": length,
            },
        )

    def rows(self, spec: DatasetSpec, limit: int, seed: int) -> Iterator[SourceRow]:
        """Whole split when it fits ``limit``, else ``limit`` rows from seeded random windows."""
        total = int(self._page(spec, 0, 1).get("num_rows_total") or 0)
        w = spec.window
        blocks = (total + w - 1) // w
        if total <= limit:
            starts = [b * w for b in range(blocks)]
        else:
            rng = random.Random(f"{seed}:{spec.dataset}")
            starts = sorted(b * w for b in rng.sample(range(blocks), (limit + w - 1) // w))
        taken = 0
        for start in starts:
            for item in self._page(spec, start, w).get("rows") or []:
                if taken >= limit:
                    return
                taken += 1
                yield SourceRow(item["row"], frozenset(item.get("truncated_cells") or ()))


# ---------------------------------------------------------------- the build


@dataclass
class DatasetReport:
    dataset: str
    source: str
    url: str
    licence: str
    licence_card: str | None
    revision: str | None
    synthetic: bool
    rule: str
    rows_read: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    kept_before_balance: Counter[str] = field(default_factory=Counter)
    used: Counter[str] = field(default_factory=Counter)

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.dataset,
            "source": self.source,
            "url": self.url,
            "licence": self.licence,
            "licence_in_card": self.licence_card,
            "revision": self.revision,
            "synthetic": self.synthetic,
            "derivation_rule": self.rule,
            "rows_read": self.rows_read,
            "rows_dropped": dict(sorted(self.dropped.items())),
            "rows_mapped_before_balancing": dict(sorted(self.kept_before_balance.items())),
            "rows_used": sum(self.used.values()),
            "rows_used_per_class": dict(sorted(self.used.items())),
        }


@dataclass(frozen=True)
class Row:
    state: str
    label: str
    source: str


@dataclass
class BuildResult:
    question: str
    rows: list[Row]
    reports: list[DatasetReport]
    skipped: list[dict[str, str]]
    before_balance: Counter[str]
    max_ratio: int
    warnings: list[str]


def question_labels(question: str) -> set[str]:
    if question == "touches_ui":
        return set(TOUCHES_UI_LABELS)
    return set(TRIAGE_QUESTIONS[question]["criteria"])


def _near_key(title: str) -> str:
    return re.sub(r"\d+", "#", title.lower())


def balance(rows: Sequence[Row], max_ratio: int, rng: random.Random) -> list[Row]:
    """Cap every class at ``max_ratio`` x the smallest class holding MIN_ROWS_PER_CLASS rows."""
    by_label: dict[str, list[Row]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r.label, r.state)):
        by_label[r.label].append(r)
    if max_ratio <= 0 or not by_label:
        return [r for lb in sorted(by_label) for r in by_label[lb]]
    sizes = [len(v) for v in by_label.values()]
    floor = min([n for n in sizes if n >= MIN_ROWS_PER_CLASS] or sizes)
    out: list[Row] = []
    for lb in sorted(by_label):
        group = by_label[lb]
        out.extend(
            rng.sample(group, floor * max_ratio) if len(group) > floor * max_ratio else group
        )
    return out


def build_question(
    question: str,
    source: RowSource,
    *,
    limit: int = DEFAULT_LIMIT,
    seed: int = 0,
    max_ratio: int = DEFAULT_MAX_RATIO,
    include_synthetic: bool = False,
    keep_repo_name: bool = False,
    datasets: Sequence[DatasetSpec] = DATASETS,
    log: Callable[[str], None] = lambda _m: None,
) -> BuildResult:
    if question not in QUESTIONS:
        raise ValueError(f"no public source for {question}")
    reports: list[DatasetReport] = []
    skipped: list[dict[str, str]] = [
        {"dataset": d, "reason": why} for d, why in sorted(SKIPPED_BY_DESIGN.items())
    ]
    seen: dict[str, Row] = {}
    conflicts: set[str] = set()
    near: Counter[tuple[str, str]] = Counter()
    for spec in datasets:
        if question not in spec.questions:
            continue
        if spec.synthetic and not include_synthetic:
            skipped.append(
                {"dataset": spec.dataset, "reason": "synthetic; pass --include-synthetic"}
            )
            continue
        try:
            meta = source.meta(spec)
        except Exception as e:  # a missing fixture or a hub error skips only this dataset
            skipped.append({"dataset": spec.dataset, "reason": f"metadata unavailable: {e}"})
            continue
        if meta.licence is None:
            why = (
                "its card names no licence"
                if not meta.licence_raw
                else f"licence {meta.licence_raw} is not in the allow-list"
            )
            skipped.append({"dataset": spec.dataset, "reason": why})
            continue
        rep = DatasetReport(
            spec.dataset,
            spec.source,
            meta.url,
            meta.licence,
            meta.licence_raw,
            meta.revision,
            spec.synthetic,
            spec.rules.get(question, ""),
        )
        try:
            for item in source.rows(spec, limit, seed):
                rep.rows_read += 1
                if item.truncated & set(spec.needed):
                    rep.dropped["truncated_cells"] += 1
                    continue
                draft, why = spec.mapper(item.row, question)
                if draft is None:
                    rep.dropped[why] += 1
                    continue
                title, body = clean_title(draft.title), clean_body(draft.body)
                if not title:
                    rep.dropped["empty_title"] += 1
                    continue
                raw = f"{draft.title}\n{draft.body}"
                if contains_secret(raw) or contains_secret(f"{title}\n{body}"):
                    rep.dropped["secret_or_email"] += 1
                    continue
                state = build_state(title, body, draft.repo if keep_repo_name else "")
                if state in conflicts:
                    rep.dropped["conflicting_label"] += 1
                    continue
                prior = seen.get(state)
                if prior is not None:
                    if prior.label != draft.label:
                        del seen[state]
                        conflicts.add(state)
                        rep.dropped["conflicting_label"] += 1
                    else:
                        rep.dropped["duplicate"] += 1
                    continue
                nk = (_near_key(title), draft.label)
                if near[nk] >= NEAR_DUPLICATE_CAP:
                    rep.dropped["near_duplicate"] += 1
                    continue
                near[nk] += 1
                seen[state] = Row(state, draft.label, spec.source)
        except Exception as e:  # keep what was read, say what failed
            skipped.append({"dataset": spec.dataset, "reason": f"fetch stopped early: {e}"})
        reports.append(rep)
        log(f"{question} {spec.dataset}: read {rep.rows_read}, dropped {dict(rep.dropped)}")
    pool = list(seen.values())
    before = Counter(r.label for r in pool)
    by_source = {rep.source: rep for rep in reports}
    for r in pool:
        by_source[r.source].kept_before_balance[r.label] += 1
    rng = random.Random(f"{seed}:balance:{question}")
    chosen = balance(pool, max_ratio, rng)
    rng.shuffle(chosen)
    for r in chosen:
        by_source[r.source].used[r.label] += 1
    counts = Counter(r.label for r in chosen)
    warnings = [
        f"{question}: class {lb} has {counts.get(lb, 0)} rows (fit needs {MIN_ROWS_PER_CLASS})"
        for lb in sorted(question_labels(question))
        if counts.get(lb, 0) < MIN_ROWS_PER_CLASS
    ]
    return BuildResult(question, chosen, reports, skipped, before, max_ratio, warnings)


# ---------------------------------------------------------------- output


def csv_bytes(question: str, rows: Sequence[Row]) -> bytes:
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["state", question])
    for r in rows:
        w.writerow([r.state, r.label])
    return out.getvalue().encode("utf-8")


def _licence_summary(reports: Sequence[DatasetReport]) -> str:
    names = sorted({r.licence for r in reports if r.source})
    return names[0] if len(names) == 1 else "mixed: " + ", ".join(names)


def write_outputs(
    result: BuildResult, out_dir: Path, built_on: str, args: Mapping[str, Any]
) -> Path:
    """Write <q>.csv, one <q>.<source>.csv per dataset used and <q>.provenance.json; validate each
    with the importer's own parser. Returns the provenance path."""
    q = result.question
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = question_labels(q)
    used = [rep for rep in result.reports if rep.used]
    files: list[dict[str, Any]] = []

    def emit(name: str, rows: Sequence[Row], source: str, origin: str, licence: str) -> None:
        path = out_dir / name
        data = csv_bytes(q, rows)
        path.write_bytes(data)
        parsed = read_import_file(path, q, labels)  # raises ImportRefused on any problem
        want = Counter(r.label for r in rows)
        if len(parsed) != len(rows) or Counter(lb for _, lb in parsed) != want:
            raise ImportRefused(f"{name}: importer parse differs from what was written")
        files.append(
            {
                "file": name,
                "rows": len(rows),
                "rows_per_class": dict(sorted(want.items())),
                "sha256": hashlib.sha256(data).hexdigest(),
                "import_source": source,
                "import_origin": origin,
                "import_licence": licence,
            }
        )

    keep = {f"{q}.csv", f"{q}.provenance.json"} | {f"{q}.{rep.source}.csv" for rep in used}
    for stale in out_dir.glob(f"{q}.*.csv"):
        if stale.name not in keep:
            stale.unlink()
    emit(
        f"{q}.csv",
        result.rows,
        f"public-{q}",
        ", ".join(rep.url for rep in used),
        _licence_summary(used),
    )
    for rep in used:
        emit(
            f"{q}.{rep.source}.csv",
            [r for r in result.rows if r.source == rep.source],
            rep.source,
            rep.url,
            rep.licence,
        )
    counts = Counter(r.label for r in result.rows)
    prov = {
        "question": q,
        "built_on": built_on,
        "arguments": dict(sorted(args.items())),
        "state_shape": "triage_state(job, app, max_tokens=512): title, feature, app, stack, "
        "labels, ui_globs; stack=unknown, labels=[] (source labels are the answer), "
        "ui_globs=AppDef default, app=unknown (the source repo name with --keep-repo-name)",
        "rows": len(result.rows),
        "rows_per_class": dict(sorted(counts.items())),
        "rows_per_class_before_balancing": dict(sorted(result.before_balance.items())),
        "balancing": f"every class capped at {result.max_ratio} x the smallest class with at "
        f"least {MIN_ROWS_PER_CLASS} rows (0 = off); rows picked with a seeded shuffle",
        "dedupe": "exact state; same state with two labels dropped; at most "
        f"{NEAR_DUPLICATE_CAP} rows per digit-normalised title and label",
        "secrets": "rows with an email address, key or token pattern are dropped, not redacted",
        "datasets": [rep.as_json() for rep in result.reports],
        "skipped_datasets": result.skipped,
        "warnings": result.warnings,
        "files": files,
    }
    path = out_dir / f"{q}.provenance.json"
    path.write_text(json.dumps(prov, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8")
    return path


# ---------------------------------------------------------------- command line


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m swfactory.laya.public_data",
        description="Build Laya training CSVs from public datasets.",
    )
    p.add_argument("--question", default="all", help="kind, size, touches_ui or all")
    p.add_argument("--out-dir", type=Path, default=Path("var/laya/public"))
    p.add_argument("--limit-per-dataset", type=int, default=DEFAULT_LIMIT, help="max rows read")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--built-on", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--max-ratio", type=int, default=DEFAULT_MAX_RATIO, help="0 turns balancing off")
    p.add_argument(
        "--keep-repo-name",
        action="store_true",
        help="put the source repo name in `app` (default unknown: the repo must not be a shortcut)",
    )
    p.add_argument(
        "--include-synthetic", action="store_true", help="also use SWE-smith (synthetic issues)"
    )
    p.add_argument(
        "--offline-fixture", type=Path, metavar="DIR", help="read JSON fixtures, no network"
    )
    p.add_argument("--min-interval", type=float, default=1.0, help="seconds between requests")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ns.built_on):
        print("error: --built-on must look like 2026-11-20", file=sys.stderr)
        return 2
    if ns.question == NO_PUBLIC_SOURCE or ns.question.startswith("tier."):
        print(
            f"error: {ns.question} has no honest public source; label it from your own jobs",
            file=sys.stderr,
        )
        return 2
    if ns.question != "all" and ns.question not in QUESTIONS:
        print(f"error: --question must be one of {', '.join(QUESTIONS)}, all", file=sys.stderr)
        return 2
    source: RowSource = (
        FixtureSource(ns.offline_fixture)
        if ns.offline_fixture
        else HubSource(min_interval=ns.min_interval)
    )
    args = {
        "limit_per_dataset": ns.limit_per_dataset,
        "seed": ns.seed,
        "max_ratio": ns.max_ratio,
        "include_synthetic": ns.include_synthetic,
        "keep_repo_name": ns.keep_repo_name,
        "offline_fixture": bool(ns.offline_fixture),
    }
    failed = False
    for q in QUESTIONS if ns.question == "all" else (ns.question,):
        result = build_question(
            q,
            source,
            limit=ns.limit_per_dataset,
            seed=ns.seed,
            max_ratio=ns.max_ratio,
            include_synthetic=ns.include_synthetic,
            keep_repo_name=ns.keep_repo_name,
            log=lambda m: print(m, flush=True),
        )
        if not result.rows:
            print(f"{q}: no rows built; skipped {result.skipped}", file=sys.stderr)
            failed = True
            continue
        prov = write_outputs(result, ns.out_dir, ns.built_on, args)
        counts = Counter(r.label for r in result.rows)
        print(f"{q}: {len(result.rows)} rows {dict(sorted(counts.items()))} -> {prov.parent}")
        print(f"{q}: before balancing {dict(sorted(result.before_balance.items()))}")
        for w in result.warnings:
            print(f"warning: {w}")
        for s in result.skipped:
            print(f"skipped {s['dataset']}: {s['reason']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
