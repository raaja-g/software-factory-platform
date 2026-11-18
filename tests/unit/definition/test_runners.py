import pytest
from pydantic import ValidationError

from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import RunnerDef, runner_spec


def load_defaults():
    r = load_definition(str(defaults_dir()))
    assert not r.errors, r.errors
    assert r.definition is not None
    return r.definition


def test_default_runners_load():
    std = runner_spec(load_defaults(), "standard")
    assert (std.image, std.cpus, std.memory_mb, std.pids) == (
        "swfactory/runner:0.1.0",
        2.0,
        4096,
        512,
    )
    assert runner_spec(load_defaults(), "web").image == "swfactory/runner-web:0.1.0"
    assert "registry.npmjs.org" in std.allowed_hosts
    assert isinstance(std.allowed_hosts, tuple)


def test_runner_max_work_gb():
    assert runner_spec(load_defaults(), "standard").max_work_gb == 20
    with pytest.raises(ValidationError):
        RunnerDef(image="swfactory/runner:0.1.0", max_work_gb=0)
