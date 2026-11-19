"""Mirror URLs, the sandbox environment that points at them, and the config renderers."""

import json
from collections.abc import Mapping

import yaml

from swfactory.definition.models import RegistryDef

MIRROR_HOST = "pkg-mirror"  # port numbers: M0 spike S13 must confirm
NPM_PORT = 4873
PYPI_PORT = 3141
NPM_URL = f"http://{MIRROR_HOST}:{NPM_PORT}"
PYPI_URL = f"http://{MIRROR_HOST}:{PYPI_PORT}/root/pypi/+simple/"
PYPI_USER = {"artifact_registry": "oauth2accesstoken", "token": "__token__"}
NPMJS_URL = "https://registry.npmjs.org/"
PYPI_PUBLIC_INDEX = "https://pypi.org/simple/"

_NOBODY = {"access": "$all", "publish": "$nobody", "unpublish": "$nobody"}


def mirror_env() -> dict[str, str]:
    """What a sandbox sees: the mirror's URLs and no credential."""
    return {
        "NPM_CONFIG_REGISTRY": NPM_URL,
        "PIP_INDEX_URL": PYPI_URL,
        "PIP_TRUSTED_HOST": MIRROR_HOST,
        "UV_DEFAULT_INDEX": PYPI_URL,
        "UV_INSECURE_HOST": MIRROR_HOST,
    }


def render_npm_config(registries: Mapping[str, RegistryDef], tokens: Mapping[str, str]) -> str:
    """Verdaccio config. A scope whose registry has no token gets no proxy: it answers 404 and
    never falls through to the public registry."""
    uplinks: dict[str, dict] = {
        "npmjs": {"url": NPMJS_URL, "cache": True, "maxage": "10m", "max_fails": 3}
    }
    scope_entries: dict[str, dict] = {}
    for name, reg in sorted(registries.items()):
        if reg.kind != "npm":
            continue
        has_token = name in tokens
        if has_token:
            uplinks[name] = {"url": reg.url, "auth": {"type": "bearer", "token": tokens[name]}}
        for scope in reg.scopes:
            entry = dict(_NOBODY)
            if has_token:
                entry["proxy"] = name
            scope_entries[f"{scope}/*"] = entry
    packages = {k: scope_entries[k] for k in sorted(scope_entries)}
    packages["**"] = {**_NOBODY, "proxy": "npmjs"}
    cfg = {
        "storage": "/verdaccio/storage",
        "listen": f"0.0.0.0:{NPM_PORT}",
        "web": {"enable": False},
        "auth": {"htpasswd": {"file": "/tmp/htpasswd", "max_users": -1}},
        "uplinks": uplinks,
        "packages": packages,
        "log": {"type": "stdout", "format": "pretty", "level": "warn"},
    }
    return yaml.safe_dump(cfg, sort_keys=False)


def render_pypi_routes(registries: Mapping[str, RegistryDef], tokens: Mapping[str, str]) -> str:
    """Name-prefix routes for the PyPI proxy. A prefix without a token is denied."""
    routes: list[dict] = []
    for name, reg in registries.items():
        if reg.kind != "pypi":
            continue
        for prefix in reg.scopes:
            if name in tokens:
                routes.append(
                    {
                        "prefix": prefix,
                        "index": reg.url,
                        "auth": {"username": PYPI_USER[reg.upstream], "password": tokens[name]},
                    }
                )
            else:
                routes.append({"prefix": prefix, "deny": True})
    routes.sort(key=lambda r: r["prefix"])
    doc = {"routes": routes, "default": {"index": PYPI_PUBLIC_INDEX}, "upload": False}
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"
