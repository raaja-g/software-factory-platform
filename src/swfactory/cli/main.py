"""The ``factory`` command line."""

import asyncio
import json
import os
import signal
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from swfactory.api.auth import TOKEN_DAYS_DEFAULT
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
    if source is None:  # the orchestrator (which holds the App key) syncs FACTORY_DEFINITION
        _api_call(lambda c: c.sync_definition())
        typer.echo("sync requested; the orchestrator loads FACTORY_DEFINITION")
        return
    src = source
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
    """List the apps visible to you."""
    if not _has_token():
        _apps_list_direct()
        return
    for a in _api_call(lambda c: c.list_apps()):
        mp = a.get("merge_policy") or ""
        typer.echo(f"{a['slug']}  {a['repo_kind']}  {mp}  {a['profile']}")


@apps_app.command("create")
def apps_create(
    slug: Annotated[str, typer.Argument(help="App slug for the definition")],
    github: Annotated[str, typer.Option("--github", help="<owner>/<name> of the new repo")],
    template: Annotated[
        str | None, typer.Option("--template", help="<owner>/<repo> to generate from")
    ] = None,
    public: Annotated[bool, typer.Option("--public", help="Create a public repo")] = False,
):
    """Create a GitHub repo with auto-merge on and print the apps: block to add."""
    from swfactory.github.auth import make_auth
    from swfactory.gitops.github import provision_repo

    for value in (github, template):
        if value is not None and (value.count("/") != 1 or not all(value.split("/"))):
            typer.echo(f"expected <owner>/<name>, got {value!r}")
            raise typer.Exit(2)
    try:
        auth = make_auth(get_settings())
        result = asyncio.run(
            provision_repo(auth, full_name=github, template=template, private=not public)
        )
    except Exception as e:  # noqa: BLE001 - one readable line for any GitHub or auth failure
        typer.echo(f"cannot create {github}: {e}")
        raise typer.Exit(1) from None
    typer.echo(f"created: {result.url}")
    typer.echo(
        "Add this to the definition repo through a pull request:\n\n"
        "apps:\n"
        f"  {slug}:\n"
        "    repo_kind: github\n"
        f"    repo_url: {result.url}\n"
        f"    default_branch: {result.default_branch}\n"
        "    merge_policy: pr_auto_merge\n"
        "    github: {ci_safe: true}\n\n"
        "Set ci_safe to false if organization secrets are available to this repo."
    )
    if not result.reachable:
        typer.echo(
            f"installation: not reachable. Please add {result.full_name} to the app installation."
        )
        raise typer.Exit(2)
    typer.echo("installation: reachable")


def _apps_list_direct() -> None:
    """No API token: read the apps from the database (local development)."""
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


gateway_app = typer.Typer(help="Gateway commands", no_args_is_help=True)
app.add_typer(gateway_app, name="gateway")


@gateway_app.command("render")
def gateway_render(
    out: Annotated[Path, typer.Option(help="Where to write the LiteLLM config")] = Path(
        "var/litellm.yaml"
    ),
    def_source: Annotated[
        str | None, typer.Option("--def", help="Definition path or git URL")
    ] = None,
    active: Annotated[
        bool, typer.Option("--active", help="Render the active definition (loads the source first)")
    ] = False,
):
    """Write the LiteLLM config rendered from the definition."""
    from swfactory.gateway.render import config_text

    settings = get_settings()
    if active:
        sha, defn = asyncio.run(_active_for_render(settings))
    else:
        src = def_source or settings.definition_source
        if not src:
            typer.echo("no definition given and FACTORY_DEFINITION is not set")
            raise typer.Exit(1)
        result = load_definition(src, mode=settings.mode)
        if result.errors or result.definition is None:
            for e in result.errors:
                typer.echo(str(e))
            raise typer.Exit(1)
        sha, defn = result.sha, result.definition
    text = config_text(defn, sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    typer.echo(f"wrote {out}: {len(defn.factory.models)} models from {sha[:8]}")


mirror_app = typer.Typer(help="Package mirror commands", no_args_is_help=True)
app.add_typer(mirror_app, name="mirror")


def _mirror_settings():
    settings = get_settings()
    if not settings.definition_source:
        typer.echo("FACTORY_DEFINITION is not set")
        raise typer.Exit(1)
    return settings


@mirror_app.command("render")
def mirror_render(
    out: Annotated[Path, typer.Option(help="Where to write the mirror config")],
):
    """One pass: mint registry tokens and write the mirror's config; one line per registry."""
    from swfactory.mirror.service import registry_status

    settings = _mirror_settings()
    assert settings.definition_source is not None
    result = load_definition(settings.definition_source, mode=settings.mode)
    if result.errors or result.definition is None:
        for e in result.errors:
            typer.echo(str(e))
        raise typer.Exit(1)
    status = asyncio.run(registry_status(settings, result.definition, out))
    for name, reason in status.items():
        typer.echo(f"failed {name}: {reason}" if reason else f"ok {name}")


@mirror_app.command("run")
def mirror_run(
    out: Annotated[Path, typer.Option(help="Where to write the mirror config")],
    interval: Annotated[int, typer.Option(min=1, help="Seconds between definition reads")] = 60,
):
    """Keep the mirror's config fresh (the token sidecar's command)."""
    from swfactory.mirror.service import run_mirror

    settings = _mirror_settings()
    asyncio.run(run_mirror(settings, out_dir=out, interval_s=interval))


async def _active_for_render(settings):
    """The active definition; when none is active yet, load the source first (as def sync)."""
    from swfactory.definition.store import DefinitionStore, NoActiveDefinition

    engine = make_engine(settings.sqlalchemy_url())
    try:
        store = DefinitionStore(make_sessionmaker(engine), mode=settings.mode)
        try:
            return await store.active()
        except NoActiveDefinition:
            pass
        src = settings.definition_source
        if not src:
            typer.echo("no active definition and FACTORY_DEFINITION is not set")
            raise typer.Exit(1) from None
        result = await asyncio.to_thread(
            load_definition, src, ref=settings.definition_ref, mode=settings.mode
        )
        if result.errors:
            for e in result.errors:
                typer.echo(str(e))
            raise typer.Exit(1)
        if not await store.save(result):
            typer.echo(f"definition {result.sha[:12]} stored but not activated")
            raise typer.Exit(1)
        return await store.active()
    finally:
        await engine.dispose()


profiles_app = typer.Typer(help="Profile commands", no_args_is_help=True)
app.add_typer(profiles_app, name="profiles")


@profiles_app.command("show")
def profiles_show(
    name: Annotated[str | None, typer.Argument(help="Profile name (default: global)")] = None,
):
    """Show a profile's per-role models and their status against the catalog."""
    from sqlalchemy import select

    from swfactory.db.tables import CatalogModel
    from swfactory.definition.store import load_active
    from swfactory.models.catalog import ModelInfo
    from swfactory.models.profiles import (
        ResolvedProfile,
        profile_warnings,
        validate_profile,
    )

    settings = get_settings()

    async def run() -> tuple[str, ResolvedProfile, dict[str, ModelInfo]] | None:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine)() as s:
                found = await load_active(s)
                if found is None:
                    return None
                defn = found[1]
                pname = name or defn.factory.defaults.profile
                if pname not in defn.profiles:
                    raise KeyError(pname)
                rows = (await s.scalars(select(CatalogModel))).all()
            infos = {
                r.alias: ModelInfo(
                    alias=r.alias,
                    provider=r.provider,
                    upstream_id=r.upstream_id,
                    strength=r.strength or "",
                    price_in_per_mtok=r.price_in_per_mtok or Decimal(0),
                    price_out_per_mtok=r.price_out_per_mtok or Decimal(0),
                    context_window=r.context_window,
                    supports_tools=r.supports_tools,
                    messages_tools_ok=r.messages_tools_ok,
                    available=r.available,
                    error_class=r.error_class,
                    latency_ms=r.latency_ms,
                    last_error=r.last_error,
                )
                for r in rows
            }
            return pname, ResolvedProfile.from_def(pname, defn.profiles[pname]), infos
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except KeyError as e:
        typer.echo(f"unknown profile {e.args[0]}")
        raise typer.Exit(1) from None
    if result is None:
        typer.echo("no active definition")
        raise typer.Exit(1)
    pname, prof, infos = result
    errors = validate_profile(prof, infos)
    warnings = profile_warnings(prof, infos)
    typer.echo(f"profile {pname}")
    for role, rm in prof.roles.items():
        problems = [e for e in errors if e.startswith(f"{role.value}: ")]
        warns = [w for w in warnings if w.startswith(f"{role.value}: ")]
        status = "; ".join(problems) if problems else ("ok" if not warns else f"warn: {warns[0]}")
        typer.echo(
            f"{role.value}  {rm.engine}  {rm.model}  {rm.mode.value}  "
            f"{','.join(rm.fallback) or '-'}  sticky={str(rm.sticky).lower()}  {status}"
        )
    if errors:
        raise typer.Exit(1)


jobs_app = typer.Typer(help="Job commands", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


def _client() -> Any:
    from swfactory.cli.api_client import ApiClient

    return ApiClient.from_settings(get_settings())


def _api_call(fn: Any) -> Any:
    """Run ``fn(client)``; an API or transport failure prints the detail and exits 1."""
    import httpx

    from swfactory.cli.api_client import ApiError, RefreshTimeout

    client = _client()
    try:
        return fn(client)
    except ApiError as e:
        typer.echo(e.detail)
        raise typer.Exit(1) from None
    except RefreshTimeout as e:
        typer.echo(str(e))
        raise typer.Exit(1) from None
    except httpx.TransportError as e:
        typer.echo(f"cannot reach the API at {client.base_url}: {e!r}")
        raise typer.Exit(1) from None


def _has_token() -> bool:
    return _client().token is not None


def _norm_ref(ref: str) -> str:
    """``app/3`` and ``app#3`` become ``app:3``; a uuid or ``app:3`` is kept as typed."""
    from swfactory.commands import parse_job_ref

    try:
        key = parse_job_ref(ref)
    except ValueError:
        return ref
    return f"{key[0]}:{key[1]}" if isinstance(key, tuple) else ref


def _job_link(job_id: str) -> str:
    return f"{get_settings().api_url.rstrip('/')}/#/job/{job_id}"


@jobs_app.command("create")
def jobs_create(
    feature_arg: Annotated[str | None, typer.Argument(metavar="[FEATURE]")] = None,
    app_slug: Annotated[str, typer.Option("--app", help="App slug")] = "",
    feature_opt: Annotated[str | None, typer.Option("--feature", help="Feature text")] = None,
    title: Annotated[str | None, typer.Option(help="Job title")] = None,
    profile: Annotated[str | None, typer.Option(help="Profile name")] = None,
    budget: Annotated[Decimal | None, typer.Option(help="Budget in USD")] = None,
):
    """Queue a feature job (FEATURE or --feature)."""
    if feature_arg is not None and feature_opt is not None:
        typer.echo("give the feature once: as FEATURE or as --feature")
        raise typer.Exit(2)
    feature = feature_arg if feature_arg is not None else feature_opt
    if not feature or not app_slug:
        typer.echo("jobs create needs --app and a feature")
        raise typer.Exit(2)
    if not _has_token():
        _jobs_create_direct(app_slug, feature, title, profile, budget)
        return
    out = _api_call(
        lambda c: c.create_job(app_slug, feature, title=title, profile=profile, budget=budget)
    )
    from swfactory.commands import parse_job_ref

    ref = str(out["ref"])
    key = parse_job_ref(ref)
    shown = f"{key[0]}:{key[1]:03d}" if isinstance(key, tuple) else ref
    typer.echo(f"queued {shown} {out['job_id']}")
    typer.echo(_job_link(out["job_id"]))


def _jobs_create_direct(
    app_slug: str, feature: str, title: str | None, profile: str | None, budget: Decimal | None
) -> None:
    """No API token: queue straight in the database (local development)."""
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
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
):
    """List jobs, newest first."""
    if not _has_token():
        _jobs_list_direct(app_slug, stage)
        return
    import json

    rows = _api_call(lambda c: c.list_jobs(app_slug, stage))
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for j in rows:
        typer.echo(f"{j['ref']} {j['stage']} {j['title']} {j['created_at']}")


def _jobs_list_direct(app_slug: str | None, stage: str | None) -> None:
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


def _head_sha(job: dict[str, Any]) -> str | None:
    return job.get("head_sha") or (job.get("waiting") or {}).get("head_sha")


@jobs_app.command("show")
def jobs_show(
    ref: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
):
    """Show one job: stage, verdicts, decision, models and cost."""
    import json

    from swfactory.cli.api_client import ApiError

    ref = _norm_ref(ref)

    def fetch(
        c: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        job = c.show_job(ref)
        try:
            runs = c.job_runs(ref)
        except ApiError:
            runs = []
        try:
            scores = c.job_scores(ref)
        except ApiError:
            scores = []
        try:
            judges = c.job_judges(ref) if any(_is_judge_row(r) for r in scores) else []
        except ApiError:
            judges = []
        return job, runs, scores, judges

    job, runs, scores, judges = _api_call(fetch)
    if as_json:
        typer.echo(json.dumps({**job, "runs": runs, "scores": scores}, indent=2))
        return
    typer.echo(f"{job['ref']}  {job['title']}")
    typer.echo(f"stage: {job['stage']}  round: {job['round']}  head: {_head_sha(job) or '-'}")
    verdicts = [r for r in runs if r.get("verdict")]
    if verdicts:
        typer.echo("verdicts:")
        for r in verdicts:
            typer.echo(f"  round {r['round']}  {r['role']}  {r['verdict']}")
    typer.echo(f"decision: {job.get('decision') or '-'}")
    seen: dict[str, str] = {}
    for r in runs:
        used = ",".join(r.get("used_models") or []) or "-"
        seen[r["role"]] = f"{r.get('requested_model') or '-'} -> {used}"
    if seen:
        typer.echo("models:")
        for role, m in seen.items():
            typer.echo(f"  {role}: {m}")
    typer.echo(f"cost: ${job['cost_usd']} of ${job['budget_usd']}")
    block = _scores_line(scores)
    judge_rows = [r for r in scores if _is_judge_row(r)]
    if block or judge_rows:
        typer.echo("Scores:")
        if block:
            typer.echo(f"  {block}")
        for r in judge_rows:
            typer.echo(f"  {_score_line(r, judges)}")
    typer.echo(_job_link(job["job_id"]))


_SCORES_BLOCK = (
    ("obj.gate", "gate {}"),
    ("obj.fix_attempts", "fix attempts {}"),
    ("obj.rounds", "rounds {}"),
    ("obj.decision", "decision {}"),
    ("obj.human", "human {}"),
    ("obj.delivery", "delivery {}"),
    ("obj.fallback", "fallback {}"),
    ("obj.duration_s", "{}"),
    ("obj.cost_usd", "{}"),
    ("obj.quality", "quality {}"),
)


def _is_judge_row(r: dict[str, Any]) -> bool:
    return not str(r["scorer"]).startswith("obj.")


def _scores_line(scores: list[dict[str, Any]]) -> str:
    """The objective signals in one line: gate passed · fix attempts 0 · ... · quality good."""
    by = {str(r["scorer"]): str(r["label"]) for r in scores}
    return " · ".join(fmt.format(by[name]) for name, fmt in _SCORES_BLOCK if name in by)


def _score_line(r: dict[str, Any], judges: list[dict[str, Any]] | None = None) -> str:
    status = {True: "passed", False: "failed", None: "-"}[r.get("passed")]
    parts = [str(r["scorer"]), str(r["label"]), status]
    if r.get("judge_model"):
        parts.append(f"judge {r['judge_model']}")
    parts.append(f"${r.get('cost_usd') or '0.000000'}")
    if r.get("judge_model"):
        tag = next(
            (
                j.get("calibration")
                for j in judges or []
                if j.get("scorer") == r["scorer"] and j.get("judge_model") == r["judge_model"]
            ),
            None,
        )
        if tag:
            parts.append(str(tag))
    return "  ".join(parts)


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


def _job_action(action: str, ref: str, note: str | None, by: str | None) -> None:
    if not _has_token():
        _send_command(action, ref, note, by)
        return
    ref = _norm_ref(ref)

    def go(c: Any) -> tuple[str | None, dict[str, Any]]:
        sha = None
        if action == "approve":
            sha = _head_sha(c.show_job(ref))
        return sha, c.action(ref, action, note, head_sha=sha)

    sha, _ = _api_call(go)
    suffix = f" (head {sha})" if sha else ""
    typer.echo(f"{action} sent to {ref}{suffix}")


def _register_job_action(action: str) -> None:
    def cmd(
        job: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
        note: Annotated[str | None, typer.Option(help="Note for the job")] = None,
        by: Annotated[str | None, typer.Option(help="Who is acting (local database mode)")] = None,
    ):
        _job_action(action, job, note, by)

    cmd.__doc__ = f"Send '{action}' to a job."
    jobs_app.command(action)(cmd)


for _action in ("approve", "reject", "retry", "cancel"):
    _register_job_action(_action)


@jobs_app.command("rework")
def jobs_rework(
    job: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    note_arg: Annotated[str | None, typer.Argument(metavar="[NOTE]")] = None,
    note: Annotated[str | None, typer.Option("--note", help="What to change")] = None,
    by: Annotated[str | None, typer.Option(help="Who is acting (local database mode)")] = None,
):
    """Send 'rework' with a note (--note TEXT or NOTE)."""
    if note_arg is not None and note is not None:
        typer.echo("give the note once: as NOTE or as --note")
        raise typer.Exit(2)
    text = note_arg if note_arg is not None else note
    if not text or not text.strip():
        typer.echo("rework needs a note")
        raise typer.Exit(2)
    _job_action("rework", job, text, by)


@jobs_app.command("follow-up")
def jobs_follow_up(
    job: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    note: Annotated[str, typer.Argument(metavar="NOTE", help="What should change on the PR")],
):
    """Send a delivered job back as a follow-up job on its open PR."""
    if not note.strip():
        typer.echo("a follow-up needs a note")
        raise typer.Exit(2)
    ref = _norm_ref(job)
    out = _api_call(lambda c: c.follow_up(ref, note))
    typer.echo(f"queued {out['ref']} {out['job_id']}")


@jobs_app.command("add-budget")
def jobs_add_budget(
    job: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    usd: Annotated[str, typer.Argument(metavar="USD", help="Extra budget in USD")],
    note: Annotated[str | None, typer.Option("--note", help="Note for the job")] = None,
):
    """Add budget to a job stopped at its budget (admins)."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(usd)
    except InvalidOperation:
        amount = Decimal(0)
    if not amount.is_finite() or amount <= 0:
        typer.echo("the amount must be a positive number of USD")
        raise typer.Exit(2)
    ref = _norm_ref(job)
    _api_call(lambda c: c.action(ref, "add_budget", note, amount_usd=usd.strip()))
    typer.echo(f"add_budget {usd.strip()} sent to {ref}")


class _ScoreGroup(TyperGroup):
    """``factory score REF`` scores a job; ``label`` and ``calibration`` stay subcommands."""

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            return "run", self.commands["run"], args
        return super().resolve_command(ctx, args)


score_app = typer.Typer(help="Scoring commands", no_args_is_help=True, cls=_ScoreGroup)
app.add_typer(score_app, name="score")


@score_app.command("run", hidden=True)
def score_cmd(
    ref: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    scorer: Annotated[
        list[str] | None, typer.Option("--scorer", help="Judge scorer to run (repeatable)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Run every judge, sampled or not")] = False,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="Queue the judges and return at once")
    ] = False,
):
    """Score a finished job: objective rows now, the sampled (or chosen) judges on the worker."""
    ref = _norm_ref(ref)
    out = _api_call(lambda c: c.score_job(ref, scorer, force))
    s = out["summary"]
    rid = out.get("request_id")
    errors: list[str] = list(s["errors"])
    skipped: list[str] = list(s["judges_skipped"])
    if rid and no_wait:
        names = ", ".join(s.get("judges_pending") or []) or "-"
        typer.echo(f"queued score request {rid} for {names}")
        return
    if rid:
        st = _api_call(lambda c: c.wait_score_request(str(rid)))
        if st["status"] == "failed":
            typer.echo(f"scoring failed: {st.get('detail') or 'unknown error'}")
            raise typer.Exit(1)
        done = st.get("summary") or {}
        skipped = list(done.get("judges_skipped", []))
        errors = [*errors, *done.get("errors", [])]

    def fetch(c: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return c.job_scores(ref), c.job_judges(ref)

    scores, judges = _api_call(fetch)
    for r in scores:
        if _is_judge_row(r):
            typer.echo(_score_line(r, judges))
    for line in (*skipped, *errors):
        typer.echo(f"  {line}")


bench_app = typer.Typer(help="Benchmark commands", no_args_is_help=True)
app.add_typer(bench_app, name="bench")


@bench_app.command("add")
def bench_add(
    from_job: Annotated[str, typer.Option("--from-job", help="Merged job: uuid or <app>:<seq>")],
    suite: Annotated[str, typer.Option("--suite", help="Benchmark suite name")],
    name: Annotated[str | None, typer.Option("--name", help="Task name")] = None,
    scorer: Annotated[
        list[str] | None, typer.Option("--scorer", help="Scorer to run on the task (repeatable)")
    ] = None,
    check_command: Annotated[
        str | None, typer.Option("--check-command", help="Command with {file} to run each check")
    ] = None,
):
    """Add a benchmark task, with hidden checks, from a merged job."""
    ref = _norm_ref(from_job)
    out = _api_call(
        lambda c: c.add_bench_task(
            ref, suite, name=name, scorers=scorer, check_command=check_command
        )
    )
    save = out["save"]
    where = save.get("pr_url") or (save.get("sha") or "")[:12]
    typer.echo(f"added task {out['task']} to suite {out['suite']} ({save.get('mode')}: {where})")
    if save.get("mode") == "pr":
        typer.echo("merge the pull request, then the definition syncs")


@bench_app.command("seed")
def bench_seed(suite: Annotated[str, typer.Argument(help="Benchmark suite name")]):
    """Import a suite's repo.bundle into the local repos directory (run in the orchestrator)."""
    from swfactory.bench.tasks import seed_bench_repo
    from swfactory.definition.loader import defaults_dir

    settings = get_settings()
    roots = [Path(settings.definition_source)] if settings.definition_source else []
    roots.append(defaults_dir())
    bundle = next(
        (
            r / "benchmarks" / suite / "repo.bundle"
            for r in roots
            if (r / "benchmarks" / suite / "repo.bundle").is_file()
        ),
        None,
    )
    if bundle is None:
        typer.echo(f"no repo.bundle for suite '{suite}'")
        raise typer.Exit(1)
    try:
        bare = seed_bench_repo(suite, bundle, Path(settings.repos_dir))
    except (OSError, RuntimeError) as e:
        typer.echo(f"seed failed: {e}")
        raise typer.Exit(1) from None
    import subprocess

    from swfactory.gitops.service import safe_git

    refs = subprocess.run(
        safe_git("-C", str(bare), "for-each-ref", "--format=%(refname)", "refs/heads/base-*"),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    typer.echo(f"seeded {suite}: {len(refs)} base commits")


def _estimate_line(est: dict[str, Any], budget: Decimal | None) -> str:
    cap = budget if budget is not None else est.get("budget_usd")
    cap_text = f"budget cap ${Decimal(str(cap)):.2f}" if cap is not None else "no budget cap"
    return (
        f"{est['trials']} trials \u00b7 estimated ${Decimal(est['estimate_usd']):.2f} "
        f"(up to ${Decimal(est['max_usd']):.2f}) \u00b7 {cap_text}"
    )


@bench_app.command("estimate")
def bench_estimate(
    suite: Annotated[str, typer.Argument(help="Benchmark suite name")],
    config: Annotated[
        list[str] | None, typer.Option("--config", help="Configuration to run (repeatable)")
    ] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=1, help="Runs per trial")] = 1,
):
    """Show what a benchmark run would cost, without starting it."""
    est = _api_call(lambda c: c.bench_estimate(suite, configs=config, repetitions=repetitions))
    typer.echo(_estimate_line(est, None))
    for name, usd in est.get("per_config", {}).items():
        typer.echo(f"  {name}: ${Decimal(usd):.2f} ({est.get('basis', {}).get(name, '')})")


@bench_app.command("run")
def bench_run(
    suite: Annotated[str, typer.Argument(help="Benchmark suite name")],
    config: Annotated[
        list[str] | None, typer.Option("--config", help="Configuration to run (repeatable)")
    ] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=1, help="Runs per trial")] = 1,
    budget: Annotated[
        Decimal | None, typer.Option("--budget", help="Run budget cap in USD")
    ] = None,
    definition: Annotated[
        str | None, typer.Option("--definition", help="Definition sha to run under")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Start without asking")] = False,
):
    """Start a benchmark run after showing its estimate."""
    est = _api_call(lambda c: c.bench_estimate(suite, configs=config, repetitions=repetitions))
    typer.echo(_estimate_line(est, budget))
    if not yes and not typer.confirm("Start this run?"):
        typer.echo("not started")
        raise typer.Exit(1)
    out = _api_call(
        lambda c: c.bench_run(
            suite,
            accept_estimate_usd=est["estimate_usd"],
            configs=config,
            repetitions=repetitions,
            budget=budget,
            definition=definition,
        )
    )
    typer.echo(f"started bench run {out['run_id']}")


@bench_app.command("list")
def bench_list():
    """List benchmark suites and runs."""
    suites = _api_call(lambda c: c.bench_suites())
    runs = _api_call(lambda c: c.bench_runs())
    typer.echo("Suites")
    for su in suites:
        cfgs = ", ".join(su["configurations"])
        typer.echo(f"  {su['name']}  {su['tasks']} tasks  [{cfgs}]  budget ${su['budget_usd']}")
    typer.echo("Runs")
    for r in runs:
        spent = f"${Decimal(r['cost_usd']):.2f}"
        cap = f"${Decimal(r['budget_usd']):.2f}" if r.get("budget_usd") else "no cap"
        typer.echo(f"  {r['id']}  {r['suite']}  {r['status']}  {spent} of {cap}")


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def report_lines(rep: dict[str, Any]) -> list[str]:
    """The per-configuration table and the recommendation of a bench report."""
    lines = [
        f"Run {rep['id']} - suite {rep['suite']} - {rep['status']} - "
        f"spent ${Decimal(rep['cost_usd']):.2f} of ${Decimal(rep['budget_usd']):.2f}",
        f"{'config':<14}{'hidden pass':<28}{'judge (not ranked)':<20}{'rounds':<8}"
        f"{'cost/task':<11}{'time':<8}not run",
    ]
    for c in rep["configs"]:
        iv = c.get("hidden_pass_interval")
        hidden = "-"
        if c["hidden_pass_rate"] is not None and iv:
            hidden = f"{c['hidden_pass_rate']:.2f} (n={c['completed']}, {iv[0]:.2f}-{iv[1]:.2f})"
        judge = _pct(c["judge_pass_rate"])
        if c.get("judge_pass_rate_uncalibrated") is not None:
            judge += f" (not counted: {c['judge_pass_rate_uncalibrated']:.2f})"
        cost = "-" if c["mean_cost_usd"] is None else f"${Decimal(c['mean_cost_usd']):.2f}"
        dur = c["median_duration_s"]
        rounds = "-" if c["mean_rounds"] is None else f"{c['mean_rounds']:.1f}"
        lines.append(
            f"{c['name']:<14}{hidden:<28}{judge:<20}{rounds:<8}{cost:<11}"
            f"{'-' if dur is None else f'{dur:.0f}s':<8}{c['not_run']}"
        )
    for t in rep.get("invalid_tasks", []):
        why = (rep.get("invalid_reasons") or {}).get(t)
        lines.append(f"invalid task {t}" + (f": {why}" if why else ""))
    if rep["recommended"]:
        lines.append(f"Recommended: {rep['recommendation_reason']}")
    else:
        lines.append(f"Recommended: none ({rep['recommendation_reason']})")
    return lines


@bench_app.command("report")
def bench_report(
    run: Annotated[str, typer.Argument(help="Benchmark run id")],
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON")] = False,
):
    """Show a benchmark run's per-configuration report and recommendation."""
    rep = _api_call(lambda c: c.bench_report(run))
    if as_json:
        typer.echo(json.dumps(rep, indent=2))
        return
    for line in report_lines(rep):
        typer.echo(line)


@score_app.command("label")
def score_label(
    ref: Annotated[str, typer.Argument(help="Job uuid or <app>:<seq>")],
    scorer: Annotated[str, typer.Argument(help="Scorer name")],
    label: Annotated[str, typer.Argument(help="Your label for the job")],
):
    """Store your label for a job's scorer, then show what each judge said."""
    from swfactory.scoring.calibration import CALIBRATION_MIN_LABELS

    out = _api_call(lambda c: c.label_score(_norm_ref(ref), scorer, label))
    judges = out.get("judges", [])
    n = max((int(j.get("n") or 0) for j in judges), default=0)
    typer.echo(
        f"labelled {scorer} on {ref}: {out['label']} ({n} of {CALIBRATION_MIN_LABELS} labels)"
    )
    for j in judges:
        typer.echo(f"  {j['judge_model']}: {j['label']} ({j['calibration']})  {j['reason'] or ''}")


@score_app.command("calibration")
def score_calibration():
    """Show each judge's agreement with your labels."""
    rows = _api_call(lambda c: c.calibration())
    if not rows:
        typer.echo("no labels yet")
        return
    for r in rows:
        k = "-" if r["kappa"] is None else f"{r['kappa']:.2f}"
        typer.echo(
            f"{r['scorer']}  {r['judge_model']}  n={r['n']} pass={r['human_pass']} "
            f"fail={r['human_fail']} kappa={k}  {r['tag']}"
        )


backlog_app = typer.Typer(help="Backlog commands", no_args_is_help=True)
app.add_typer(backlog_app, name="backlog")


@backlog_app.command("import")
def backlog_import(
    app_slug: Annotated[str, typer.Argument(metavar="APP", help="App slug")],
    file: Annotated[Path, typer.Argument(help="Markdown backlog file")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print, post nothing")] = False,
):
    """Import the open items of a markdown file as one ordered backlog."""
    from swfactory.cli.backlog import parse_backlog

    try:
        text = file.read_text()
    except OSError as e:
        typer.echo(f"cannot read {file}: {e.strerror or e}")
        raise typer.Exit(1) from None
    items = parse_backlog(text)
    if not items:
        typer.echo("no open items (- [ ]) in the file")
        raise typer.Exit(1)
    n = f"{len(items)} item{'s' if len(items) != 1 else ''}"
    if dry_run:
        typer.echo(n)
        for it in items:
            typer.echo(f"{it.line}: {it.text}")
        return
    body = [{"id": str(i.line), "title": i.text[:80], "feature": i.text} for i in items]
    out = _api_call(lambda c: c.import_backlog(app_slug, file.name, body))
    typer.echo(f"backlog {out['ref']} running, {len(items)} items")


@backlog_app.command("list")
def backlog_list(
    app_slug: Annotated[str | None, typer.Option("--app", help="Filter by app")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
):
    """List backlogs."""
    import json

    rows = _api_call(lambda c: c.list_backlogs(app_slug))
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for b in rows:
        typer.echo(f"{b['ref']}  {b['status']}  {b['jobs_merged']}/{b['jobs_total']}  {b['title']}")


@backlog_app.command("show")
def backlog_show(
    ref: Annotated[str, typer.Argument(help="Backlog <app>:<bNNN>")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
):
    """Show a backlog: status, then its ordered items."""
    import json

    b = _api_call(lambda c: c.show_backlog(ref))
    if as_json:
        typer.echo(json.dumps(b, indent=2))
        return
    typer.echo(f"{b['ref']}  {b['status']}  {b['title']}")
    for it in b.get("items", []):
        typer.echo(f"{it['pos']}  {it['job_ref']}  {it['stage']}  ${it['cost_usd']}  {it['title']}")
    if b.get("blocked_job"):
        typer.echo(f"blocked on {b['blocked_job']}")


def _register_backlog_action(action: str) -> None:
    def cmd(ref: Annotated[str, typer.Argument(help="Backlog <app>:<bNNN>")]):
        _api_call(lambda c: c.backlog_action(ref, action))
        typer.echo(f"{action} sent to {ref}")

    cmd.__doc__ = f"Send '{action}' to a backlog."
    backlog_app.command(action)(cmd)


for _baction in ("resume", "skip", "cancel"):
    _register_backlog_action(_baction)


def _resolve_mode_or_exit(settings: Any) -> None:
    from swfactory.api.auth import ModeError, gce_metadata_reachable, resolve_mode

    try:
        resolve_mode(settings, on_gce=gce_metadata_reachable)
    except ModeError as e:
        typer.echo(str(e))
        raise typer.Exit(2) from None


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8080,
):
    """Run the API and dashboard."""
    from swfactory.api.auth import check_bind_host

    settings = get_settings()
    _resolve_mode_or_exit(settings)
    try:
        check_bind_host(settings, host)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(2) from None
    import uvicorn

    uvicorn.run("swfactory.api.app:app_factory", factory=True, host=host, port=port)


@app.command("login-url")
def login_url():
    """Print a one-time dashboard login URL (personal mode)."""
    code = _api_call(lambda c: c.login_code())
    typer.echo(f"{get_settings().api_url.rstrip('/')}/login?code={code}")


@app.command("worker")
def worker(
    worker_id: Annotated[str | None, typer.Option("--id", help="Worker id")] = None,
    fake_script: Annotated[Path | None, typer.Option(help="Scripted fake engine (YAML)")] = None,
    max_jobs: Annotated[
        int | None, typer.Option(help="Jobs run at once (default: definition)")
    ] = 2,
    fake_git_state: Annotated[
        Path | None, typer.Option(help="Fake git state file (tests)", hidden=True)
    ] = None,
    lease_s: Annotated[int, typer.Option(help="Job lease in seconds")] = 90,
    renew_s: Annotated[float, typer.Option(help="Lease renewal interval in seconds")] = 30,
    exit_when_idle: Annotated[bool, typer.Option(help="Exit when no job is left")] = False,
):
    """Run jobs from the queue."""
    from swfactory.worker import run_worker

    settings = get_settings()
    _resolve_mode_or_exit(settings)

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
            worker_id=worker_id or settings.worker_id,
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


models_app = typer.Typer(help="Model catalog commands", no_args_is_help=True)
app.add_typer(models_app, name="models")


def _models_table(infos: "list[Any]", probed: "dict[str, Any] | None" = None) -> list[str]:
    head: tuple[str, ...] = (
        "alias", "provider", "upstream_id", "strength", "available", "tools", "messages tools",
        "$ in/Mtok", "$ out/Mtok", "latency ms", "error class", "probed at",
    )  # fmt: skip

    def yn(v: bool | None) -> str:
        return "-" if v is None else ("yes" if v else "no")

    rows: list[tuple[str, ...]] = [head]
    for i in infos:
        at = (probed or {}).get(i.alias)
        rows.append(
            (
                i.alias, i.provider, i.upstream_id, i.strength, yn(i.available),
                yn(i.supports_tools), yn(i.messages_tools_ok), f"{i.price_in_per_mtok:f}",
                f"{i.price_out_per_mtok:f}", "-" if i.latency_ms is None else str(i.latency_ms),
                i.error_class or "-", at.strftime("%Y-%m-%d %H:%M") if at else "-",
            )
        )  # fmt: skip
    widths = [max(len(r[c]) for r in rows) for c in range(len(head))]
    return ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in rows]


def _model_infos(rows: list[dict[str, Any]]) -> list[Any]:
    from swfactory.models.catalog import ModelInfo

    out = []
    for r in rows:
        d = {k: v for k, v in r.items() if k in ModelInfo.__dataclass_fields__}
        for k in ("price_in_per_mtok", "price_out_per_mtok"):
            d[k] = Decimal(str(d[k]))
        out.append(ModelInfo(**d))
    return out


@models_app.command("refresh")
def models_refresh(
    alias: Annotated[list[str] | None, typer.Option("--alias", help="Only this alias")] = None,
):
    """Probe the gateway's models (through the API) and print the catalog."""
    rows = _api_call(lambda c: c.refresh_models(alias or None))
    for line in _models_table(_model_infos(rows)):
        typer.echo(line)


@models_app.command("list")
def models_list(
    available: Annotated[bool, typer.Option("--available", help="Only available models")] = False,
):
    """Show the stored model catalog."""
    rows = _api_call(lambda c: c.list_models(available))
    for line in _models_table(_model_infos(rows)):
        typer.echo(line)


improve_app = typer.Typer(
    help="Improver commands (read and write the database directly)", no_args_is_help=True
)
app.add_typer(improve_app, name="improve")


def _improve_db(fn: Any, *args: Any) -> Any:
    """Run ``fn(sessions, *args)`` against the database; the improver CLI has no API route."""
    settings = get_settings()

    async def run() -> Any:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            return await fn(make_sessionmaker(engine), *args)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _improve_status_data(sessions: Any) -> dict[str, Any] | None:
    from datetime import UTC, datetime

    from swfactory.definition.store import load_active
    from swfactory.improve.graph import improver_status

    async with sessions() as s:
        found = await load_active(s)
        if found is None:
            return None
        return await improver_status(s, found[1], now=datetime.now(UTC))


async def _improve_run_data(sessions: Any, role: str | None) -> dict[str, Any]:
    from swfactory.db.tables import Improvement
    from swfactory.definition.store import load_active
    from swfactory.domain.types import Role
    from swfactory.improve.graph import start_cycle

    async with sessions() as s, s.begin():
        found = await load_active(s)
        if found is None:
            return {"error": "no active definition"}
        defn = found[1]
        if not defn.factory.improver.bench_suite:
            return {"error": "set improver.bench_suite first"}
        ids = await start_cycle(
            s, defn, trigger="manual", role=Role(role) if role is not None else None
        )
        rows = [await s.get(Improvement, i) for i in ids]
    return {"started": [{"id": str(r.id), "role_group": r.role_group} for r in rows if r]}


async def _improve_list_data(sessions: Any, limit: int) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from swfactory.db.tables import Improvement

    async with sessions() as s:
        rows = await s.scalars(
            select(Improvement).order_by(Improvement.created_at.desc()).limit(limit)
        )
        return [
            {
                "id": str(r.id),
                "role_group": r.role_group,
                "trigger": r.trigger,
                "status": r.status,
                "pr_url": r.pr_url,
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in rows
        ]


@improve_app.command("status")
def improve_status():
    """Show the failures counted against the improver's trigger."""
    st = _improve_db(_improve_status_data)
    if st is None:
        typer.echo("no active definition")
        raise typer.Exit(1)
    state = "enabled" if st["enabled"] else "disabled"
    typer.echo(f"improver: {state}, benchmark suite {st['suite'] or '(not set)'}")
    typer.echo(f"failures since last improvement: {st['failures']} of {st['threshold']}")
    typer.echo(f"last improvement: {st['last_improvement_at'] or 'none yet'}")
    typer.echo(
        f"oldest failure: {st['oldest_failure_at'] or 'none'} "
        f"(age trigger after {st['max_failure_age_days']} days)"
    )
    typer.echo(f"next trigger: {st['due'] or 'none'}")
    for row in st["in_progress"]:
        typer.echo(f"in progress: {row['role_group']} ({row['status']})")


@improve_app.command("run")
def improve_run(
    role: Annotated[str | None, typer.Option("--role", help="Only this role group")] = None,
):
    """Start an improvement cycle now (even when the improver is disabled)."""
    from swfactory.domain.types import Role

    if role is not None:
        try:
            Role(role)
        except ValueError:
            typer.echo(f"unknown role '{role}'")
            raise typer.Exit(2) from None
    out = _improve_db(_improve_run_data, role)
    if "error" in out:
        typer.echo(out["error"])
        raise typer.Exit(1)
    if not out["started"]:
        typer.echo(
            "nothing to improve: no failure signals since the last improvement, "
            "or every role group already has an improvement open"
        )
        return
    for row in out["started"]:
        typer.echo(f"queued improvement {row['id']} for {row['role_group']}")
    if not get_settings().definition_source:
        typer.echo(
            "warning: no definition source is configured here (FACTORY_DEFINITION_SOURCE), so a "
            "worker started with this configuration has no improver ticker and nothing will pick "
            "these up; they fail after 72 h"
        )
    else:
        typer.echo("a worker picks them up on its next improver tick (hourly)")


@improve_app.command("list")
def improve_list(
    limit: Annotated[int, typer.Option(min=1, max=200, help="How many to show")] = 20,
):
    """List improvements, newest first."""
    for r in _improve_db(_improve_list_data, limit):
        typer.echo(
            f"{r['created_at']} {r['role_group']} {r['trigger']} {r['status']} {r['pr_url'] or '-'}"
        )


tokens_app = typer.Typer(
    help="API token commands (write the database directly)", no_args_is_help=True
)
app.add_typer(tokens_app, name="tokens")


def _by(by: str | None) -> str:
    return by or get_settings().local_user


@tokens_app.command("create")
def tokens_create(
    email: Annotated[str, typer.Option(help="Owner email")],
    name: Annotated[str, typer.Option(help="Token name")],
    days: Annotated[
        int, typer.Option(min=0, help="Days valid; 0 means no expiry")
    ] = TOKEN_DAYS_DEFAULT,
    by: Annotated[str | None, typer.Option(help="Who is acting")] = None,
):
    """Create an API token; the token is printed once."""
    from sqlalchemy import text

    from swfactory.api.auth import hash_token, new_token
    from swfactory.events import record_event

    settings = get_settings()
    token = new_token()

    async def run() -> str:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine).begin() as s:
                tid = str(
                    await s.scalar(
                        text(
                            "insert into app.api_tokens (email, name, token_hash, expires_at) "
                            "values (:e, :n, :h, case when :d > 0 "
                            "then now() + make_interval(days => :d) end) returning id"
                        ),
                        {"e": email, "n": name, "h": hash_token(token), "d": days},
                    )
                )
                await record_event(
                    s, None, "admin.changed", {"what": "token.create", "target": tid, "by": _by(by)}
                )
            return tid
        finally:
            await engine.dispose()

    tid = asyncio.run(run())
    typer.echo(f"token {token}")
    typer.echo(f"id {tid}")
    typer.echo("store the token now; it is not shown again")


@tokens_app.command("list")
def tokens_list(
    email: Annotated[str | None, typer.Option(help="Only this owner")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
):
    """List tokens (never the token or its hash)."""
    import json

    from sqlalchemy import select

    from swfactory.db.tables import ApiToken

    settings = get_settings()

    async def run() -> list[dict[str, Any]]:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            q = select(ApiToken).order_by(ApiToken.created_at)
            if email:
                q = q.where(ApiToken.email == email)
            async with make_sessionmaker(engine)() as s:
                rows = (await s.scalars(q)).all()
            return [
                {
                    "id": str(t.id),
                    "email": t.email,
                    "name": t.name,
                    "created_at": t.created_at.isoformat(timespec="seconds"),
                    "last_used_at": t.last_used_at.isoformat(timespec="seconds")
                    if t.last_used_at
                    else None,
                    "expires_at": t.expires_at.isoformat(timespec="seconds")
                    if t.expires_at
                    else None,
                    "revoked_at": t.revoked_at.isoformat(timespec="seconds")
                    if t.revoked_at
                    else None,
                }
                for t in rows
            ]
        finally:
            await engine.dispose()

    rows = asyncio.run(run())
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        typer.echo(
            f"{r['id']}  {r['email']}  {r['name']}  created {r['created_at']}  "
            f"used {r['last_used_at'] or '-'}  expires {r['expires_at'] or '-'}  "
            f"revoked {r['revoked_at'] or '-'}"
        )


@tokens_app.command("revoke")
def tokens_revoke(
    token_id: Annotated[str, typer.Argument(metavar="ID", help="Token id")],
    by: Annotated[str | None, typer.Option(help="Who is acting")] = None,
):
    """Revoke an open token."""
    import uuid

    from sqlalchemy import text

    from swfactory.events import record_event

    settings = get_settings()
    try:
        tid = uuid.UUID(token_id)
    except ValueError:
        typer.echo(f"'{token_id}' is not a token id")
        raise typer.Exit(1) from None

    async def run() -> bool:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine).begin() as s:
                hit = await s.scalar(
                    text(
                        "update app.api_tokens set revoked_at = now() "
                        "where id = :i and revoked_at is null returning id"
                    ),
                    {"i": tid},
                )
                if hit is None:
                    return False
                await record_event(
                    s,
                    None,
                    "admin.changed",
                    {"what": "token.revoke", "target": str(tid), "by": _by(by)},
                )
            return True
        finally:
            await engine.dispose()

    if not asyncio.run(run()):
        typer.echo("no open token with that id")
        raise typer.Exit(1)
    typer.echo(f"revoked {tid}")


notify_app = typer.Typer(help="Notifications", no_args_is_help=True)
app.add_typer(notify_app, name="notify")


@notify_app.command("test")
def notify_test():
    """Send one notify.test notice to check the webhook."""
    from swfactory.notify import Notifier

    settings = get_settings()
    notifier = Notifier.from_settings(settings)
    asyncio.run(notifier.send("notify.test", "webhook check from `factory notify test`"))
    typer.echo("sent" if settings.notify_webhook_url else "log only")


from swfactory.cli.run import run as _run_command  # noqa: E402

app.command("run")(_run_command)


laya_app = typer.Typer(help="Laya router training data", no_args_is_help=True)
app.add_typer(laya_app, name="laya")


@laya_app.command("export")
def laya_export(
    question: Annotated[
        str, typer.Option("--question", help="kind, size, risk, touches_ui or tier.<role>")
    ],
    out: Annotated[
        Path | None, typer.Option("--out", help="CSV path (default var/laya/<q>.csv)")
    ] = None,
):
    """Write the state and label CSV for one router question."""
    from swfactory.laya.export import (
        export_rows,
        format_source_counts,
        known_question,
        source_counts,
        write_csv,
    )

    if not known_question(question):
        typer.echo(f"unknown question '{question}'", err=True)
        raise typer.Exit(2)
    settings = get_settings()
    path = out or settings.laya_export_dir / f"{question}.csv"

    async def run() -> list[Any]:
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine)() as s:
                return await export_rows(s, question)
        finally:
            await engine.dispose()

    rows = asyncio.run(run())
    try:
        counts = write_csv(rows, path, question)
    except OSError as e:
        typer.echo(f"cannot write {path}: {e.strerror or e}", err=True)
        raise typer.Exit(1) from None
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    typer.echo(f"wrote {path}: {len(rows)} rows ({detail})")
    typer.echo(f"sources: {format_source_counts(source_counts(rows))}")


def _laya_run(fn):
    settings = get_settings()

    async def go():
        engine = make_engine(settings.sqlalchemy_url())
        try:
            async with make_sessionmaker(engine)() as s, s.begin():
                return await fn(s, settings)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@laya_app.command("import")
def laya_import(
    question: Annotated[
        str, typer.Option("--question", help="kind, size, risk, touches_ui or tier.<role>")
    ],
    file: Annotated[Path, typer.Option("--file", help="CSV with columns state,<question>")],
    source: Annotated[str, typer.Option("--source", help="Dataset name, e.g. synth-v1")],
    origin: Annotated[str, typer.Option("--origin", help="Where the data came from (URL)")],
    licence: Annotated[str, typer.Option("--licence", help="Licence of the data")],
):
    """Import synthetic training rows, tagged external:<source>. Exit 2 refused."""
    from swfactory.definition.loader import defaults_dir, load_definition
    from swfactory.definition.store import load_active
    from swfactory.laya.import_rows import ImportRefused, import_rows

    async def go(s, settings):
        active = await load_active(s)
        defn = active[1] if active else load_definition(str(defaults_dir())).definition
        if defn is None:
            raise ImportRefused("refused: no definition is loaded")
        ds = await import_rows(
            s,
            defn,
            question=question,
            path=file,
            source=source,
            origin_url=origin,
            licence=licence,
            imported_by=_who(),
        )
        return ds.name, ds.rows, ds.sha256

    try:
        name, n, sha = _laya_run(go)
    except ImportRefused as e:
        msg = str(e)
        typer.echo(msg if msg.startswith("refused") else f"refused: {msg}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"imported {name}: {n} rows for {question} (sha256 {sha[:12]})")


@laya_app.command("fit")
def laya_fit(
    question: Annotated[
        str, typer.Option("--question", help="kind, size, risk, touches_ui or tier.<role>")
    ],
):
    """Fit a candidate head in the router (never activates it). Exit 2 refused, 3 router down."""
    from swfactory.laya.export import format_source_counts, known_question
    from swfactory.laya.fit import (
        FitRefused,
        RouterRefused,
        RouterUnreachable,
        fit_question,
    )

    if not known_question(question):
        typer.echo(f"unknown question '{question}'", err=True)
        raise typer.Exit(2)

    async def go(s, settings):
        key = settings.laya_api_key.get_secret_value() if settings.laya_api_key else ""
        h = await fit_question(s, settings.laya_url or "", key, question)
        return h.version, h.rows, h.source_counts

    try:
        version, rows, counts = _laya_run(go)
    except FitRefused as e:
        msg = str(e)
        typer.echo(msg if msg.startswith("refused") else f"refused: {msg}", err=True)
        raise typer.Exit(2) from None
    except RouterUnreachable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(3) from None
    except RouterRefused as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"fitted {question} v{version} from {rows} rows ({format_source_counts(counts)}) "
        "(candidate, not active)"
    )


def _who() -> str:
    import getpass

    return getpass.getuser()


@laya_app.command("evaluate")
def laya_evaluate(
    question: Annotated[
        str, typer.Option("--question", help="kind, size, risk, touches_ui or tier.<role>")
    ],
    suite: Annotated[str, typer.Option("--suite", help="A holdout benchmark suite")],
    version: Annotated[
        int | None, typer.Option("--version", help="Head version (default the newest)")
    ] = None,
    repetitions: Annotated[
        int, typer.Option("--repetitions", help="Repetitions per task (at least 2)")
    ] = 2,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Profile (default the suite's first)")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation")] = False,
):
    """Benchmark fixed against laya for one head and say whether to switch. Exit 2 refused."""
    from swfactory.laya.evaluate import (
        EVAL_REPETITIONS,
        EvaluationRefused,
        report_table,
        run_evaluation,
        verdict_line,
    )

    if repetitions < EVAL_REPETITIONS:
        typer.echo(f"refused: at least {EVAL_REPETITIONS} repetitions are needed", err=True)
        raise typer.Exit(2)
    settings = get_settings()

    async def confirm(est, names):
        typer.echo(
            f"{' vs '.join(names)} on {suite}: {est.trials} trials, "
            f"estimate ${est.total_usd:.2f} (at most ${est.max_usd:.2f})"
        )
        return yes or typer.confirm("Start the run?", default=False)

    async def go():
        engine = make_engine(settings.sqlalchemy_url())
        try:
            return await run_evaluation(
                make_sessionmaker(engine),
                question=question,
                suite=suite,
                version=version,
                repetitions=repetitions,
                profile=profile,
                requested_by=_who(),
                confirm=confirm,
            )
        finally:
            await engine.dispose()

    try:
        out = asyncio.run(go())
    except EvaluationRefused as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    if out is None:
        typer.echo("not started")
        raise typer.Exit(1)
    verdict, rep, path = out
    for line in report_table(rep):
        typer.echo(line)
    typer.echo(f"report: {path}")
    typer.echo(verdict_line(verdict))


@laya_app.command("activate")
def laya_activate(
    question: Annotated[
        str, typer.Option("--question", help="kind, size, risk, touches_ui or tier.<role>")
    ],
    version: Annotated[int, typer.Option("--version", help="Head version to make active")],
):
    """Make an evaluated head the only active one (also how you roll back). Exit 2 refused."""
    from swfactory.laya.evaluate import ActivationRefused, activate_head
    from swfactory.laya.fit import RouterRefused, RouterUnreachable

    async def go(s, settings):
        key = settings.laya_api_key.get_secret_value() if settings.laya_api_key else ""
        await activate_head(s, settings.laya_url or "", key, question, version, by=_who())

    try:
        _laya_run(go)
    except ActivationRefused as e:
        typer.echo(f"refused: {e}", err=True)
        raise typer.Exit(2) from None
    except RouterUnreachable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(3) from None
    except RouterRefused as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"{question}@v{version} is now the active head; the router serves it only where the "
        "definition says mode: laya"
    )


@laya_app.command("status")
def laya_status():
    """Per question: mode, active head, newest candidate, labelled rows, last fit."""
    from sqlalchemy import select

    from swfactory.db.tables import LayaHead
    from swfactory.definition.store import load_active
    from swfactory.laya.export import (
        QUESTIONS_TRIAGE,
        export_rows,
        format_source_counts,
        question_ids,
        source_counts,
    )

    async def go(s, settings):
        active = await load_active(s)
        defn = active[1] if active else None
        questions = question_ids(defn) if defn else list(QUESTIONS_TRIAGE)
        heads = list(await s.scalars(select(LayaHead).order_by(LayaHead.version)))
        lines = []
        for q in questions:
            mine = [h for h in heads if h.question == q]
            on = next((h for h in mine if h.active), None)
            cands = [h for h in mine if not h.active and (on is None or h.version > on.version)]
            cand = cands[-1] if cands else None
            if defn is None:
                mode = "-"
            elif q.startswith("tier."):
                modes = {
                    p.roles[r].mode.value
                    for p in defn.profiles.values()
                    for r in p.roles
                    if f"tier.{r.value}" == q and p.roles[r].tiers
                }
                mode = ",".join(sorted(modes)) or "-"
            else:
                mode = defn.factory.triage.modes.get(q, defn.factory.triage.mode).value  # type: ignore[call-overload]
            erows = await export_rows(s, q)
            n = len(erows)
            ev = "none"
            if cand is not None and cand.evaluation:
                ev = str(cand.evaluation.get("verdict", "evaluated"))
            last = max((h.created_at for h in mine), default=None)
            cand_txt = f"{q}@v{cand.version} (eval: {ev})" if cand else "-"
            lines.append(
                f"{q}\tmode={mode}\tactive={q + '@v' + str(on.version) if on else '-'}"
                f"\tcandidate={cand_txt}"
                f"\trows={n} ({format_source_counts(source_counts(erows))})"
                f"\tlast_fit={last.isoformat(timespec='seconds') if last else '-'}"
            )
        return lines

    for line in _laya_run(go):
        typer.echo(line)
