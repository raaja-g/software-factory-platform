"""Signed webhooks: verification, replay protection, filters and fenced feature rendering."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import Any

from swfactory.definition.models import WebhookDef
from swfactory.roles.fence import fence_untrusted

TIMESTAMP_TOLERANCE_S = 300
MAX_VALUE_CHARS = 4000
TOKEN_HEADER = "x-factory-token"

_DEFAULT_ID_HEADER = {
    "github": "x-github-delivery",
    "standard_webhooks": "webhook-id",
    "sentry": "request-id",
}
_UNTIMESTAMPED = {"github", "sentry"}


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _hmac_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _fresh(ts: int, now: int) -> bool:
    return abs(now - ts) <= TIMESTAMP_TOLERANCE_S


def _sw_key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_") :], validate=True)
        except ValueError:
            return secret.encode()
    return secret.encode()


def _sw_signature(secret: str, msg_id: str, ts: str, body: bytes) -> str:
    signed = f"{msg_id}.{ts}.".encode() + body
    return base64.b64encode(hmac.new(_sw_key(secret), signed, hashlib.sha256).digest()).decode()


def _verify_standard(secret: str, h: dict[str, str], body: bytes, now: int) -> bool:
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not msg_id or not ts or not sigs or not ts.isdigit() or not _fresh(int(ts), now):
        return False
    want = _sw_signature(secret, msg_id, ts, body)
    ok = False
    for part in sigs.split():  # several space-separated "v1,<b64>" entries; check them all
        version, _, sig = part.partition(",")
        if version == "v1" and _eq(want, sig):
            ok = True
    return ok


def _verify_stripe(secret: str, h: dict[str, str], body: bytes, now: int) -> bool:
    raw = h.get("stripe-signature")
    if not raw:
        return False
    ts = ""
    sigs: list[str] = []
    for part in raw.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    if not ts.isdigit() or not _fresh(int(ts), now):
        return False
    want = _hmac_hex(secret.encode(), ts.encode() + b"." + body)
    ok = False
    for sig in sigs:
        if _eq(want, sig):
            ok = True
    return ok


def verify_webhook(
    hook: WebhookDef, secret: str, headers: Mapping[str, str], body: bytes, *, now: int
) -> bool:
    h = _lower(headers)
    if hook.auth_mode == "token":
        got = h.get(TOKEN_HEADER)
        return got is not None and _eq(got, secret)
    scheme = hook.signature_scheme
    if scheme == "github":
        got = h.get("x-hub-signature-256")
        want = "sha256=" + _hmac_hex(secret.encode(), body)
        return got is not None and _eq(want, got)
    if scheme == "sentry":
        got = h.get("sentry-hook-signature")
        return got is not None and _eq(_hmac_hex(secret.encode(), body), got)
    if scheme == "standard_webhooks":
        return _verify_standard(secret, h, body, now)
    if scheme == "stripe":
        return _verify_stripe(secret, h, body, now)
    return False


def delivery_ids(hook: WebhookDef, headers: Mapping[str, str], body: bytes) -> list[str]:
    """Ids to dedupe on; any already-seen id makes the delivery a duplicate."""
    h = _lower(headers)
    digest = "body:" + hashlib.sha256(body).hexdigest()
    scheme = hook.signature_scheme if hook.auth_mode == "signature" else None
    name = (hook.delivery_id_header or "").lower() or _DEFAULT_ID_HEADER.get(scheme or "", "")
    ident: str | None = h.get(name) if name else None
    if not ident and scheme == "stripe" and not hook.delivery_id_header:
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("id"), str):
            ident = doc["id"]
    ids = [ident] if ident else []
    if scheme in _UNTIMESTAMPED or scheme is None or not ident:
        ids.append(digest)
    return ids


def _lookup(payload: Any, path: str) -> tuple[bool, Any]:
    cur = payload
    for key in path.split("."):
        if isinstance(cur, Mapping) and key in cur:
            cur = cur[key]
        else:
            return False, None
    return True, cur


def _values(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list | tuple) else [v]


def match_filter(filter: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:  # noqa: A002
    for path, cond in filter.items():
        found, value = _lookup(payload, path)
        have = _values(value) if found else []
        if isinstance(cond, list):
            cond = {"in": cond}
        if not isinstance(cond, Mapping):
            cond = {"in": [cond]}
        for op, wanted in cond.items():
            wanted = _values(wanted)
            hit = any(x in wanted for x in have)
            if op == "in" and not hit:
                return False
            if op == "not_in" and hit:
                return False
            if op not in ("in", "not_in"):
                return False
    return True


_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def render_feature(template: str, payload: Mapping[str, Any], *, label: str) -> str:
    """Splice each ``{{ path }}`` as fenced untrusted text; nothing is ever evaluated."""

    def sub(m: re.Match[str]) -> str:
        path = m.group(1)
        found, value = _lookup(payload, path)
        if not found or value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, sort_keys=True)
        return fence_untrusted(f"{label}: {path}", text[:MAX_VALUE_CHARS])

    return _TOKEN.sub(sub, template)


def sign_headers(
    hook: WebhookDef, secret: str, body: bytes, *, now: int, msg_id: str = "msg_cli"
) -> dict[str, str]:
    """Headers a sender would add (used by the exit check and tests)."""
    if hook.auth_mode == "token":
        return {TOKEN_HEADER: secret}
    ts = str(now)
    match hook.signature_scheme:
        case "github":
            return {"X-Hub-Signature-256": "sha256=" + _hmac_hex(secret.encode(), body)}
        case "sentry":
            return {"Sentry-Hook-Signature": _hmac_hex(secret.encode(), body)}
        case "standard_webhooks":
            return {
                "webhook-id": msg_id,
                "webhook-timestamp": ts,
                "webhook-signature": "v1," + _sw_signature(secret, msg_id, ts, body),
            }
        case _:
            sig = _hmac_hex(secret.encode(), ts.encode() + b"." + body)
            return {"Stripe-Signature": f"t={ts},v1={sig}"}


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "sign":
        print("usage: python -m swfactory.automations.webhooks sign <hook> <file>", file=sys.stderr)
        return 2
    import asyncio

    from swfactory.db.engine import make_engine, make_sessionmaker
    from swfactory.definition.store import DefinitionStore
    from swfactory.settings import get_settings

    async def load() -> WebhookDef | None:
        settings = get_settings()
        engine = make_engine(settings.sqlalchemy_url())
        try:
            _, defn = await DefinitionStore(make_sessionmaker(engine), mode=settings.mode).active()
        finally:
            await engine.dispose()
        return defn.webhooks.get(argv[1])

    hook = asyncio.run(load())
    if hook is None:
        print(f"unknown webhook {argv[1]!r}", file=sys.stderr)
        return 1
    secret = os.environ.get(hook.secret_env)
    if not secret:
        print(f"{hook.secret_env} is not set", file=sys.stderr)
        return 1
    with open(argv[2], "rb") as f:
        body = f.read()
    for k, v in sign_headers(hook, secret, body, now=int(time.time())).items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
