import json

import yaml

from swfactory.definition.models import RegistryDef
from swfactory.mirror.config import mirror_env, render_npm_config, render_pypi_routes

NPM_AR = RegistryDef(
    kind="npm",
    upstream="artifact_registry",
    url="https://us-npm.pkg.dev/acme/js/",
    scopes=("@acme",),
)
NPM_GH = RegistryDef(
    kind="npm", upstream="github_packages", url="https://npm.pkg.github.com/", scopes=("@acme-gh",)
)
PY_TOK = RegistryDef(
    kind="pypi",
    upstream="token",
    url="https://pypi.example.com/simple/",
    scopes=("acme-",),
    credential="REGISTRY_ACME_PY_TOKEN",
)
REGS = {"ar": NPM_AR, "gh": NPM_GH, "py": PY_TOK}
TOKENS = {"ar": "t-ar", "gh": "t-gh", "py": "t-py"}


def test_mirror_env():
    assert mirror_env() == {
        "NPM_CONFIG_REGISTRY": "http://pkg-mirror:4873",
        "PIP_INDEX_URL": "http://pkg-mirror:3141/root/pypi/+simple/",
        "PIP_TRUSTED_HOST": "pkg-mirror",
        "UV_DEFAULT_INDEX": "http://pkg-mirror:3141/root/pypi/+simple/",
        "UV_INSECURE_HOST": "pkg-mirror",
    }


def test_env_carries_no_credential():
    assert not any(t in " ".join(mirror_env().values()) for t in TOKENS.values())


def test_npm_publish_is_impossible():
    cfg = yaml.safe_load(render_npm_config(REGS, TOKENS))
    assert cfg["packages"] and all(
        p["publish"] == "$nobody" and p["unpublish"] == "$nobody" for p in cfg["packages"].values()
    )
    assert cfg["auth"]["htpasswd"]["max_users"] == -1


def test_npm_scopes_go_only_to_their_private_upstream():
    cfg = yaml.safe_load(render_npm_config(REGS, TOKENS))
    assert cfg["packages"]["@acme/*"]["proxy"] == "ar"
    assert cfg["packages"]["@acme-gh/*"]["proxy"] == "gh"
    assert cfg["packages"]["**"]["proxy"] == "npmjs"
    assert cfg["uplinks"]["npmjs"]["url"] == "https://registry.npmjs.org/"
    assert cfg["uplinks"]["ar"]["auth"] == {"type": "bearer", "token": "t-ar"}
    assert cfg["uplinks"]["ar"]["url"] == "https://us-npm.pkg.dev/acme/js/"
    assert list(cfg["packages"])[-1] == "**"  # scopes first: Verdaccio takes the first match


def test_npm_scope_without_a_token_never_falls_back_to_public():
    cfg = yaml.safe_load(render_npm_config(REGS, {"ar": "t-ar"}))
    assert cfg["packages"]["@acme-gh/*"] == {
        "access": "$all",
        "publish": "$nobody",
        "unpublish": "$nobody",
    }
    assert "gh" not in cfg["uplinks"]


def test_pypi_routes_by_name_prefix():
    routes = json.loads(render_pypi_routes(REGS, TOKENS))
    assert routes["routes"] == [
        {
            "prefix": "acme-",
            "index": "https://pypi.example.com/simple/",
            "auth": {"username": "__token__", "password": "t-py"},
        }
    ]
    assert routes["default"] == {"index": "https://pypi.org/simple/"}
    assert routes["upload"] is False


def test_pypi_prefix_without_a_token_is_denied():
    assert json.loads(render_pypi_routes(REGS, {}))["routes"] == [{"prefix": "acme-", "deny": True}]
