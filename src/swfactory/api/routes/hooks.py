"""POST /hooks/_github (the signed GitHub webhook, registered first) and POST /hooks/{name}."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from swfactory.automations.scheduler import automation_block_reason
from swfactory.automations.webhooks import (
    delivery_ids,
    match_filter,
    render_feature,
    verify_webhook,
)
from swfactory.db.repo import record_delivery
from swfactory.definition.store import NoActiveDefinition
from swfactory.github.intake import (
    GitHubEventHandlers,
    HookCtx,
    default_handlers,
    make_client_factory,
    verify_github_signature,
)
from swfactory.queue import UnknownApp, enqueue_job

log = structlog.get_logger(__name__)

router = APIRouter()
MAX_BODY = 1_000_000


def _handlers(request: Request) -> GitHubEventHandlers:
    st = request.app.state
    if getattr(st, "github_handlers", None) is None:
        st.github_handlers = default_handlers()
    return st.github_handlers


def _clients(request: Request):
    st = request.app.state
    if getattr(st, "github_clients", None) is None:
        st.github_clients = make_client_factory(st.settings, broker=True)
    return st.github_clients


@router.post("/hooks/_github")
async def github_hook(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    secret = settings.github_webhook_secret
    if secret is None:
        return JSONResponse({"detail": "github webhook not configured"}, status_code=503)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    if not verify_github_signature(
        secret.get_secret_value().encode(), body, request.headers.get("x-hub-signature-256")
    ):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return JSONResponse({"detail": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "invalid json"}, status_code=400)
    if (payload.get("sender") or {}).get("type") == "Bot":
        return JSONResponse({"skipped": "bot"}, status_code=202)
    event = request.headers.get("x-github-event", "")
    handler = _handlers(request).get(event, str(payload.get("action") or ""))
    if handler is None:
        return JSONResponse({"ignored": True}, status_code=202)
    delivery = request.headers.get("x-github-delivery") or ""
    deps = request.app.state.deps
    after = None
    try:
        _, defn = await deps.definitions.active()
        async with deps.sessions.begin() as s:
            fresh_id = await record_delivery(s, source="github", delivery_id=delivery)
            fresh_body = await record_delivery(
                s, source="github-body", delivery_id=hashlib.sha256(body).hexdigest()
            )
            if not (fresh_id and fresh_body):
                return JSONResponse({"duplicate": True}, status_code=200)
            ctx = HookCtx(s, deps.sessions, defn, _clients(request), settings)
            after = await handler(ctx, payload)
    except (NoActiveDefinition, UnknownApp) as exc:
        log.warning("github.hook_unavailable", gh_event=event, error=repr(exc))
        return JSONResponse({"detail": "not ready"}, status_code=503)
    except Exception as exc:
        log.warning("github.hook_failed", gh_event=event, error=repr(exc))
        return JSONResponse({"detail": "handler failed"}, status_code=500)
    if after is not None:
        try:
            await after()
        except Exception as exc:
            log.warning("github.hook_after_failed", gh_event=event, error=repr(exc))
    return JSONResponse({"ok": True}, status_code=202)


@router.post("/hooks/{name}")
async def named_hook(name: str, request: Request) -> JSONResponse:
    deps = request.app.state.deps
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        return JSONResponse({"detail": "not ready"}, status_code=503)
    hook = defn.webhooks.get(name)
    if hook is None or not hook.enabled:
        return JSONResponse({"detail": "not found"}, status_code=404)
    secret = os.environ.get(hook.secret_env)
    if not secret:
        log.warning("webhook.secret_missing", webhook=name)
        return JSONResponse({"detail": "webhook secret not configured"}, status_code=500)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    if not verify_webhook(hook, secret, request.headers, body, now=int(time.time())):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return JSONResponse({"detail": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "invalid json"}, status_code=400)
    ids = delivery_ids(hook, request.headers, body)
    jobs: list[str] = []
    skipped: dict[str, str] = {}
    try:
        async with deps.sessions.begin() as s:
            fresh = [await record_delivery(s, source=f"hook:{name}", delivery_id=i) for i in ids]
            if not all(fresh):
                return JSONResponse({"duplicate": True}, status_code=200)
            now = datetime.now(UTC)
            for auto_name, auto in defn.automations.items():
                t = auto.trigger
                if not auto.enabled or t.provider != "webhook":
                    continue
                if name not in t.filter.get("webhook_ids", []):
                    continue
                rest = {k: v for k, v in t.filter.items() if k != "webhook_ids"}
                if not match_filter(rest, payload):
                    continue
                requested_by = f"webhook:{name}/{auto_name}"
                reason = await automation_block_reason(
                    s, name=auto_name, auto=auto, requested_by=requested_by, now=now
                )
                if reason is not None:
                    log.info("automation.skipped", name=auto_name, reason=reason)
                    skipped[auto_name] = reason
                    continue
                job = await enqueue_job(
                    s,
                    app_slug=auto.app,
                    feature=render_feature(auto.feature_template, payload, label=f"webhook {name}"),
                    title=None,
                    source="webhook",
                    source_ref=f"{name}:{ids[0]}",
                    requested_by=requested_by,
                    profile_name=auto.profile,
                )
                jobs.append(str(job.id))
    except (NoActiveDefinition, UnknownApp) as exc:
        log.warning("webhook.unavailable", webhook=name, error=repr(exc))
        return JSONResponse({"detail": "not ready"}, status_code=503)
    except Exception as exc:
        log.warning("webhook.failed", webhook=name, error=repr(exc))
        return JSONResponse({"detail": "handler failed"}, status_code=500)
    return JSONResponse({"jobs": jobs, "skipped": skipped}, status_code=202)
