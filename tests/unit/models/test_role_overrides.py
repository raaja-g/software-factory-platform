from tests.unit.models.test_profiles import make_def

from swfactory.definition.models import RoleOverride
from swfactory.domain.types import Role, SelectionMode
from swfactory.models.profiles import resolve_profile

DEFN, APP, _ = make_def()


def _resolve(**overrides: RoleOverride):
    return resolve_profile(
        DEFN,
        APP,
        override="team-gemini",
        triage_profile=None,
        role_overrides={Role(k): v for k, v in overrides.items()},
    )[0]


def test_role_override_model():
    rm = _resolve(builder=RoleOverride(model="gemini-flash@vertex")).for_role(Role.BUILDER)
    assert rm.model == "gemini-flash@vertex"


def test_role_override_mode_keeps_model():
    base = resolve_profile(DEFN, APP, override="team-gemini", triage_profile=None)[0]
    rm = _resolve(builder=RoleOverride(mode=SelectionMode.LAYA)).for_role(Role.BUILDER)
    assert (rm.mode, rm.model) == (SelectionMode.LAYA, base.for_role(Role.BUILDER).model)


def test_role_override_engine_and_other_roles_untouched():
    base = resolve_profile(DEFN, APP, override="team-gemini", triage_profile=None)[0]
    got = _resolve(builder=RoleOverride(engine="opencode"))
    assert got.for_role(Role.BUILDER).engine == "opencode"
    assert got.for_role(Role.APPROVER) == base.for_role(Role.APPROVER)


def test_no_overrides_is_the_plain_profile():
    plain = resolve_profile(DEFN, APP, override="team-gemini", triage_profile=None)
    assert (
        resolve_profile(DEFN, APP, override="team-gemini", triage_profile=None, role_overrides={})
        == plain
    )
