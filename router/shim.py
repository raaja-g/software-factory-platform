"""Router sidecar entry point (M11.1).

Runs laya-serve's own ASGI app and passes every request through. It adds only:
  * the response header `x-factory-router-version: laya <version>` on every response, and
  * `head_version` on every answer of `/v1/systemone` and `/v1/systemone/batch`: `<question>@v<n>`
    when the question is served by a fitted head, else null (M11.8),
  * `POST /v1/factory/fit`, `POST /v1/factory/heads/activate` and `GET /v1/factory/heads`, and
  * `pins: {question_id: version}` on the answer routes, to serve one question from a candidate.
A "head" here is laya's own calibration (temperature per question kind and a binning map) fitted
on the labelled rows: laya 0.4.31 has no head trainer for its encoder, only `fit_temperatures` and
`fit_binning`. It changes confidence and route, never which option wins (see router/README.md).
It refuses to start without LAYA_API_KEY, before any heavy import.
"""

import asyncio
import copy
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

ANSWER_PATHS = ("/v1/systemone", "/v1/systemone/batch")
HEADER = "x-factory-router-version"


def router_version() -> str:
    return f"laya {version('laya')}"


def add_head_version(payload: object, versions: Mapping[str, str | None] | None = None) -> object:
    """Set `head_version` on every answer, and null on each result (a result has many answers)."""
    versions = versions or {}

    def one(item: dict[str, Any]) -> None:
        item.setdefault("head_version", None)
        answers = item.get("answers")
        if isinstance(answers, dict):
            for q, a in answers.items():
                if isinstance(a, dict):
                    a["head_version"] = versions.get(q)

    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            for item in payload["results"]:
                if isinstance(item, dict):
                    one(item)
        else:
            one(payload)
    return payload


# --- fitted heads (M11.8) -----------------------------------------------------------------

HEADS_DIR = Path(os.environ.get("LAYA_HEADS_DIR", "/heads"))
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
INFER_LOCK = threading.RLock()  # one torch user at a time: inference, a fit, a head swap


class HeadError(Exception):
    pass


def questions_sha256(definition: Mapping[str, Any]) -> str:
    """Same canonical hash as swfactory.laya.fit.questions_sha256."""
    blob = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class HeadStore:
    """`<root>/<question>/v<n>/{train.csv,questions.json,meta.json,head.json}`, `<q>/active`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _qdir(self, question: str) -> Path:
        if not _NAME.match(question):
            raise HeadError(f"bad question id {question!r}")
        return self.root / question

    def _vdir(self, question: str, v: int) -> Path:
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise HeadError(f"bad version {v!r}")
        return self._qdir(question) / f"v{v}"

    def save(
        self,
        question: str,
        v: int,
        *,
        csv_text: str,
        questions: Mapping[str, Any],
        payload: Mapping[str, Any],
        metrics: Mapping[str, Any],
        router_version: str,
    ) -> str:
        d = self._vdir(question, v)
        d.mkdir(parents=True, exist_ok=True)
        head = json.dumps(payload, sort_keys=True).encode()
        (d / "train.csv").write_text(csv_text, encoding="utf-8")
        (d / "questions.json").write_text(json.dumps(questions, sort_keys=True), encoding="utf-8")
        (d / "head.json").write_bytes(head)
        meta = {
            "question_id": question,
            "version": v,
            "head_sha256": _sha(head),
            "questions_sha256": questions_sha256(questions[question]),
            "metrics": dict(metrics),
            "router_version": router_version,
        }
        (d / "meta.json").write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        return meta["head_sha256"]

    def meta(self, question: str, v: int) -> dict[str, Any] | None:
        try:
            doc = json.loads((self._vdir(question, v) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def payload(self, question: str, v: int) -> dict[str, Any] | None:
        """The head's data, only when it still hashes to what meta.json recorded."""
        m = self.meta(question, v)
        try:
            raw = (self._vdir(question, v) / "head.json").read_bytes()
        except OSError:
            return None
        if m is None or _sha(raw) != m.get("head_sha256"):
            return None
        return json.loads(raw)

    def activate(self, question: str, v: int, head_sha256: str) -> None:
        m = self.meta(question, v)
        if m is None:
            raise HeadError(f"no head {question}@v{v}")
        if self.payload(question, v) is None or m.get("head_sha256") != head_sha256:
            raise HeadError(f"head {question}@v{v} does not hash to {head_sha256}")
        d = self._qdir(question)
        tmp = d / ".active.tmp"
        tmp.write_text(str(v), encoding="utf-8")
        os.replace(tmp, d / "active")

    def active(self, question: str) -> int | None:
        try:
            return int((self._qdir(question) / "active").read_text(encoding="utf-8").strip())
        except (OSError, ValueError, HeadError):
            return None

    def listing(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for qd in sorted(p for p in self.root.iterdir() if p.is_dir()):
            active = self.active(qd.name)
            for vd in sorted(
                qd.glob("v*"), key=lambda p: int(p.name[1:] or 0) if p.name[1:].isdigit() else 0
            ):
                if not vd.name[1:].isdigit():
                    continue
                m = self.meta(qd.name, int(vd.name[1:]))
                if m is None:
                    continue
                out.append(
                    {
                        "question_id": qd.name,
                        "version": m["version"],
                        "active": active == m["version"],
                        "head_sha256": m["head_sha256"],
                        "questions_sha256": m["questions_sha256"],
                        "metrics": m.get("metrics", {}),
                    }
                )
        return out

    def resolve(
        self, questions: Mapping[str, Any], pins: Mapping[str, Any] | None = None
    ) -> dict[str, int | None]:
        """The head version that answers each question (None: the unfitted encoder).

        A pin beats the active head. A head is used only if its files still hash to its
        recorded sha and its question definition equals the one in this request.
        """
        pins = pins or {}
        out: dict[str, int | None] = {}
        for q, definition in questions.items():
            v: int | None
            try:
                v = pins[q] if q in pins else self.active(q)
                ok = (
                    v is not None
                    and isinstance(definition, dict)
                    and (m := self.meta(q, v)) is not None
                    and m.get("questions_sha256") == questions_sha256(definition)
                    and self.payload(q, v) is not None
                )
            except HeadError:
                ok, v = False, None
            out[q] = v if ok else None
        return out


def head_labels(versions: Mapping[str, int | None]) -> dict[str, str | None]:
    return {q: (f"{q}@v{v}" if v else None) for q, v in versions.items()}


class Serving:
    """Applies one calibration head at a time to the loaded agents around each prediction."""

    def __init__(self, router: Any, store: HeadStore) -> None:
        self.router = router
        self.store = store
        self.plan: dict[str, int | None] | None = None  # set by the middleware per request
        self._baseline: list[tuple[Any, dict[str, Any]]] | None = None

    # laya touchpoints, replaced in tests
    def agents(self) -> list[Any]:
        return list((getattr(self.router, "_agents", None) or {}).values())

    def export(self, agent: Any) -> dict[str, Any]:
        from laya.calibrate import calibration_payload

        t = getattr(agent, "temperature_raw", None) or agent.temperature
        b = getattr(agent, "temperature_by_options_raw", None) or agent.temperature_by_options
        return calibration_payload(
            t, b, binning_map=copy.deepcopy(getattr(agent, "binning_map", None))
        )

    def install(self, agent: Any, payload: Mapping[str, Any]) -> None:
        from laya.calibrate import apply_calibration_payload

        apply_calibration_payload(agent, dict(payload))

    def baseline(self) -> list[tuple[Any, dict[str, Any]]]:
        if self._baseline is None:
            self._baseline = [(a, self.export(a)) for a in self.agents()]
        return self._baseline

    def restore(self) -> None:
        for agent, payload in self.baseline():
            self.install(agent, payload)

    def use(self, question: str, version: int | None) -> None:
        self.restore()
        if version is not None:
            payload = self.store.payload(question, version)
            if payload is None:
                raise HeadError(f"head {question}@v{version} is not loadable")
            for agent in self.agents():
                self.install(agent, payload)

    def _groups(self, questions: Mapping[str, Any]) -> list[tuple[str, int | None, dict[str, Any]]]:
        """(question carrying the head, version, questions): one group per headed question, and
        one group for every question served unfitted."""
        plan = self.plan if self.plan is not None else self.store.resolve(questions)
        unfitted = {q: d for q, d in questions.items() if plan.get(q) is None}
        groups: list[tuple[str, int | None, dict[str, Any]]] = []
        if unfitted:
            groups.append((next(iter(unfitted)), None, unfitted))
        for q, d in questions.items():
            if plan.get(q) is not None:
                groups.append((q, plan[q], {q: d}))
        return groups

    def run(
        self, questions: Mapping[str, Any], call: Callable[[dict[str, Any]], list[dict[str, Any]]]
    ):
        """`call(subset)` returns one result dict per state; answers of the groups are merged."""
        with INFER_LOCK:
            self.baseline()  # first use: remember the shipped calibration
            merged: list[dict[str, Any]] | None = None
            try:
                for q, version, subset in self._groups(questions):
                    self.use(q, version)
                    part = call(subset)
                    if merged is None:
                        merged = part
                    else:
                        for base, extra in zip(merged, part, strict=True):
                            base["answers"].update(extra.get("answers") or {})
            finally:
                self.restore()
            return merged if merged is not None else call(dict(questions))

    def wrap(self) -> None:
        router = self.router
        orig_predict = router.predict
        orig_batch = getattr(router, "predict_batch", None)

        def predict(state: Any = None, questions: Mapping[str, Any] | None = None, **kw: Any):
            if not questions:
                return orig_predict(state, questions, **kw)
            out = self.run(questions, lambda sub: [orig_predict(state, sub, **kw)])
            return out[0]

        router.predict = predict
        if orig_batch is not None:

            def predict_batch(requests: Any, **kw: Any):
                reqs = list(requests)
                qs = reqs[0].get("questions") if reqs else None
                if not qs:
                    return orig_batch(requests, **kw)

                def call(sub: dict[str, Any]) -> list[dict[str, Any]]:
                    return list(orig_batch([{**r, "questions": sub} for r in reqs], **kw))

                return self.run(qs, call)

            router.predict_batch = predict_batch


def parse_csv(csv_text: str, question: str) -> list[tuple[str, str]]:
    rd = csv.DictReader(io.StringIO(csv_text, newline=""))
    if rd.fieldnames is None or "state" not in rd.fieldnames or question not in rd.fieldnames:
        raise HeadError(f"csv needs the columns state and {question}")
    return [(r["state"], r[question]) for r in rd]


def fit_head(serving: Serving, body: Mapping[str, Any]) -> dict[str, Any]:
    """Fit laya's temperature and binning on the rows and store them as a candidate."""
    import numpy as np
    from laya.calibrate import records_from_labeled

    q = str(body["question_id"])
    version = int(body["version"])
    questions = body["questions"]
    definition = questions[q]
    labels = list((definition.get("criteria") or {}).keys())
    rows = parse_csv(str(body["csv"]), q)
    pairs = []
    for state, label in rows:
        if label not in labels:
            continue
        try:
            st: Any = json.loads(state)
        except ValueError:
            st = state
        pairs.append((st, {q: definition}, {q: [1.0 if lab == label else 0.0 for lab in labels]}))
    if len(pairs) < 2:
        raise HeadError("fewer than 2 rows carry a known label")
    with INFER_LOCK:
        serving.baseline()
        agent = serving.agents()[0]
        try:
            serving.restore()  # fit from the shipped calibration, never from another head
            records = records_from_labeled(agent, pairs)
            result = agent.fit_temperatures(records)
            agent.fit_binning(records)
            payload = serving.export(agent)
        finally:
            serving.restore()
    right = sum(
        1 for _, logits, target, _k in records if int(np.argmax(logits)) == int(np.argmax(target))
    )
    metrics = {
        "rows": len(records),
        "skipped_rows": len(rows) - len(pairs),
        "encoder_accuracy": round(right / len(records), 4),
        "temperature": [float(t) for t in result.get("temperature", [])],
        "kind": "calibration",
    }
    sha = serving.store.save(
        q,
        version,
        csv_text=str(body["csv"]),
        questions=questions,
        payload=payload,
        metrics=metrics,
        router_version=router_version(),
    )
    return {"head_version": f"{q}@v{version}", "metrics": metrics, "head_sha256": sha}


def build_app():
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response
    from laya.serve import build_router, create_app

    router = build_router()
    store = HeadStore(HEADS_DIR)
    serving = Serving(router, store)
    serving.wrap()
    app = create_app(router)
    label = router_version()
    api_key = os.environ.get("LAYA_API_KEY", "")
    plan_lock = asyncio.Lock()

    def authorized(request: Request) -> bool:
        got = (request.headers.get("authorization") or "").encode("utf-8", "surrogateescape")
        return hmac.compare_digest(got, f"Bearer {api_key}".encode())

    @app.post("/v1/factory/fit")
    async def fit(request: Request):  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return JSONResponse({"detail": "invalid or missing bearer token"}, status_code=401)
        try:
            body = await request.json()
            result = await asyncio.to_thread(fit_head, serving, body)
        except (HeadError, KeyError, TypeError, ValueError) as e:
            return JSONResponse({"detail": f"fit refused: {e}"}, status_code=422)
        return JSONResponse(result)

    @app.post("/v1/factory/heads/activate")
    async def activate(request: Request):  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return JSONResponse({"detail": "invalid or missing bearer token"}, status_code=401)
        try:
            body = await request.json()
            store.activate(str(body["question_id"]), int(body["version"]), str(body["head_sha256"]))
        except (HeadError, KeyError, TypeError, ValueError) as e:
            return JSONResponse({"detail": f"activate refused: {e}"}, status_code=422)
        return JSONResponse({"active": f"{body['question_id']}@v{int(body['version'])}"})

    @app.get("/v1/factory/heads")
    async def heads(request: Request):  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return JSONResponse({"detail": "invalid or missing bearer token"}, status_code=401)
        return JSONResponse({"heads": store.listing()})

    @app.middleware("http")
    async def factory_headers(request: Request, call_next):  # pyright: ignore[reportUnusedFunction]
        if request.method == "POST" and request.url.path in ANSWER_PATHS:
            # the plan (which head answers which question) is fixed per request and read by the
            # wrapped predict on the inference thread, so answer requests take turns here
            async with plan_lock:
                try:
                    req = json.loads(await request.body())
                    qs = req.get("questions") if isinstance(req, dict) else None
                    pins = req.get("pins") if isinstance(req, dict) else None
                    plan = (
                        store.resolve(qs, pins if isinstance(pins, dict) else None)
                        if isinstance(qs, dict)
                        else {}
                    )
                except ValueError:
                    plan = {}
                serving.plan = plan
                try:
                    return await finish(request, call_next, head_labels(plan))
                finally:
                    serving.plan = None
        return await finish(request, call_next, {})

    async def finish(request: Request, call_next: Any, versions: Mapping[str, str | None]):
        response = await call_next(request)
        response.headers[HEADER] = label
        ctype = response.headers.get("content-type", "")
        if (
            request.method == "POST"
            and request.url.path in ANSWER_PATHS
            and response.status_code == 200
            and ctype.startswith("application/json")
        ):
            body = b"".join([chunk async for chunk in response.body_iterator])  # pyright: ignore
            try:
                body = json.dumps(add_head_version(json.loads(body), versions)).encode()
            except ValueError:
                pass
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return Response(body, status_code=200, headers=headers, media_type="application/json")
        return response

    return app


def main() -> None:
    if not os.environ.get("LAYA_API_KEY"):
        print("LAYA_API_KEY is required", file=sys.stderr)
        raise SystemExit(1)
    import uvicorn

    uvicorn.run(
        build_app(),
        host=os.environ.get("LAYA_HOST", "0.0.0.0"),
        port=int(os.environ.get("LAYA_PORT", "8000")),
        log_level=os.environ.get("LAYA_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
