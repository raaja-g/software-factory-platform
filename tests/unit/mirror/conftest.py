import pytest

from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import RegistryDef


def _defn_with_registries(regs: dict[str, RegistryDef]):
    r = load_definition(str(defaults_dir()))
    assert r.definition is not None, r.errors
    d = r.definition
    return d.model_copy(update={"factory": d.factory.model_copy(update={"registries": regs})})


@pytest.fixture
def defn_with_registries():
    return _defn_with_registries
