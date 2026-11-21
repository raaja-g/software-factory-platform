from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from swfactory.definition.loader import defaults_dir, init_definition_repo, load_definition
from swfactory.definition.models import AppDef, Definition
from swfactory.domain.types import FactoryMode, Role
from swfactory.improve.proposal import (
    FileChange,
    changed_keys,
    check_minimums,
    check_paths,
    check_redraft,
    check_scope,
    validate_proposal,
)


def load_defaults() -> Definition:
    r = load_definition(str(defaults_dir()))
    assert not r.errors, r.errors
    assert r.definition is not None
    return r.definition


SHIPPED = load_defaults()
_pay = AppDef(
    repo_kind="local",
    repo_url="pay",
    profile="team-gemini",
    sensitive=True,
    reviewers=("security", "ux", "ui", "code"),
)
BASE = SHIPPED.model_copy(
    update={
        "factory": SHIPPED.factory.model_copy(
            update={"apps": {**SHIPPED.factory.apps, "pay": _pay}}
        )
    }
)
P = (defaults_dir() / "profiles" / "team-gemini.yaml").read_text()
F = (defaults_dir() / "factory.yaml").read_text() + "\n"
F = F.replace(
    "  demo:\n",
    "  pay:\n    repo_kind: local\n    repo_url: pay\n    sensitive: true\n  demo:\n",
    1,
)
B = (defaults_dir() / "roles" / "builder.md").read_text()
REDRAFT = (
    B.split("---\n", 2)[0] + "---\n" + B.split("---\n", 2)[1] + "---\nCompletely new guidance.\n"
)
assert "job_usd: 10" in F


def mod(path: str, old: str = "a\n", new: str = "b\n", mode: str = "100644") -> FileChange:
    return FileChange(path, "M", None, mode, old, new)


def add(path: str, mode: str = "100644") -> FileChange:
    return FileChange(path, "A", None, mode, None, "x\n")


def delete(path: str) -> FileChange:
    return FileChange(path, "D", None, "000000", "x\n", None)


def with_field(text: str, role: str, field: str, value: str) -> str:
    d = yaml.safe_load(text)
    d["roles"][role][field] = value
    return yaml.safe_dump(d)


def prof_edit(defn: Definition, pname: str, role: str, **kw: Any) -> Definition:
    prof = defn.profiles[pname]
    r = Role(role)
    entry = prof.roles[r].model_copy(update=kw)
    newp = prof.model_copy(update={"roles": {**prof.roles, r: entry}})
    return defn.model_copy(update={"profiles": {**defn.profiles, pname: newp}})


def with_role(defn: Definition, pname: str, role: str, **kw: Any) -> Definition:
    if "fallback" in kw:
        kw["fallback"] = tuple(kw["fallback"])
    return prof_edit(defn, pname, role, **kw)


def with_budget(defn: Definition, **kw: Any) -> Definition:
    b = defn.factory.budgets.model_copy(update=kw)
    return defn.model_copy(update={"factory": defn.factory.model_copy(update={"budgets": b})})


def with_app(defn: Definition, name: str, **kw: Any) -> Definition:
    apps = {**defn.factory.apps, name: defn.factory.apps[name].model_copy(update=kw)}
    return defn.model_copy(update={"factory": defn.factory.model_copy(update={"apps": apps})})


def with_app_budget(defn: Definition, name: str, v: Decimal) -> Definition:
    return with_app(defn, name, budget_usd=v)


def with_reviewers(defn: Definition, name: str, rv: list[str]) -> Definition:
    return with_app(defn, name, reviewers=tuple(rv))


def with_judge(defn: Definition, model: str, fallback: list[str]) -> Definition:
    r = defn.model_copy(deep=True)
    for sn, sc in list(r.scorers.items()):
        j = sc.judge.model_copy(update={"model": model, "fallback": tuple(fallback)})
        r.scorers[sn] = sc.model_copy(update={"judge": j})
    d = prof_edit(r, "team-gemini", "judge", model=model, fallback=tuple(fallback))
    return d


def without(defn: Definition, profile_role: tuple[str, str], scorer: str) -> Definition:
    pn, rn = profile_role
    prof = defn.profiles[pn]
    newp = prof.model_copy(update={"roles": {k: v for k, v in prof.roles.items() if k != Role(rn)}})
    return defn.model_copy(
        update={
            "profiles": {**defn.profiles, pn: newp},
            "scorers": {k: v for k, v in defn.scorers.items() if k != scorer},
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "runners/standard.yaml",
        ".github/workflows/ci.yml",
        "automations/weekly.yaml",
        "benchmarks/v1-parity/suite.yaml",
        "roles/../../outside.md",
    ],
)
def test_disallowed_path_rejected(path: str) -> None:
    assert [v.rule for v in check_paths([mod(path)])] == ["path"]


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "roles\\x.md",
        "roles/x\x00.md",
        "roles/.git/x.md",
        "ROLES/x.md",
        "roles/sub/x.md",
        "roles/ｘ.md",
        "roles//x.md",
        "roles/x.md/",
    ],
)
def test_odd_paths_rejected(path: str) -> None:
    assert check_paths([mod(path)])


@pytest.mark.parametrize("mode", ["100755", "160000", "040000"])
def test_non_regular_mode_rejected(mode: str) -> None:
    assert [v.rule for v in check_paths([add("roles/x.md", mode=mode)])] == ["path"]


def test_rename_checks_both_ends() -> None:
    assert check_paths([FileChange("roles/x.md", "R", "webhooks/x.yaml", "100644", "", "")])


def test_factory_yaml_budget_only() -> None:
    ok = mod("factory.yaml", old=F, new=F.replace("job_usd: 10", "job_usd: 8"))
    assert check_paths([ok]) == []
    bad = mod("factory.yaml", old=F, new=F.replace("sensitive: true", "sensitive: false"))
    assert check_paths([bad])[0].message.startswith("apps.pay.sensitive")
    assert check_paths([FileChange("factory.yaml", "D", None, "000000", F, None)])
    assert check_paths([mod("factory.yaml", old=F, new="a: [")])


def test_changed_keys() -> None:
    assert changed_keys("a: {b: 1, c: [1, 2]}", "a: {b: 2, c: [1, 3, 4]}") == [
        "a.b",
        "a.c.1",
        "a.c.2",
    ]


def test_symlink_rejected() -> None:
    assert check_paths([add("roles/builder.md", mode="120000")])[0].rule == "symlink"


def test_append_only_role_rejected() -> None:
    assert (
        check_redraft([mod("roles/builder.md", old=B, new=B + "\n## Addendum\nAlso...")])[0].rule
        == "append_only"
    )


def test_redraft_and_delete_allowed() -> None:
    assert (
        check_redraft([mod("roles/builder.md", old=B, new=REDRAFT), delete("roles/reviewer-ux.md")])
        == []
    )


def test_other_role_file_out_of_scope() -> None:
    assert [v.rule for v in check_scope([mod("roles/reviewer-security.md")], Role.BUILDER)] == [
        "scope"
    ]


def test_other_role_profile_entry_out_of_scope() -> None:
    new = P.replace(
        "reviewer-code:     { engine: opencode, model: gemini-flash@vertex }",
        "reviewer-code:     { engine: opencode, model: gemini-pro@vertex }",
    )
    assert new != P
    v = check_scope([mod("profiles/team-gemini.yaml", old=P, new=new)], Role.BUILDER)
    assert v[0].message.startswith("roles.reviewer-code")


@pytest.mark.parametrize("field,value", [("engine", "direct"), ("mode", "shadow")])
def test_engine_and_mode_never_change(field: str, value: str) -> None:
    new = with_field(P, "reviewer-security", field, value)
    c = mod("profiles/team-gemini.yaml", old=P, new=new)
    assert check_scope([c], Role.REVIEWER_SECURITY)[0].rule == "scope"


def test_own_role_file_and_entry_in_scope() -> None:
    new = P.replace("fallback: [gemini-flash@vertex]", "fallback: [gemini-lite@vertex]")
    assert new != P
    c = [mod("roles/builder.md"), mod("profiles/team-gemini.yaml", old=P, new=new)]
    assert check_scope(c, Role.BUILDER) == []


def test_scorer_scope_by_applies_to() -> None:
    s = (defaults_dir() / "scorers" / "merge-readiness.md").read_text()
    c = mod("scorers/merge-readiness.md", old=s, new=s + "x")
    assert check_scope([c], Role.BUILDER) == []
    assert check_scope([c], Role.JUDGE)[0].rule == "scope"
    assert check_scope([mod("scorers/x.md", old="no frontmatter", new="none")], Role.BUILDER)


def test_unknown_path_out_of_scope() -> None:
    assert check_scope([mod("runners/x.yaml")], Role.BUILDER)[0].rule == "scope"


class TmpDef:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.before: dict[str, str] = {}

    def edit(self, rel: str, drop_role: str) -> None:
        f = self.path / rel
        self.before[rel] = f.read_text()
        d = yaml.safe_load(self.before[rel])
        del d["roles"][drop_role]
        f.write_text(yaml.safe_dump(d))

    def changes(self) -> list[FileChange]:
        return [
            FileChange(r, "M", None, "100644", old, (self.path / r).read_text())
            for r, old in self.before.items()
        ]


@pytest.fixture
def tmp_def(tmp_path: Path) -> TmpDef:
    p = tmp_path / "def"
    init_definition_repo(p)
    f = p / "factory.yaml"
    d = yaml.safe_load(f.read_text())
    d["apps"]["pay"] = {
        "repo_kind": "local",
        "repo_url": "pay",
        "sensitive": True,
        "profile": "team-gemini",
    }
    f.write_text(yaml.safe_dump(d))
    return TmpDef(p)


def test_security_reviewer_removed_from_sensitive_app_fails(tmp_def: TmpDef) -> None:
    tmp_def.edit("profiles/team-gemini.yaml", drop_role="reviewer-security")
    chk = validate_proposal(
        tmp_def.path,
        BASE,
        tmp_def.changes(),
        mode=FactoryMode.PERSONAL,
        role=Role.REVIEWER_SECURITY,
    )
    assert not chk.ok
    assert any("security" in v.message for v in chk.violations)


def test_validate_ok_when_unchanged(tmp_def: TmpDef) -> None:
    chk = validate_proposal(tmp_def.path, BASE, [], mode=FactoryMode.PERSONAL, role=Role.BUILDER)
    assert chk.ok, chk.violations


def test_validate_reports_load_errors(tmp_def: TmpDef) -> None:
    (tmp_def.path / "profiles" / "team-gemini.yaml").write_text("roles: [")
    chk = validate_proposal(tmp_def.path, BASE, [], mode=FactoryMode.PERSONAL, role=Role.BUILDER)
    assert not chk.ok and chk.violations[0].rule == "invalid"


def test_budget_above_cap_fails() -> None:
    assert check_minimums(BASE, with_budget(BASE, job_usd=Decimal("12")))[0].rule == "minimum"


def test_daily_budget_above_cap_fails() -> None:
    assert check_minimums(BASE, with_budget(BASE, daily_usd=Decimal("999")))


def test_app_budget_above_job_cap_fails() -> None:
    assert check_minimums(BASE, with_app_budget(BASE, "pay", Decimal("11")))


def test_required_reviewer_removed_fails() -> None:
    assert check_minimums(BASE, with_reviewers(BASE, "pay", ["ux", "ui", "code"]))


def test_sensitive_flag_must_stay() -> None:
    assert check_minimums(BASE, with_app(BASE, "pay", sensitive=False))


def test_profile_role_and_scorer_deletion_fail() -> None:
    d = without(BASE, profile_role=("claude-vertex", "approver"), scorer="merge-readiness")
    assert len(check_minimums(BASE, d)) == 2


def test_security_reviewer_same_family_as_builder_fails() -> None:
    d = with_role(BASE, "team-gemini", "reviewer-security", model="gemini-pro@vertex")
    assert "D17" in check_minimums(BASE, d)[0].message


def test_judge_same_family_as_builder_fails() -> None:
    assert check_minimums(BASE, with_judge(BASE, model="gemini-lite@vertex", fallback=[]))


def test_reviewer_strength_drop_fails() -> None:
    d = with_role(BASE, "claude-vertex", "reviewer-code", model="local@ollama")
    assert check_minimums(BASE, d)[0].rule == "minimum"


def test_experimental_fails() -> None:
    assert check_minimums(
        BASE, with_role(BASE, "claude-vertex", "builder", allow_experimental=True)
    )


def test_same_strength_model_swap_allowed() -> None:
    d = with_role(BASE, "claude-vertex", "reviewer-code", model="claude-opus@vertex")
    assert check_minimums(BASE, d) == []


def test_shipped_definition_meets_its_own_minimums() -> None:
    assert check_minimums(SHIPPED, SHIPPED) == []


def test_new_same_family_fallback_fails() -> None:
    d = with_role(
        SHIPPED,
        "team-gemini",
        "reviewer-security",
        fallback=["gemini-pro@vertex", "gemini-flash@vertex"],
    )
    assert any("D17" in v.message for v in check_minimums(SHIPPED, d))


def test_default_moved_into_builder_family_fails() -> None:
    d = with_role(
        SHIPPED,
        "team-gemini",
        "reviewer-security",
        model="gemini-pro@vertex",
        fallback=["claude-sonnet@vertex"],
    )
    assert "D17" in check_minimums(SHIPPED, d)[0].message


def test_improver_config_unchanged() -> None:
    f = BASE.factory.model_copy(
        update={
            "improver": BASE.factory.improver.model_copy(update={"max_cycle_usd": Decimal("999")})
        }
    )
    assert check_minimums(BASE, BASE.model_copy(update={"factory": f}))
