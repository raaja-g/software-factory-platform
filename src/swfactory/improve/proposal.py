"""Guardrails for an improver proposal (untrusted input): every check fails closed."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml

from swfactory.bench.report import ConfigSummary
from swfactory.definition.loader import LoadResult, _split_frontmatter, load_definition
from swfactory.definition.models import Definition, FactoryConfig, RoleProfile
from swfactory.domain.types import FactoryMode, Role
from swfactory.improve.signals import FailureSignal
from swfactory.models.profiles import model_family

ALLOWED_DIRS = ("roles/", "profiles/", "scorers/")
# Exact keys only: `budgets.*` would also open job_max_usd, step_max_usd and app_monthly_usd.
BUDGET_KEYS = (
    "budgets.job_usd",
    "budgets.daily_usd",
    "apps.*.budget_usd",
    "triage.rules.*.budget_usd",
)
BUDGET_FLOOR = Decimal("0.5")  # a budget may be cut to half of base, never to nothing

_EXT = {"roles/": ".md", "scorers/": ".md", "profiles/": ".yaml"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STRENGTH = {"local": 0, "cheap": 1, "mid": 2, "strong": 3}


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    status: Literal["A", "M", "D", "R"]
    old_path: str | None
    mode: str
    old_text: str | None
    new_text: str | None


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    rule: Literal["path", "symlink", "append_only", "invalid", "minimum", "scope"]
    message: str


@dataclass(frozen=True, slots=True)
class ProposalCheck:
    ok: bool
    load: LoadResult
    violations: tuple[Violation, ...]


# --------------------------------------------------------------------------
# changed_keys
# --------------------------------------------------------------------------


def _flatten(node: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(node, dict) and node:
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(node, list) and node:
        for i, v in enumerate(node):
            _flatten(v, f"{prefix}.{i}" if prefix else str(i), out)
    else:
        out[prefix] = node


def _load_yaml(text: str) -> Any:
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("top level of the yaml file is not a mapping")
    return data


def changed_keys(old_yaml: str, new_yaml: str) -> list[str]:
    """Dotted paths of differing leaves (list indexes as numbers); ValueError on bad yaml."""
    try:
        old, new = _load_yaml(old_yaml), _load_yaml(new_yaml)
    except yaml.YAMLError as e:
        raise ValueError(f"unparseable yaml: {e}") from e
    a: dict[str, Any] = {}
    b: dict[str, Any] = {}
    _flatten(old, "", a)
    _flatten(new, "", b)
    keys = [k for k in a if k not in b or a[k] != b[k]]
    keys += [k for k in b if k not in a]
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return sorted(out)


def _matches_budget(key: str) -> bool:
    parts = key.split(".")
    for pat in BUDGET_KEYS:
        pp = pat.split(".")
        if len(pp) == len(parts) and all(
            p == "*" or p == q for p, q in zip(pp, parts, strict=True)
        ):
            return True
    return False


# --------------------------------------------------------------------------
# check_paths
# --------------------------------------------------------------------------


def _bad_path(path: str | None) -> str | None:
    """Why `path` is never acceptable, or None. Allowlist is by exact path or anchored prefix."""
    if not isinstance(path, str) or not path:
        return "empty path"
    if "\x00" in path or "\\" in path:
        return "path has a NUL or backslash"
    if path.startswith("/"):
        return "absolute path"
    parts = path.split("/")
    if ".." in parts or "." in parts or "" in parts:
        return "path is not canonical"
    if posixpath.normpath(path) != path:
        return "path is not canonical"
    if any(p.lower() == ".git" or p.lower().startswith(".git") for p in parts):
        return "path inside .git"
    if path == "factory.yaml":
        return None
    for d in ALLOWED_DIRS:
        if path.startswith(d):
            name = path[len(d) :]
            if not _NAME_RE.match(name) or not name.endswith(_EXT[d]) or name == _EXT[d]:
                return f"not a plain {_EXT[d]} file directly under {d}"
            return None
    return "outside the allowlist (factory.yaml, roles/, profiles/, scorers/)"


def check_paths(changes: Sequence[FileChange]) -> list[Violation]:
    out: list[Violation] = []
    for c in changes:
        if c.status not in ("A", "M", "D", "R"):
            out.append(Violation(str(c.path), "path", f"unsupported change status {c.status!r}"))
            continue
        for p in (c.path, c.old_path):
            if p is None and p is c.old_path:
                continue
            why = _bad_path(p)
            if why:
                out.append(Violation(str(p), "path", why))
        if c.status != "D":
            if c.mode == "120000":
                out.append(Violation(c.path, "symlink", "symlinks are not allowed"))
            elif c.mode != "100644":
                out.append(Violation(c.path, "path", f"mode {c.mode} is not a regular file"))
        if c.path == "factory.yaml":
            if c.status != "M" or c.old_text is None or c.new_text is None:
                out.append(Violation(c.path, "path", "factory.yaml may only be modified"))
                continue
            try:
                keys = changed_keys(c.old_text, c.new_text)
            except ValueError as e:
                out.append(Violation(c.path, "path", str(e)))
                continue
            for k in keys:
                if not _matches_budget(k):
                    out.append(Violation(c.path, "path", f"{k}: only budget keys may change"))
    return out


# --------------------------------------------------------------------------
# check_redraft
# --------------------------------------------------------------------------


def _body(text: str) -> str:
    return _split_frontmatter(text)[1]


def check_redraft(changes: Sequence[FileChange]) -> list[Violation]:
    out: list[Violation] = []
    for c in changes:
        if c.status != "M" or not (
            c.path.startswith(("roles/", "scorers/")) and c.path.endswith(".md")
        ):
            continue
        if c.old_text is None or c.new_text is None:
            continue
        old, new = _body(c.old_text).strip(), _body(c.new_text).strip()
        if len(new) > len(old) and new.startswith(old):
            out.append(
                Violation(c.path, "append_only", "append-only change; redraft the whole guidance")
            )
    return out


# --------------------------------------------------------------------------
# check_scope
# --------------------------------------------------------------------------


def _frontmatter(text: str | None) -> dict[str, Any] | None:
    """The parsed frontmatter mapping, None when there is none; ValueError when unreadable."""
    if text is None:
        return None
    fm, _, _ = _split_frontmatter(text)
    if fm is None:
        return None
    try:
        data = yaml.safe_load(fm)
    except yaml.YAMLError as e:
        raise ValueError(f"unreadable frontmatter: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data


def _scorer_roles(text: str | None) -> set[str]:
    if text is None:
        return set()
    data = _frontmatter(text)
    if data is None:
        raise ValueError("no frontmatter")
    at = data.get("applies_to") or []
    if not isinstance(at, list):
        raise ValueError("applies_to is not a list")
    return {str(x) for x in at}


def _frontmatter_locked(path: str, old: str | None, new: str | None) -> Violation | None:
    """Only the body of a role or scorer file may change: the frontmatter must stay equal."""
    try:
        same = _frontmatter(old) == _frontmatter(new)
    except ValueError as e:
        return Violation(path, "scope", f"{path}: {e}")
    if not same:
        return Violation(
            path, "scope", f"{path}: only the body may change, not the frontmatter (settings)"
        )
    return None


def check_scope(changes: Sequence[FileChange], role: Role) -> list[Violation]:
    out: list[Violation] = []
    own = f"roles/{role.value}.md"
    for c in changes:
        paths = [p for p in (c.path, c.old_path) if p is not None]
        for p in paths:
            if p == "factory.yaml":
                continue
            if p.startswith("roles/"):
                if p != own:
                    out.append(Violation(p, "scope", f"{p} belongs to another role group"))
                elif c.status != "M" or c.old_text is None or c.new_text is None:
                    out.append(Violation(p, "scope", f"{p} may only be modified"))
                elif (lock := _frontmatter_locked(p, c.old_text, c.new_text)) is not None:
                    out.append(lock)
            elif p.startswith("profiles/"):
                if c.status != "M" or c.old_text is None or c.new_text is None:
                    out.append(Violation(p, "scope", "profiles may only be modified"))
                    continue
                try:
                    keys = changed_keys(c.old_text, c.new_text)
                except ValueError as e:
                    out.append(Violation(p, "scope", str(e)))
                    continue
                prefix = f"roles.{role.value}."
                for k in keys:
                    if not k.startswith(prefix):
                        out.append(Violation(p, "scope", f"{k} is outside {role.value}'s entry"))
                    elif k in (prefix + "engine", prefix + "mode"):
                        out.append(Violation(p, "scope", f"{k}: engine and mode never change"))
                break  # old_path and path name the same file here
            elif p.startswith("scorers/"):
                # A scorer that exists is judged by what it applied to BEFORE the proposal (an
                # edit of applies_to could otherwise grant itself the scope); only a new scorer
                # is judged by the scope it declares. An edit may change the rubric body only.
                try:
                    if c.status == "A":
                        roles = _scorer_roles(c.new_text)
                    else:
                        roles = _scorer_roles(c.old_text)
                except (ValueError, yaml.YAMLError) as e:
                    out.append(Violation(p, "scope", f"unreadable scorer frontmatter: {e}"))
                    break
                if role.value not in roles:
                    out.append(Violation(p, "scope", f"{p} does not apply to {role.value}"))
                elif c.status != "A" and c.new_text is not None:
                    lock = _frontmatter_locked(p, c.old_text, c.new_text)
                    if lock is not None:
                        out.append(lock)
                break
            else:
                out.append(Violation(p, "scope", f"{p} is outside every role group"))
    return out


# --------------------------------------------------------------------------
# check_minimums
# --------------------------------------------------------------------------


def _entry_aliases(rp: RoleProfile) -> list[str]:
    return [rp.model, *(t.model for t in rp.tiers), *rp.fallback]


def _fam(defn: Definition, alias: str) -> str:
    info = defn.factory.models.get(alias)
    return model_family(info.upstream_id) if info else f"alias:{alias}"


def _strength(defn: Definition, alias: str) -> int:
    info = defn.factory.models.get(alias)
    return _STRENGTH[info.strength] if info else 0


def _builder_families(defn: Definition, prof_name: str) -> set[str]:
    prof = defn.profiles.get(prof_name)
    if prof is None or Role.BUILDER not in prof.roles:
        return set()
    return {_fam(defn, a) for a in _entry_aliases(prof.roles[Role.BUILDER])}


def _collisions(defn: Definition, aliases: Sequence[str], fams: set[str]) -> set[tuple[str, str]]:
    """(alias, builder family) pairs: the aliases whose model family is a builder family."""
    return {(a, _fam(defn, a)) for a in aliases if _fam(defn, a) in fams}


def _d17(
    where: str,
    new_pairs: set[tuple[str, str]],
    old_pairs: set[tuple[str, str]],
    new_default: tuple[str, str] | None,
    old_default: tuple[str, str] | None,
) -> Violation | None:
    """D17 may only get better: the colliding pairs must not grow against the BASE's own.

    Both sides use their own definition's families and catalog, so moving the builder into a
    reviewer's or judge's family (or the reverse) shows up as a new pair, not as base state."""
    gained = new_pairs - old_pairs
    default_in = new_default is not None and old_default is None
    if gained or default_in:
        names = sorted({a for a, _ in gained} | ({new_default[0]} if new_default else set()))
        return Violation(
            where,
            "minimum",
            f"{where}: D17 cross-family rule weakened, {', '.join(names)} share the builder's "
            "model family",
        )
    return None


def _weaker_somewhere(
    defn_new: Definition, defn_base: Definition, new: RoleProfile, base: RoleProfile
) -> bool:
    """True when some position of the entry is weaker than the base's same position.

    The primary is held to the base primary, each tier and fallback to the base entry's tier or
    fallback at the same position, and an added position to the base's weakest. A weak base
    fallback therefore never lets the primary drop."""
    if _strength(defn_new, new.model) < _strength(defn_base, base.model):
        return True
    weakest = min(_strength(defn_base, a) for a in _entry_aliases(base))
    lists = (
        ([t.model for t in new.tiers], [t.model for t in base.tiers]),
        (list(new.fallback), list(base.fallback)),
    )
    for news, bases in lists:
        for i, alias in enumerate(news):
            bound = _strength(defn_base, bases[i]) if i < len(bases) else weakest
            if _strength(defn_new, alias) < bound:
                return True
    return False


def _app_profiles(base: Definition, app_name: str) -> set[str]:
    app = base.factory.apps[app_name]
    names = set(app.allowed_profiles)
    if app.profile:
        names.add(app.profile)
    return names or {base.factory.defaults.profile}


def _cut_only(path: str, new: Decimal, base: Decimal) -> Violation | None:
    """A budget may be cut to at most half of base: never raised, never zero."""
    if new > base:
        return Violation(path, "minimum", f"{path} {new} is above the cap {base}")
    if new <= 0 or new < base * BUDGET_FLOOR:
        return Violation(
            path,
            "minimum",
            f"{path} {new} is below half of the base {base} (budgets are never cut to nothing)",
        )
    return None


def _budget_violations(bf: FactoryConfig, pf: FactoryConfig) -> list[Violation]:
    out: list[Violation] = []

    def add(v: Violation | None) -> None:
        if v is not None:
            out.append(v)

    add(_cut_only("budgets.job_usd", pf.budgets.job_usd, bf.budgets.job_usd))
    add(_cut_only("budgets.daily_usd", pf.budgets.daily_usd, bf.budgets.daily_usd))
    locked = {"job_usd", "daily_usd"}
    if pf.budgets.model_dump(exclude=locked) != bf.budgets.model_dump(exclude=locked):
        out.append(
            Violation(
                "budgets", "minimum", "budgets: only job_usd and daily_usd may change (cut only)"
            )
        )
    cap: Decimal = bf.budgets.job_usd
    for name, app in pf.apps.items():
        if app.budget_usd is not None and app.budget_usd > cap:
            out.append(
                Violation(
                    f"apps.{name}.budget_usd",
                    "minimum",
                    f"apps.{name}.budget_usd {app.budget_usd} is above job_usd {cap}",
                )
            )
        bapp = bf.apps.get(name)
        if bapp is not None:
            before = bapp.budget_usd if bapp.budget_usd is not None else bf.budgets.job_usd
            after = app.budget_usd if app.budget_usd is not None else pf.budgets.job_usd
            add(_cut_only(f"apps.{name}.budget_usd", after, before))
    if len(pf.triage.rules) != len(bf.triage.rules):
        out.append(Violation("triage.rules", "minimum", "triage rules may not be added or removed"))
    for i, rule in enumerate(pf.triage.rules):
        if rule.budget_usd is not None and rule.budget_usd > cap:
            out.append(
                Violation(
                    f"triage.rules.{i}.budget_usd",
                    "minimum",
                    f"triage.rules.{i}.budget_usd {rule.budget_usd} is above job_usd {cap}",
                )
            )
        if i < len(bf.triage.rules):
            brule = bf.triage.rules[i]
            before = brule.budget_usd if brule.budget_usd is not None else bf.budgets.job_usd
            after = rule.budget_usd if rule.budget_usd is not None else pf.budgets.job_usd
            add(_cut_only(f"triage.rules.{i}.budget_usd", after, before))
    return out


def check_minimums(base: Definition, proposed: Definition) -> list[Violation]:
    out: list[Violation] = []

    def v(path: str, msg: str) -> None:
        out.append(Violation(path, "minimum", msg))

    bf, pf = base.factory, proposed.factory
    out += _budget_violations(bf, pf)
    for name, bapp in bf.apps.items():
        papp = pf.apps.get(name)
        if papp is None:
            v(f"apps.{name}", f"apps.{name} was removed")
            continue
        missing = set(bapp.reviewers) - set(papp.reviewers)
        if missing:
            v(f"apps.{name}.reviewers", f"apps.{name}.reviewers lost {sorted(missing)}")
        if bapp.sensitive:
            if not papp.sensitive:
                v(f"apps.{name}.sensitive", f"apps.{name}.sensitive must stay true")
            if "security" not in papp.reviewers:
                v(
                    f"apps.{name}.reviewers",
                    f"apps.{name} is sensitive and needs the security reviewer",
                )
            for pn in sorted(_app_profiles(proposed, name)):
                prof = proposed.profiles.get(pn)
                if prof is None or Role.REVIEWER_SECURITY not in prof.roles:
                    v(
                        f"profiles.{pn}.reviewer-security",
                        f"profiles.{pn} runs sensitive app {name} and needs reviewer-security",
                    )

    for pn, bprof in base.profiles.items():
        pprof = proposed.profiles.get(pn)
        if pprof is None:
            v(f"profiles.{pn}", f"profile {pn} was removed")
            continue
        for r in bprof.roles:
            if r not in pprof.roles:
                v(f"profiles.{pn}.{r.value}", f"profiles.{pn}.{r.value} was removed")
    for sn in base.scorers:
        if sn not in proposed.scorers:
            v(f"scorers.{sn}", f"scorer {sn} was removed")
    if bf.improver != pf.improver:
        v("improver", "improver config must not change")

    for pn, pprof in proposed.profiles.items():
        bprof = base.profiles.get(pn)
        pfams = _builder_families(proposed, pn)
        bfams = _builder_families(base, pn)
        for role, rp in pprof.roles.items():
            where = f"profiles.{pn}.{role.value}"
            brp = bprof.roles.get(role) if bprof else None
            if role in (Role.REVIEWER_SECURITY, Role.JUDGE):
                viol = _d17(
                    where,
                    _collisions(proposed, _entry_aliases(rp), pfams),
                    _collisions(base, _entry_aliases(brp), bfams) if brp else set(),
                    _first(_collisions(proposed, [rp.model], pfams)),
                    _first(_collisions(base, [brp.model], bfams)) if brp else None,
                )
                if viol:
                    out.append(viol)
            if brp is not None and _weaker_somewhere(proposed, base, rp, brp):
                v(where, f"{where}: model strength dropped below the base entry's")
            if rp.allow_experimental and not (brp is not None and brp.allow_experimental):
                v(where, f"{where}: allow_experimental may not be turned on")

    # scorer judges: same D17 rule against the builder families of every profile
    all_new: set[str] = set()
    all_old: set[str] = set()
    for pn in proposed.profiles:
        all_new |= _builder_families(proposed, pn)
    for pn in base.profiles:
        all_old |= _builder_families(base, pn)
    for sn, sc in proposed.scorers.items():
        bsc = base.scorers.get(sn)
        where = f"scorers.{sn}"
        new_aliases = [sc.judge.model, *sc.judge.fallback]
        viol = _d17(
            where,
            _collisions(proposed, new_aliases, all_new),
            _collisions(base, [bsc.judge.model, *bsc.judge.fallback], all_old) if bsc else set(),
            _first(_collisions(proposed, [sc.judge.model], all_new)),
            _first(_collisions(base, [bsc.judge.model], all_old)) if bsc else None,
        )
        if viol:
            out.append(viol)
    out += _scorer_locks(base, proposed)
    return out


def _first(pairs: set[tuple[str, str]]) -> tuple[str, str] | None:
    return next(iter(pairs), None)


def _scorer_locks(base: Definition, proposed: Definition) -> list[Violation]:
    """A scorer edit may change the rubric text only, and never a rubric the benchmark uses."""
    out: list[Violation] = []
    used = {
        name
        for defn in (base, proposed)
        for suite in defn.benchmarks.values()
        for task in suite.tasks.values()
        for name in task.scorers
    }
    for sn, bsc in base.scorers.items():
        psc = proposed.scorers.get(sn)
        if psc is None or psc == bsc:
            continue
        where = f"scorers.{sn}"
        if psc.model_dump(exclude={"rubric"}) != bsc.model_dump(exclude={"rubric"}):
            out.append(
                Violation(
                    where,
                    "minimum",
                    f"{where}: only the rubric text may change (labels, passing_score, "
                    "sampling_rate, judge and applies_to are locked)",
                )
            )
        if sn in used:
            out.append(
                Violation(
                    where,
                    "minimum",
                    f"{where}: a scorer used by a benchmark task cannot be edited by the improver",
                )
            )
    return out


# --------------------------------------------------------------------------
# validate_proposal
# --------------------------------------------------------------------------


def validate_proposal(
    workdir: Path,
    base: Definition,
    changes: Sequence[FileChange],
    *,
    mode: FactoryMode,
    role: Role,
) -> ProposalCheck:
    violations: list[Violation] = []
    load = load_definition(str(workdir), mode=mode)
    for e in load.errors:
        violations.append(Violation(e.file, "invalid", str(e)))
    violations += check_paths(changes)
    violations += check_redraft(changes)
    violations += check_scope(changes, role)
    if load.definition is not None:
        violations += check_minimums(base, load.definition)
    elif not load.errors:
        violations.append(Violation("", "invalid", "definition did not load"))
    return ProposalCheck(not violations, load, tuple(violations))


# --------------------------------------------------------------------------
# compare, describe_model_changes, render_improvement_pr
# --------------------------------------------------------------------------

UNMEASURED_ROLES = frozenset(
    {
        Role.REVIEWER_SECURITY,
        Role.REVIEWER_CODE,
        Role.REVIEWER_UX,
        Role.REVIEWER_UI,
        Role.APPROVER,
        Role.JUDGE,
    }
)
REGRESSIONS_HEADING = "## Regressions addressed"
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Comparison:
    rows: tuple[tuple[ConfigSummary, ConfigSummary], ...]
    result: Literal["better", "same", "worse", "unmeasured", "inconclusive"]
    common_tasks: int = 0


MIN_COMMON_TASKS = 3  # fewer tasks done on both sides than this say nothing
QUALITY_MARGIN_TASKS = 2  # a one-task flip is noise (one repetition per task), two are a signal
COST_MARGIN = Decimal("0.10")  # "cheaper" means at least this much cheaper


def exercised_by_benchmark(
    defn: Definition, suite: str, role: Role, changes: Sequence[FileChange]
) -> bool:
    """Does the benchmarked configuration (the suite's first) actually run what was changed?

    A role file reaches every configuration. A profile edit only counts when it is in the profile
    the configuration uses and the configuration does not override the changed role's model. A
    scorer or factory.yaml edit changes nothing the hidden checks measure."""
    sdef = defn.benchmarks.get(suite)
    if sdef is None or not sdef.configurations:
        return False
    first = sdef.configurations[0]
    override = first.roles.get(role)
    touched = False
    for c in changes:
        if c.path.startswith("profiles/"):
            name = c.path[len("profiles/") :].removesuffix(".yaml")
            if name != first.profile:
                continue
            if override is not None and override.model is not None:
                continue  # the configuration pins the model the profile edit changes
            touched = True
        elif c.path.startswith("roles/"):
            touched = True
    return touched


def compare(
    before: Sequence[ConfigSummary],
    after: Sequence[ConfigSummary],
    *,
    role: Role | None = None,
    exercised: bool = True,
) -> Comparison:
    after_by = {c.name: c for c in after}
    rows = tuple((b, after_by[b.name]) for b in before if b.name in after_by)
    q_before: list[float] = []
    q_after: list[float] = []
    common_all: set[str] = set()
    fewest: int | None = None
    for b, a in rows:
        common = sorted(set(b.task_pass) & set(a.task_pass))
        if not common:
            continue
        common_all |= set(common)
        fewest = len(common) if fewest is None else min(fewest, len(common))
        q_before.append(sum(b.task_pass[t] for t in common) / len(common))
        q_after.append(sum(a.task_pass[t] for t in common) / len(common))
    result: Literal["better", "same", "worse", "unmeasured", "inconclusive"] = "inconclusive"
    if q_before and fewest is not None and fewest >= MIN_COMMON_TASKS:
        qb, qa = sum(q_before) / len(q_before), sum(q_after) / len(q_after)
        costs = [(b.mean_cost_usd, a.mean_cost_usd) for b, a in rows]
        known = [(x, y) for x, y in costs if x is not None and y is not None]
        cb = sum((x for x, _ in known), Decimal(0)) / len(known) if known else None
        ca = sum((y for _, y in known), Decimal(0)) / len(known) if known else None
        margin = QUALITY_MARGIN_TASKS / fewest - _EPS
        if qa - qb >= margin:
            result = "better"
        elif qb - qa >= margin:
            result = "worse"
        elif abs(qa - qb) > _EPS:
            result = "inconclusive"  # a one-task flip
        elif cb is not None and ca is not None and ca < cb * (1 - COST_MARGIN):
            result = "better"
        else:
            result = "same"
    if result != "worse" and (role in UNMEASURED_ROLES or not exercised):
        result = "unmeasured"
    return Comparison(rows, result, len(common_all))


@dataclass(frozen=True, slots=True)
class PrContext:
    role: Role
    suite: str
    summary: str
    signals: tuple[FailureSignal, ...]
    changes: tuple[FileChange, ...]
    comparison: Comparison
    improver_cost_usd: Decimal
    bench_cost_usd: tuple[Decimal, Decimal]
    dashboard_url: str
    model_changes: tuple[str, ...] = ()


def _info(defn: Definition, alias: str) -> str:
    info = defn.factory.models.get(alias)
    if info is None:
        return "unknown, unknown"
    return f"{model_family(info.upstream_id)}, {info.strength}"


def describe_model_changes(base: Definition, proposed: Definition) -> tuple[str, ...]:
    out: list[str] = []

    def line(where: str, old: object, new: object, alias: str | None) -> None:
        if old == new:
            return
        tail = f" ({_info(proposed, alias)})" if alias is not None else ""
        out.append(f"- {where}: {old} \u2192 {new}{tail}")

    for pn, pprof in proposed.profiles.items():
        bprof = base.profiles.get(pn)
        if bprof is None:
            continue
        for role, rp in pprof.roles.items():
            brp = bprof.roles.get(role)
            if brp is None:
                continue
            w = f"profiles.{pn}.{role.value}"
            line(f"{w}.model", brp.model, rp.model, rp.model)
            bt = {t.label: t.model for t in brp.tiers}
            nt = {t.label: t.model for t in rp.tiers}
            for lab in sorted(set(bt) | set(nt)):
                o, n = bt.get(lab, "(none)"), nt.get(lab, "(none)")
                line(f"{w}.tiers.{lab}.model", o, n, n if lab in nt else None)
            for i in range(max(len(brp.fallback), len(rp.fallback))):
                o = brp.fallback[i] if i < len(brp.fallback) else "(none)"
                n = rp.fallback[i] if i < len(rp.fallback) else "(none)"
                line(f"{w}.fallback.{i}", o, n, n if i < len(rp.fallback) else None)
            line(f"{w}.allow_experimental", brp.allow_experimental, rp.allow_experimental, None)
    for sn, sc in proposed.scorers.items():
        bsc = base.scorers.get(sn)
        if bsc is None:
            continue
        w = f"scorers.{sn}.judge"
        line(f"{w}.model", bsc.judge.model, sc.judge.model, sc.judge.model)
        for i in range(max(len(bsc.judge.fallback), len(sc.judge.fallback))):
            o = bsc.judge.fallback[i] if i < len(bsc.judge.fallback) else "(none)"
            n = sc.judge.fallback[i] if i < len(sc.judge.fallback) else "(none)"
            line(f"{w}.fallback.{i}", o, n, n if i < len(sc.judge.fallback) else None)
    return tuple(sorted(out))


def _clean(text: object, limit: int = 400) -> str:
    """One safe line of agent text for markdown: no control chars, no mentions, no pipes."""
    t = "".join(ch if ch.isprintable() else " " for ch in str(text))
    t = " ".join(t.split())
    t = t.replace("@", "@\u200b").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    return t[:limit]


def _defang(text: str) -> str:
    """Break what GitHub would act on in text it should only show: issue references (so no
    closing keyword can fire) and URLs. Mentions and html are handled by the callers."""
    return text.replace("#", "#\u200b").replace("://", ":\u200b//")


def _fenced(text: str) -> str:
    """Multi-line agent text as one code block: inert whatever it contains.

    The fence is one backtick longer than the longest backtick run in the text, so the text can
    neither close it early nor swallow the sections after it."""
    raw = "\n".join(
        "".join(ch if ch.isprintable() else " " for ch in ln) for ln in str(text).splitlines()
    )
    t = raw.replace("@", "@\u200b").replace("<", "&lt;").replace(">", "&gt;")
    t = _defang(t).strip() or "(empty)"
    longest = max((len(m) for m in re.findall(r"`+", t)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{t}\n{fence}"


def _code(text: str) -> str:
    return "`" + _defang(_clean(text, 200)).replace("`", "'") + "`"


def _pct(v: float | None) -> str:
    return "\u2013" if v is None else f"{v * 100:.0f}%"


def _fmt(v: float | None, spec: str) -> str:
    return "\u2013" if v is None else format(v, spec)


def _usd(v: Decimal) -> str:
    return f"${v.quantize(Decimal('0.01'))}"


def _arrow(a: str, b: str) -> str:
    return f"{a} \u2192 {b}"


def _dash_base(url: str) -> str | None:
    u = url.strip().rstrip("/")
    if re.match(r"^https?://[A-Za-z0-9._~:/\[\]-]+$", u):
        return u
    return None


def _render(ctx: PrContext, n_signals: int, summary_limit: int | None) -> tuple[str, str]:
    """Machine-computed sections first (host numbers, host-built links), agent text last.

    The agent's summary is the one free-text field; it only ever appears inside a code fence under
    a heading that says so. Signal details are code spans. No agent text reaches the title."""
    cmp = ctx.comparison
    unmeasured = cmp.result == "unmeasured"
    reviewer = ctx.role in UNMEASURED_ROLES
    verdict = cmp.result
    if unmeasured:
        verdict = "review strictness not measured" if reviewer else "not measured by this benchmark"
    title = f"Improve {ctx.role.value} guidance ({_clean(ctx.suite, 80)}: {verdict})"
    summary = str(ctx.summary).strip()
    if summary_limit is not None and len(summary) > summary_limit:
        summary = summary[:summary_limit].rstrip() + " ..."
    base_url = _dash_base(ctx.dashboard_url)
    parts = [f"## Benchmark: {_clean(ctx.suite, 80)}", ""]
    parts.append("| Configuration | Hidden checks | Judge pass | Rounds | Cost | Time |")
    parts.append("| --- | --- | --- | --- | --- | --- |")
    for b, a in cmp.rows:
        parts.append(
            "| "
            + " | ".join(
                [
                    _clean(b.name, 80),
                    _arrow(_pct(b.hidden_pass_rate), _pct(a.hidden_pass_rate)),
                    _arrow(_pct(b.judge_pass_rate), _pct(a.judge_pass_rate)),
                    _arrow(_fmt(b.mean_rounds, ".1f"), _fmt(a.mean_rounds, ".1f")),
                    _arrow(_usd(b.total_cost_usd), _usd(a.total_cost_usd)),
                    _arrow(
                        _fmt(b.median_duration_s, ".0f") + "s",
                        _fmt(a.median_duration_s, ".0f") + "s",
                    ),
                ]
            )
            + " |"
        )
    parts.append("")
    if unmeasured and reviewer:
        parts.append(
            "Hidden checks do not measure review strictness; judge this change by reading it."
        )
    elif unmeasured:
        parts.append(
            "The benchmarked configuration does not run what this change edits; "
            "judge this change by reading it."
        )
    elif cmp.result == "inconclusive":
        parts.append(
            f"Result: inconclusive over {cmp.common_tasks} common tasks "
            "(too few tasks or too small a difference to call)."
        )
    else:
        parts.append(f"Result: {cmp.result} over {cmp.common_tasks} common tasks.")
    total = ctx.improver_cost_usd + ctx.bench_cost_usd[0] + ctx.bench_cost_usd[1]
    parts += [
        "",
        "## Cost",
        "",
        f"- Improver run: {_usd(ctx.improver_cost_usd)}",
        f"- Benchmark before: {_usd(ctx.bench_cost_usd[0])}",
        f"- Benchmark after: {_usd(ctx.bench_cost_usd[1])}",
        f"Total: {_usd(total)}",
        "",
        "## Files changed",
        "",
    ]
    for c in ctx.changes:
        parts.append(f"- {c.status} {_code(c.path)}")
    parts += [
        "",
        "## Guardrails",
        "",
        "Paths, definition validation and minimums passed. "
        "The improver never merges; a human decides.",
        "",
    ]
    # model lines come from describe_model_changes (validated aliases), so no mention escaping
    parts += [
        "".join(ch if ch.isprintable() else " " for ch in m)
        .replace("<", "&lt;")
        .replace(">", "&gt;")[:600]
        for m in ctx.model_changes
    ]
    parts += ["", REGRESSIONS_HEADING, ""]
    for s in ctx.signals[:n_signals]:
        ref = _clean(s.job_ref, 80).replace("[", "(").replace("]", ")")
        link = f"[{ref}]({base_url}/#/job/{s.job_id})" if base_url else ref
        parts.append(f"- {link} {_clean(s.kind, 60)}: {_code(s.detail)}")
    if len(ctx.signals) > n_signals:
        parts.append(f"- ... {len(ctx.signals) - n_signals} more signals omitted")
    parts += [
        "",
        "## Untrusted agent text",
        "",
        "The text below was written by the improver agent. It is shown as plain text and nothing "
        "in it is verified; the numbers above are the factory's own.",
        "",
        _fenced(summary),
    ]
    return title, "\n".join(parts).rstrip() + "\n"


def render_improvement_pr(ctx: PrContext, *, limit: int = 60_000) -> tuple[str, str]:
    """Title and body for the improvement PR; trims signals first, then the summary."""
    title, body = _render(ctx, len(ctx.signals), None)
    if len(body) <= limit:
        return title, body
    lo, hi = 0, len(ctx.signals)
    while lo < hi:  # most signals that still fit
        mid = (lo + hi + 1) // 2
        if len(_render(ctx, mid, None)[1]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    title, body = _render(ctx, lo, None)
    if len(body) <= limit:
        return title, body
    cut = len(str(ctx.summary).strip())
    while cut > 0:
        cut //= 2
        title, body = _render(ctx, 0, cut)
        if len(body) <= limit:
            return title, body
    return title, body[:limit]
