"""The ``factory`` command line."""

import asyncio
import os
import signal
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.settings import get_settings

app = typer.Typer(help="Software factory CLI", no_args_is_help=True)
def_app = typer.Typer(help="Definition commands", no_args_is_help=True)
app.add_typer(def_app, name="def")


@app.command("init")
def init(path: Annotated[Path, typer.Argument(help="Directory for the new definition repo")]):
    """Create a definition repo from the defaults."""
    try:
        sha = init_definition_repo(path)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from None
    typer.echo(f"created definition repo at {path} ({sha[:12]})")


@def_app.command("validate")
def validate(
    source: Annotated[str | None, typer.Argument(help="Path or git URL")] = None,
    ref: Annotated[str | None, typer.Option(help="Git ref for a URL source")] = None,
):
    """Validate a definition and print file:line errors."""
    settings = get_settings()
    src = source or settings.definition_source
    if not src:
        typer.echo("no source given and FACTORY_DEFINITION is not set")
        raise typer.Exit(1)
    result = load_definition(src, ref=ref, mode=settings.mode)
    if result.errors:
        for e in result.errors:
            typer.echo(str(e))
        raise typer.Exit(1)
    assert result.definition is not None
    n_apps = len(result.definition.factory.apps)
    n_prof = len(result.definition.profiles)
    typer.echo(
        f"valid: {result.sha[:12]} ({n_apps} app{'s' if n_apps != 1 else ''}, "
        f"{n_prof} profile{'s' if n_prof != 1 else ''})"
    )


@def_app.command("sync")
def def_sync(
    source: Annotated[str | None, typer.Argument(help="Path or git URL")] = None,
    ref: Annotated[str | None, typer.Option(help="Git ref for a URL source")] = None,
):
    """Load a definition and make it the active one."""
    settings = get_settings()
    src = source or settings.definition_source
    if not src:
        typer.echo("no source given and FACTORY_DEFINITION is not set")
        raise typer.Exit(1)
    result = load_definition(src, ref=ref, mode=settings.mode)
    if result.errors:
        for e in result.errors:
            typer.echo(str(e))
        raise typer.Exit(1)

    async def run() -> tuple[bool, dict[str, str]]:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            store = DefinitionStore(make_sessionmaker(engine), mode=settings.mode)
            active = await store.save(result)
            if not active:
                return False, {}
            async with store.sessions() as s:
                from sqlalchemy import select

                from swfactory.db.tables import JobEvent

                data = await s.scalar(
                    select(JobEvent.data)
                    .where(JobEvent.kind == "definition.loaded")
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
            return True, dict((data or {}).get("apps", {}))
        finally:
            await engine.dispose()

    active, apps = asyncio.run(run())
    if not active:
        typer.echo(f"definition {result.sha[:12]} stored but not activated")
        raise typer.Exit(1)
    listed = ", ".join(f"{k} ({v})" for k, v in apps.items())
    typer.echo(f"active definition {result.sha[:12]}; apps: {listed}")


db_app = typer.Typer(help="Database commands", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade():
    """Upgrade the database schema to the latest revision."""
    from swfactory.db.migrate import upgrade_database

    settings = get_settings()
    import asyncio

    from swfactory.db.engine import setup_checkpointer

    url = settings.sqlalchemy_url()
    rev = upgrade_database(url)
    asyncio.run(setup_checkpointer(url))
    typer.echo(f"database at revision {rev}")


apps_app = typer.Typer(help="App commands", no_args_is_help=True)
app.add_typer(apps_app, name="apps")


@apps_app.command("list")
def apps_list():
    """List unarchived apps."""
    from sqlalchemy import select

    from swfactory.db.tables import App
    from swfactory.definition.store import load_active

    settings = get_settings()

    async def run() -> list[str]:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine)() as s:
                found = await load_active(s)
                if found is None:
                    return []
                defn = found[1]
                slugs = (
                    await s.scalars(
                        select(App.slug).where(App.archived_at.is_(None)).order_by(App.slug)
                    )
                ).all()
            lines = []
            for slug in slugs:
                if slug not in defn.factory.apps:
                    continue
                d = defn.factory.apps[slug]
                mp = d.merge_policy.value if d.merge_policy else ""
                lines.append(
                    f"{slug}  {d.repo_kind}  {mp}  {d.profile or defn.factory.defaults.profile}"
                )
            return lines
        finally:
            await engine.dispose()

    for line in asyncio.run(run()):
        typer.echo(line)


jobs_app = typer.Typer(help="Job commands", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("create")
def jobs_create(
    feature: Annotated[str, typer.Argument(help="Feature description")],
    app_slug: Annotated[str, typer.Option("--app", help="App slug")],
    title: Annotated[str | None, typer.Option(help="Job title")] = None,
    profile: Annotated[str | None, typer.Option(help="Profile name")] = None,
    budget: Annotated[Decimal | None, typer.Option(help="Budget in USD")] = None,
):
    """Queue a feature job."""
    import getpass

    from swfactory.definition.store import NoActiveDefinition
    from swfactory.queue import UnknownApp, enqueue_job

    settings = get_settings()

    async def run() -> str:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            sessions = make_sessionmaker(engine)
            async with sessions.begin() as s:
                job = await enqueue_job(
                    s,
                    app_slug=app_slug,
                    feature=feature,
                    title=title,
                    source="cli",
                    source_ref=None,
                    requested_by=os.environ.get("USER") or getpass.getuser(),
                    profile_name=profile,
                    budget_usd=budget,
                )
            return f"queued {app_slug}:{job.seq:03d} {job.id}"
        finally:
            await engine.dispose()

    try:
        typer.echo(asyncio.run(run()))
    except NoActiveDefinition:
        typer.echo("no active definition; run 'factory def sync' first")
        raise typer.Exit(1) from None
    except UnknownApp:
        typer.echo(f"unknown app '{app_slug}'")
        raise typer.Exit(1) from None
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from None


@jobs_app.command("list")
def jobs_list(
    app_slug: Annotated[str | None, typer.Option("--app", help="Filter by app")] = None,
    stage: Annotated[str | None, typer.Option(help="Filter by stage")] = None,
):
    """List jobs, newest first."""
    from sqlalchemy import select

    from swfactory.db.tables import App, Job

    settings = get_settings()

    async def run() -> list[str]:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            q = (
                select(Job.seq, App.slug, Job.stage, Job.title, Job.created_at)
                .join(App, App.id == Job.app_id)
                .order_by(Job.created_at.desc(), Job.seq.desc())
            )
            if app_slug:
                q = q.where(App.slug == app_slug)
            if stage:
                q = q.where(Job.stage == stage)
            async with make_sessionmaker(engine)() as s:
                rows = (await s.execute(q)).all()
            return [
                f"{seq:03d} {slug} {stg} {ttl} {created.isoformat(timespec='seconds')}"
                for seq, slug, stg, ttl, created in rows
            ]
        finally:
            await engine.dispose()

    for line in asyncio.run(run()):
        typer.echo(line)


@jobs_app.command("board")
def jobs_board(
    app_slug: Annotated[str | None, typer.Option("--app", help="Filter by app")] = None,
):
    """Print the board as JSON."""
    import json

    from swfactory.db.repo import board

    settings = get_settings()

    async def run() -> dict:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine)() as s:
                return await board(s, app_slug=app_slug)
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(run()), indent=2))


def _send_command(action: str, job_ref: str, note: str | None, by: str | None) -> None:
    import getpass

    from swfactory.commands import GATE_ACTIONS, CommandRefused, check_command_allowed
    from swfactory.db.repo import (
        JobRefError,
        insert_command,
        latest_waiting,
        pending_gate_command,
        resolve_job_ref,
    )
    from swfactory.domain.types import HumanAction, Stage

    settings = get_settings()
    who = by or f"cli:{os.environ.get('USER') or getpass.getuser()}"

    async def run() -> str:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine).begin() as s:
                job = await resolve_job_ref(s, job_ref)
                act = HumanAction(action)
                check_command_allowed(act, Stage(job.stage), note)
                wait_id: int | None = None
                if (
                    act in GATE_ACTIONS and job.stage == Stage.NEEDS_HUMAN.value
                ):  # answers the current wait
                    await s.get(type(job), job.id, with_for_update=True, populate_existing=True)
                    waiting = await latest_waiting(s, job.id)
                    if waiting is not None:
                        wait_id = waiting[0]
                        if await pending_gate_command(s, job.id, wait_id) is not None:
                            raise CommandRefused("an action is already pending for this wait")
                await insert_command(
                    s,
                    job_id=job.id,
                    action=action,
                    note=note,
                    requested_by=who,
                    gate_event_id=wait_id,
                )
            return f"{action} sent to {job_ref}"
        finally:
            await engine.dispose()

    try:
        typer.echo(asyncio.run(run()))
    except (CommandRefused, JobRefError) as e:
        typer.echo(str(e))
        raise typer.Exit(1) from None


def _register_job_action(action: str) -> None:
    def cmd(
        job: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
        note: Annotated[str | None, typer.Option(help="Note for the job")] = None,
        by: Annotated[str | None, typer.Option(help="Who is acting")] = None,
    ):
        _send_command(action, job, note, by)

    cmd.__doc__ = f"Send '{action}' to a job."
    jobs_app.command(action)(cmd)


for _action in ("approve", "rework", "reject", "retry", "cancel"):
    _register_job_action(_action)


@app.command("worker")
def worker(
    fake_script: Annotated[Path | None, typer.Option(help="Scripted fake engine (YAML)")] = None,
    max_jobs: Annotated[
        int | None, typer.Option(help="Jobs run at once (default: definition)")
    ] = None,
    fake_git_state: Annotated[
        Path | None, typer.Option(help="Fake git state file (tests)", hidden=True)
    ] = None,
    lease_s: Annotated[int, typer.Option(help="Job lease in seconds")] = 90,
    renew_s: Annotated[float, typer.Option(help="Lease renewal interval in seconds")] = 30,
    exit_when_idle: Annotated[bool, typer.Option(help="Exit when no job is left")] = False,
):
    """Run jobs from the queue."""
    from swfactory.worker import run_worker

    if fake_script is None:
        typer.echo("no engines yet: pass --fake-script (real engines arrive in M6)")
        raise typer.Exit(2)
    settings = get_settings()

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            if stop.is_set():  # a second signal exits at once
                os._exit(1)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal)
        await run_worker(
            settings,
            worker_id=settings.worker_id,
            max_jobs=max_jobs,
            lease_s=lease_s,
            renew_s=renew_s,
            fake_script=fake_script,
            fake_git_state=fake_git_state,
            exit_when_idle=exit_when_idle,
            stop=stop,
        )

    try:
        asyncio.run(main())
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        pass
