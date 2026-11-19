"""Renders the mirror's config from the definition and keeps it fresh."""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from swfactory.definition.loader import load_definition
from swfactory.definition.models import Definition
from swfactory.mirror.config import render_npm_config, render_pypi_routes
from swfactory.mirror.tokens import (
    TOKEN_REFRESH_S,
    TokenUnavailable,
    adc_access_token,
    broker_packages_fetch,
    read_env_file,
    registry_org,
    registry_token,
)
from swfactory.settings import Settings

log = structlog.get_logger(__name__)


def _write_private(path: Path, text: str) -> bool:
    """Atomic write with mode 0600; True when the content changed."""
    try:
        if path.read_text() == text:
            os.chmod(path, 0o600)
            return False
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


async def mint_tokens(
    defn: Definition,
    *,
    env_file: Path | None,
    adc: Callable[[], str] | None,
    github: Callable[[], Awaitable[str]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """(tokens, failures) by registry name; one failure never stops the others."""
    regs = defn.factory.registries
    names = [r.credential for r in regs.values() if r.upstream == "token" and r.credential]
    env: dict[str, str] = {}
    if env_file is not None and names:
        try:
            env = read_env_file(env_file, names)
        except OSError:
            env = {}
    tokens: dict[str, str] = {}
    failures: dict[str, str] = {}
    for name, reg in regs.items():
        try:
            tokens[name] = await registry_token(reg, env=env, adc=adc, github=github)
        except TokenUnavailable as e:
            failures[name] = str(e)
            log.warning("mirror.token_failed", registry=name, reason=str(e))
    return tokens, failures


async def sync_mirror(
    defn: Definition,
    *,
    out_dir: Path,
    env_file: Path | None,
    adc: Callable[[], str] | None,
    github: Callable[[], Awaitable[str]] | None,
) -> bool:
    tokens, _ = await mint_tokens(defn, env_file=env_file, adc=adc, github=github)
    return write_mirror(defn, tokens, out_dir)


def write_mirror(defn: Definition, tokens: dict[str, str], out_dir: Path) -> bool:
    regs = defn.factory.registries
    out_dir.mkdir(parents=True, exist_ok=True)
    changed = _write_private(out_dir / "npm.yaml", render_npm_config(regs, tokens))
    changed = _write_private(out_dir / "pypi.json", render_pypi_routes(regs, tokens)) or changed
    _write_private(out_dir / "ready", "")
    return changed


def _adc_for(settings: Settings) -> Callable[[], str] | None:
    path = settings.registry_adc_file
    return (lambda: adc_access_token(path)) if path is not None else None


def _github_for(settings: Settings, defn: Definition) -> Callable[[], Awaitable[str]] | None:
    """The broker callable for the first github_packages registry (None without a broker token)."""
    if not settings.broker_token_file:
        return None
    for reg in defn.factory.registries.values():
        if reg.upstream == "github_packages" and registry_org(reg):
            return broker_packages_fetch(
                settings.broker_url, settings.broker_token_file, registry_org(reg)
            )
    return None


async def registry_status(settings: Settings, defn: Definition, out_dir: Path) -> dict[str, str]:
    """One pass; returns '' for ok or the failure reason, by registry name."""
    tokens, failures = await mint_tokens(
        defn,
        env_file=settings.registry_env_file,
        adc=_adc_for(settings),
        github=_github_for(settings, defn),
    )
    write_mirror(defn, tokens, out_dir)
    return {n: failures.get(n, "") for n in defn.factory.registries}


async def run_mirror(
    settings: Settings, *, out_dir: Path, interval_s: int = 60, once: bool = False
) -> None:
    source = settings.definition_source
    if not source:
        raise RuntimeError("FACTORY_DEFINITION is not set")
    last_sha = ""
    last_mint = 0.0
    tokens: dict[str, str] = {}
    while True:
        result = await asyncio.to_thread(
            load_definition, source, ref=settings.definition_ref, mode=settings.mode
        )
        if result.errors or result.definition is None:
            log.warning("mirror.definition_invalid", errors=len(result.errors))
        else:
            now = time.monotonic()
            if result.sha != last_sha or now - last_mint >= TOKEN_REFRESH_S or not last_mint:
                tokens, _ = await mint_tokens(
                    result.definition,
                    env_file=settings.registry_env_file,
                    adc=_adc_for(settings),
                    github=_github_for(settings, result.definition),
                )
                last_sha, last_mint = result.sha, now
            if write_mirror(result.definition, tokens, out_dir):
                log.info("mirror.updated", sha=result.sha[:12])
        if once:
            return
        await asyncio.sleep(interval_s)
