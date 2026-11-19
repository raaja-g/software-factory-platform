"""`factory run`: point the factory at a repo and a requirement."""

import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

POLL_S = 1.0
TIMEOUT_S = 120.0


def _requirement(req: str | None, req_text: str | None) -> str:
    if (req is None) == (req_text is None):
        typer.echo("give exactly one of --req and --req-text")
        raise typer.Exit(2)
    if req_text is not None:
        return req_text
    assert req is not None
    if req == "-":
        try:
            return sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            typer.echo("the requirement on stdin is not UTF-8 text")
            raise typer.Exit(2) from None
    try:
        return Path(req).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        typer.echo(f"cannot read {req}: {getattr(e, 'strerror', None) or e}")
        raise typer.Exit(2) from None


def _repo(repo: str) -> str:
    if repo.startswith("."):
        return str(Path(repo).resolve())
    if repo.startswith("~"):
        return os.path.expanduser(repo)
    return repo


def run(
    repo: Annotated[str, typer.Option("--repo", help="Folder, owner/name or GitHub URL")],
    req: Annotated[
        str | None, typer.Option("--req", help="Requirement file, or - for stdin")
    ] = None,
    req_text: Annotated[str | None, typer.Option("--req-text", help="Requirement text")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Model profile")] = None,
    ci_safe: Annotated[
        bool, typer.Option("--ci-safe", help="Register a GitHub repo as CI-safe")
    ] = False,
    budget: Annotated[str | None, typer.Option("--budget", help="Budget in USD")] = None,
) -> None:
    """Register the repo if needed and start a requirements job for the requirement."""
    from swfactory.cli import api_client
    from swfactory.cli.main import _api_call

    text = _requirement(req, req_text)
    body: dict[str, Any] = {"repo": _repo(repo), "requirement": text, "ci_safe": ci_safe}
    if profile is not None:
        body["profile"] = profile
    if budget is not None:
        body["budget_usd"] = budget
    request_id = _api_call(lambda c: c.create_run(body))
    waited = 0.0
    told = False
    while True:
        st = _api_call(lambda c: c.get_run(request_id))
        status = st.get("status")
        if status == "started":
            typer.echo(f"backlog {st['backlog']} started; requirements job {st['job_ref']}")
            return
        if status == "failed":
            typer.echo(st.get("detail") or "the run failed")
            raise typer.Exit(1)
        if status == "waiting" and not told:
            typer.echo(f"waiting for the definition PR {st.get('detail')} to merge")
            told = True
        if waited >= TIMEOUT_S:
            typer.echo("still working; see the dashboard")
            raise typer.Exit(1)
        api_client.time.sleep(POLL_S)
        waited += POLL_S
