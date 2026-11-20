"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from swfactory.api.auth import (
    SESSION_COOKIE,
    IapKeys,
    LoginCodes,
    ModeError,
    Principal,
    UserDirectory,
    current_principal,
    gce_metadata_reachable,
    load_personal_token,
    origin_allowed,
    resolve_mode,
    session_value,
)
from swfactory.api.routes import apps as apps_routes
from swfactory.api.routes import backlogs as backlogs_routes
from swfactory.api.routes import bench as bench_routes
from swfactory.api.routes import board as board_routes
from swfactory.api.routes import definition as definition_routes
from swfactory.api.routes import hooks as hooks_routes
from swfactory.api.routes import jobs as jobs_routes
from swfactory.api.routes import metrics as metrics_routes
from swfactory.api.routes import models as models_routes
from swfactory.api.routes import profiles as profiles_routes
from swfactory.api.routes import runs as runs_routes
from swfactory.api.sse import router as sse_router
from swfactory.artifacts import ArtifactStore
from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.definition.models import Defaults
from swfactory.definition.store import DefinitionStore, NoActiveDefinition
from swfactory.definition.sync import definition_repo_slug
from swfactory.domain.types import FactoryMode
from swfactory.events import EventBus
from swfactory.github.broker import BrokerAuth, ServerDefinitionWriter
from swfactory.health import DETECT_EVERY_S, live_workers
from swfactory.models.catalog import Catalog
from swfactory.notify import Notifier
from swfactory.sandbox.disk import disk_paths, disk_usage_pct
from swfactory.settings import Settings
from swfactory.watchdog import watchdog_tick

log = structlog.get_logger(__name__)

__all__ = ["ApiDeps", "ModeError", "SECURITY_HEADERS", "app_factory", "create_app"]

SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class _WebFiles(StaticFiles):
    """StaticFiles for the dashboard; anything but GET and HEAD is a 404."""

    async def check_config(self) -> None:
        return None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=404)
        await super().__call__(scope, receive, send)


class DefinitionWriter(Protocol):
    """Writes definition files (M7.4a supplies the implementations)."""

    async def save_file(self, path: str, content: str, *, message: str, author: str) -> Any: ...

    async def save_files(self, files: Mapping[str, str], *, message: str, author: str) -> Any: ...


@dataclass(frozen=True)
class ApiDeps:
    sessions: async_sessionmaker[AsyncSession]
    events: EventBus
    artifacts: ArtifactStore
    catalog: Catalog
    definitions: DefinitionStore
    users: UserDirectory
    writer: DefinitionWriter | None
    settings: Settings


async def alive_workers(deps: ApiDeps) -> int:
    """Workers with a heartbeat within two lease-renew intervals."""
    within = 2 * Defaults().lease_renew_s
    async with deps.sessions() as s:
        return await live_workers(s, within_s=within)


async def _stuck_loop(app: FastAPI, interval_s: float = DETECT_EVERY_S) -> None:
    """Run the watchdog here, not in the worker, so a wedged worker cannot hush it."""
    notifier = Notifier.from_settings(app.state.settings)
    while True:
        await asyncio.sleep(interval_s)
        d: ApiDeps | None = app.state.deps
        if d is None:
            continue
        try:
            _, defn = await d.definitions.active()
            async with d.sessions() as s:
                await watchdog_tick(
                    s,
                    d.events,
                    notifier,
                    defn=defn,
                    settings=d.settings,
                    now=datetime.now(UTC),
                )
        except Exception as exc:
            log.warning("watchdog.failed", check="loop", error=str(exc))


class _OriginGuard(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in MUTATING and not origin_allowed(
            request.headers.get("origin"), self._settings
        ):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        return await call_next(request)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response


def _server_writer(settings: Settings, mode: FactoryMode) -> DefinitionWriter | None:
    """Server mode: definition edits open PRs, with tokens from the broker (no App key here)."""
    if mode is not FactoryMode.SERVER:
        return None
    slug = definition_repo_slug(settings.definition_source)
    if slug is None:
        return None
    if not settings.broker_token_file:
        log.warning("definition_writer.disabled", reason="FACTORY_BROKER_TOKEN_FILE is not set")
        return None
    return ServerDefinitionWriter(BrokerAuth(settings.broker_url, settings.broker_token_file), slug)


def _build_deps(settings: Settings, mode: FactoryMode) -> tuple[ApiDeps, Any]:
    engine = make_engine(
        settings.sqlalchemy_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        statement_timeout_s=settings.db_statement_timeout_s,
    )
    sessions = make_sessionmaker(engine)
    definitions = DefinitionStore(sessions, mode=mode)
    deps = ApiDeps(
        sessions=sessions,
        events=EventBus(sessions, settings.libpq_dsn()),
        artifacts=ArtifactStore(sessions, settings.artifacts_dir),
        catalog=Catalog(sessions, None, definitions),
        definitions=definitions,
        users=UserDirectory(sessions),
        writer=_server_writer(settings, mode),
        settings=settings,
    )
    return deps, engine


def create_app(
    settings: Settings,
    deps: ApiDeps | None = None,
    *,
    on_gce: Callable[[], bool] = gce_metadata_reachable,
    iap_keys: IapKeys | None = None,
) -> FastAPI:
    mode = resolve_mode(settings, on_gce=on_gce)
    log.info("startup", mode=mode.value)
    if mode is FactoryMode.SERVER and not settings.iap_audience:
        raise RuntimeError("server mode requires FACTORY_IAP_AUDIENCE")
    if deps is not None and deps.writer is None:
        deps = replace(deps, writer=_server_writer(settings, mode))
    personal_token = load_personal_token(settings) if mode is FactoryMode.PERSONAL else ""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if app.state.deps is None:
            app.state.deps, engine = _build_deps(settings, mode)
        detector = asyncio.create_task(_stuck_loop(app))
        try:
            yield
        finally:
            detector.cancel()
            await asyncio.gather(detector, return_exceptions=True)
            if engine is not None:
                await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.mode = mode
    app.state.deps = deps
    app.state.personal_token = personal_token
    app.state.login_codes = LoginCodes()
    app.state.iap_keys = iap_keys or IapKeys()

    app.add_middleware(_OriginGuard, settings=settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(_SecurityHeaders)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        d: ApiDeps = request.app.state.deps
        db_ok = definition_ok = False
        try:
            async with d.sessions() as s:
                await s.execute(text("SELECT 1"))
            db_ok = True
            try:
                await d.definitions.active()
                definition_ok = True
            except NoActiveDefinition:
                pass
        except Exception as e:
            log.warning("readyz.db_failed", error=str(e))
        workers = 0
        if db_ok:
            try:
                workers = await alive_workers(d)
            except Exception as e:
                log.warning("readyz.workers_failed", error=str(e))
        try:
            pct, path = disk_usage_pct(disk_paths(settings))
        except OSError:
            pct, path = 0, "/"
        ready = db_ok and definition_ok and (mode is not FactoryMode.SERVER or workers > 0)
        return JSONResponse(
            {
                "ok": ready,
                "mode": mode.value,
                "db": db_ok,
                "definition": definition_ok,
                "workers": workers,
                "disk": {"pct": pct, "path": path, "high": pct > settings.disk_high_water_pct},
            },
            status_code=200 if ready else 503,
        )

    @app.get("/login")
    async def login(request: Request, code: str = "") -> Response:
        if mode is not FactoryMode.PERSONAL:
            raise HTTPException(status_code=404)
        if not code or not request.app.state.login_codes.redeem(code):
            raise HTTPException(status_code=401, detail="unauthorized")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_value(personal_token),
            httponly=True,
            samesite="strict",
            path="/",
            secure=False,
        )
        return response

    api = APIRouter(prefix="/api")

    @api.get("/me")
    async def me(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, str]:
        return {
            "email": principal.email,
            "role": principal.role,
            "mode": mode.value,
            "via": principal.via,
        }

    @api.post("/login-code")
    async def login_code(request: Request) -> dict[str, str]:
        if mode is not FactoryMode.PERSONAL:
            raise HTTPException(status_code=404)
        principal = await current_principal(request)
        if principal.via != "token":
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"code": request.app.state.login_codes.issue()}

    api.include_router(sse_router)
    api.include_router(board_routes.router)
    api.include_router(jobs_routes.router)
    for r in (
        apps_routes,
        backlogs_routes,
        bench_routes,
        models_routes,
        profiles_routes,
        definition_routes,
        metrics_routes,
        runs_routes,
    ):
        api.include_router(r.router)
    app.include_router(api)
    app.include_router(hooks_routes.router)  # before /hooks/{name} (M8.8b)
    app.mount("/", _WebFiles(directory=WEB_DIR, html=True, check_dir=False), name="web")
    return app


def app_factory() -> FastAPI:
    from swfactory.settings import get_settings

    return create_app(get_settings())
