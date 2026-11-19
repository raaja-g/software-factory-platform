from swfactory.definition.models import AppDef
from swfactory.domain.types import JobKind
from swfactory.queue import branch_name, ci_requires_approval, slugify


def app_def(repo_kind: str, **extra) -> AppDef:
    base: dict = {"repo_kind": repo_kind, "repo_url": "https://github.com/acme/web"}
    if repo_kind == "local":
        base = {"repo_kind": "local", "repo_url": "/tmp/r", "local_path": "/tmp/r"}
    return AppDef.model_validate({**base, **extra})


def test_names():
    assert slugify("Add a /health endpoint!") == "add-a-health-endpoint"
    assert branch_name(7, "x") == "factory/007-x"
    assert slugify("x" * 60) == "x" * 40


def test_ci_requires_approval():
    assert ci_requires_approval(app_def("github"), JobKind.NORMAL) is True
    assert (
        ci_requires_approval(app_def("github", github={"ci_safe": True}), JobKind.NORMAL) is False
    )
    assert ci_requires_approval(app_def("github"), JobKind.BENCHMARK) is False
    assert ci_requires_approval(app_def("local"), JobKind.NORMAL) is False
