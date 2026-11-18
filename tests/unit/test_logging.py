import asyncio
import json
import logging

import structlog

from swfactory.logging import configure_logging, log_context, redact, register_secret

CANARIES = [
    "canary-7f3a9c2e-secret",
    "sk-canaryAAAAAAAAAAAAAAAA",
    "ghp_" + "C" * 40,
    "github_pat_" + "D" * 30,
    "AIza" + "E" * 35,
    "Bearer canarytoken123",
    "postgresql://u:canarypw@h/db",
    "ya29." + "F" * 30,
    "gsk_" + "G" * 40,
    "eyJ" + "h" * 12 + "." + "i" * 12 + "." + "j" * 12,
    "Authorization: Basic Y2FuYXJ5OnB3",
    "authorization: token canarytok456",
]


def emit_canaries(canaries):
    log = structlog.get_logger()
    std = logging.getLogger("x")
    for c in canaries:
        log.info(f"event {c}")
        log.info("nested", data={"k": [c, {"z": c}]})
        try:
            raise RuntimeError(f"boom {c}")
        except RuntimeError:
            log.exception("failed")
        std.warning("stdlib %s", c)


def test_redaction_canaries(capsys):
    configure_logging()
    register_secret("canary-7f3a9c2e-secret")
    emit_canaries(CANARIES)
    lines = capsys.readouterr().err.splitlines()
    for line in lines:
        json.loads(line)
    assert [c for c in CANARIES if any(c in line for line in lines)] == []
    assert sum(line.count("[REDACTED]") for line in lines) >= 12
    assert "postgresql://u:[REDACTED]@h/db" in "".join(lines)
    assert redact("Authorization: Basic Y2FuYXJ5OnB3") == "Authorization: [REDACTED]"


def test_redact_pem():
    pem = "-----BEGIN PRIVATE KEY-----\nabc\ndef\n-----END PRIVATE KEY-----"
    assert redact(f"k {pem} z") == "k [REDACTED] z"


def test_log_context_fields(capsys):
    configure_logging()
    with log_context(job_id="j1", run_id="r1", role="builder"):
        structlog.get_logger().info("step")
        logging.getLogger("lib").warning("lib line")
    structlog.get_logger().info("after")
    a, b, c = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    want = {"job_id": "j1", "run_id": "r1", "role": "builder"}
    assert {k: a[k] for k in want} == want == {k: b[k] for k in want}
    assert "job_id" not in c


async def test_log_context_is_per_task(capsys):
    configure_logging()

    async def one(j):
        with log_context(job_id=j):
            await asyncio.sleep(0)
            structlog.get_logger().info(f"from-{j}")

    await asyncio.gather(one("a"), one("b"))
    recs = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert sorted(r["job_id"] for r in recs) == ["a", "b"]
    assert all(r["event"] == f"from-{r['job_id']}" for r in recs)
