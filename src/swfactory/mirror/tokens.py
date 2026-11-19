"""Registry token sources. Every token returned is registered for log redaction."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path

import httpx

from swfactory.definition.models import RegistryDef
from swfactory.logging import register_secret

TOKEN_REFRESH_S = 1800
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class TokenUnavailable(RuntimeError):
    pass


async def registry_token(
    reg: RegistryDef,
    *,
    env: Mapping[str, str],
    adc: Callable[[], str] | None,
    github: Callable[[], Awaitable[str]] | None,
) -> str:
    if reg.upstream == "artifact_registry":
        if adc is None:
            raise TokenUnavailable("no registry ADC (secrets/registry-credentials.json)")
        token = adc()
    elif reg.upstream == "token":
        name = reg.credential or ""
        token = env.get(name, "")
        if not token:
            raise TokenUnavailable(f"{name} is not set")
    else:
        if github is None:
            raise TokenUnavailable("github_packages needs the GitHub App (M8.10b)")
        token = await github()
    if not token:
        raise TokenUnavailable("empty token")
    register_secret(token)
    return token


def registry_org(reg: RegistryDef) -> str:
    """GitHub Packages requires the token owner to equal the scope: the first scope without '@'."""
    return reg.scopes[0].lstrip("@") if reg.scopes else ""


def broker_packages_fetch(
    broker_url: str, token_file: str, org: str
) -> Callable[[], Awaitable[str]]:
    async def fetch() -> str:
        try:
            shared = Path(token_file).read_text().strip()
            async with httpx.AsyncClient(timeout=30) as http:
                r = await http.post(
                    f"{broker_url.rstrip('/')}/internal/github-packages-token",
                    json={"org": org},
                    headers={"authorization": f"Bearer {shared}"},
                )
            r.raise_for_status()
            return str(r.json()["token"])
        except (OSError, httpx.HTTPError, KeyError, ValueError) as e:
            raise TokenUnavailable(
                f"GitHub Packages token unavailable ({type(e).__name__})"
            ) from None

    return fetch


def read_env_file(path: Path, names: Iterable[str]) -> dict[str, str]:
    """Only the named variables; the rest of the file is never kept."""
    wanted = set(names)
    out: dict[str, str] = {}
    if not wanted:
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def adc_access_token(path: Path) -> str:
    import json

    import google.auth
    import google.auth.transport.requests

    try:
        text = path.read_text().strip()
        empty = not text or not json.loads(text)
    except (OSError, ValueError) as e:
        raise TokenUnavailable(f"no registry ADC ({type(e).__name__})") from None
    if empty:
        raise TokenUnavailable("no registry ADC (file is empty)")
    try:
        creds, _ = google.auth.load_credentials_from_file(str(path), scopes=[_SCOPE])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as e:  # google-auth raises several types
        raise TokenUnavailable(f"registry ADC refresh failed ({type(e).__name__})") from None
    return str(creds.token)
