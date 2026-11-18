from typing import Any, cast

from sqlalchemy import BigInteger

from swfactory.db.tables import Base

M1_TABLES = {
    "definitions",
    "apps",
    "jobs",
    "job_events",
    "agent_runs",
    "artifacts",
    "models",
    "model_decisions",
    "scores",
    "bench_runs",
    "bench_results",
    "improvements",
    "commands",
    "users",
    "api_tokens",
    "backlogs",
}


def test_m1_section_15_tables():  # "<=": later milestones add tables
    ts = Base.metadata.sorted_tables
    assert M1_TABLES <= {t.name for t in ts} and {t.schema for t in ts} == {"app"}
    assert "lease_epoch" in Base.metadata.tables["app.jobs"].c  # added by migration 0002


def test_ids_default_uuidv7():
    ts = Base.metadata.sorted_tables
    for t in ts:
        if "id" in t.c and t.name != "job_events":
            assert str(cast(Any, t.c.id.server_default).arg) == "uuidv7()"
        for c in t.c:
            if c.name in {"cost_usd", "budget_usd"}:
                assert (cast(Any, c.type).precision, cast(Any, c.type).scale) == (12, 6)
    assert isinstance(Base.metadata.tables["app.job_events"].c.id.type, BigInteger)
    xid = Base.metadata.tables["app.job_events"].c.xid
    assert str(cast(Any, xid.server_default).arg) == "pg_current_xact_id()"
    assert {
        t.name: [c.name for c in t.primary_key]
        for t in ts
        if t.name in {"definitions", "models", "users"}
    } == {"definitions": ["sha"], "models": ["alias"], "users": ["email"]}
