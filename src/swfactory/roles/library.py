"""Role files: parse, validate, and look up the effective role at a definition sha."""

import importlib.resources
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from swfactory.definition.models import RoleFile
from swfactory.domain.types import Role, ToolPolicy


def _default_roles_dir() -> Path:
    packaged = Path(str(importlib.resources.files("swfactory") / "_defaults")) / "roles"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "defaults" / "roles"


DEFAULT_ROLES_DIR = _default_roles_dir()
PLACEHOLDERS = ("output_instruction", "in_dir", "workdir", "round")
_ALLOWED_KEYS = {"role", "description", "inputs", "output", "tool_policy", "contract"}
_REQUIRED_KEYS = ("role", "output", "tool_policy", "contract")
_CONTRACTS = {"triage", "requirements", "spec", "verdict", "decision", "none"}
_TOKEN = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_ORDER = (ToolPolicy.NONE, ToolPolicy.READ_ONLY, ToolPolicy.READ_RUN, ToolPolicy.WRITE)


class RoleFileError(ValueError):
    pass


@dataclass(frozen=True)
class RoleSpec:
    role: Role
    description: str
    inputs: tuple[str, ...]
    output: str
    tool_policy: ToolPolicy
    contract: str
    body: str


def spec_from_role_file(rf: RoleFile, *, role: Role) -> RoleSpec:
    fm = rf.frontmatter
    unknown = sorted(set(fm) - _ALLOWED_KEYS)
    if unknown:
        raise RoleFileError(f"unknown frontmatter key(s): {', '.join(map(str, unknown))}")
    missing = [k for k in _REQUIRED_KEYS if k not in fm]
    if missing:
        raise RoleFileError(f"missing frontmatter key(s): {', '.join(missing)}")
    if fm["role"] != role.value:
        raise RoleFileError(f"role is {fm['role']!r}, expected {role.value!r}")
    try:
        policy = ToolPolicy(fm["tool_policy"])
    except ValueError:
        raise RoleFileError(f"unknown tool_policy {fm['tool_policy']!r}") from None
    if fm["contract"] not in _CONTRACTS:
        raise RoleFileError(f"unknown contract {fm['contract']!r}")
    inputs = fm.get("inputs") or []
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        raise RoleFileError("inputs must be a list of strings")
    if not isinstance(fm["output"], str) or not fm["output"]:
        raise RoleFileError("output must be a non-empty string")
    for token in _TOKEN.findall(rf.body):
        if token not in PLACEHOLDERS:
            raise RoleFileError(f"unknown placeholder {{{{{token}}}}}")
    return RoleSpec(
        role=role,
        description=str(fm.get("description") or ""),
        inputs=tuple(inputs),
        output=fm["output"],
        tool_policy=policy,
        contract=fm["contract"],
        body=rf.body,
    )


def parse_role_file(text: str, *, role: Role) -> RoleSpec:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise RoleFileError("role file must start with a --- frontmatter block")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            try:
                data: Any = yaml.safe_load("".join(lines[1:i]))
            except yaml.YAMLError as e:
                raise RoleFileError(f"frontmatter is not valid YAML: {e}") from None
            if not isinstance(data, dict):
                raise RoleFileError("frontmatter must be a mapping")
            return spec_from_role_file(
                RoleFile(frontmatter=data, body="".join(lines[i + 1 :])), role=role
            )
    raise RoleFileError("frontmatter block is not closed with ---")


def check_override(default: RoleSpec, override: RoleSpec) -> None:
    for field in ("role", "output", "tool_policy", "contract"):
        if getattr(default, field) != getattr(override, field):
            raise RoleFileError(f"override may not change {field}")


def effective_tool_policy(role_policy: ToolPolicy, requested: ToolPolicy) -> ToolPolicy:
    """The narrower of the two: a caller may narrow a role's policy, never widen it."""
    return min(role_policy, requested, key=_ORDER.index)


def _no_overrides(sha: str) -> Mapping[Role, RoleFile]:
    return {}


class RoleLibrary:
    def __init__(
        self,
        defaults_dir: Path = DEFAULT_ROLES_DIR,
        overrides_for: Callable[[str], Mapping[Role, RoleFile]] = _no_overrides,
    ) -> None:
        self._dir = Path(defaults_dir)
        self._overrides_for = overrides_for
        self._defaults: dict[Role, RoleSpec] = {}
        self._cache: dict[tuple[str, Role], RoleSpec] = {}

    def _default(self, role: Role) -> RoleSpec:
        if role not in self._defaults:
            path = self._dir / f"{role.value}.md"
            self._defaults[role] = parse_role_file(path.read_text(encoding="utf-8"), role=role)
        return self._defaults[role]

    def get(self, role: Role, *, definition_sha: str) -> RoleSpec:
        key = (definition_sha, role)
        if key not in self._cache:
            default = self._default(role)
            rf = self._overrides_for(definition_sha).get(role)
            if rf is None:
                spec = default
            else:
                spec = spec_from_role_file(rf, role=role)
                check_override(default, spec)
            self._cache[key] = spec
        return self._cache[key]
