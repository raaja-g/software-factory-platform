"""Acceptance: replay v1's four features with team-gemini and claude-vertex (opt-in).

The live test needs `make up`.

Set FACTORY_E2E_APP_TEAM_GEMINI and FACTORY_E2E_APP_CLAUDE_VERTEX to fresh local apps (one per
profile, merge_policy local_merge). The report helpers at the top are pure and run by default.
"""

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swfactory.cli.api_client import ApiClient
from swfactory.cli.backlog import parse_backlog
from swfactory.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/v1-backlog.md"
ACCEPT = ROOT / "ref/acceptance"
WAIVERS = ROOT / "ref/acceptance/waivers.md"
PARITY_PROFILES = ("team-gemini", "claude-vertex")
FORBIDDEN_ENGINES: dict[str, set[str]] = {"team-gemini": {"claude_code"}, "claude-vertex": set()}
PARITY_MIN_UNATTENDED = 3
V1_ROUNDS = {"001": 1, "002": 1, "003": 2, "004": 1}  # v1's rounds 1/1/2/1
REVIEWER_ORDER = ("security", "ux", "ui", "code")
SETTLED = {"done", "needs_human"}


# -- pure report helpers -------------------------------------------------------------------------


def _short(verdict: str) -> str:
    v = (verdict or "").upper()
    return "CHANGES" if v.startswith("CHANGE") else v or "-"


def _round_cell(label: str, reviews: Mapping[str, str]) -> str:
    keys = [k for k in REVIEWER_ORDER if k in reviews] + sorted(
        k for k in reviews if k not in REVIEWER_ORDER
    )
    vs = [_short(reviews[k]) for k in keys]
    if vs and len(set(vs)) == 1:
        return f"{label} {vs[0]}×{len(vs)}"
    return f"{label} {'/'.join(vs) if vs else '-'}"


def _seq(job: Mapping[str, Any]) -> str:
    m = re.match(r"(\d+)", str(job.get("id") or job.get("ref", "").rsplit("-", 1)[-1]))
    return m.group(1) if m else str(job.get("id", "?"))[:3]


def _rounds(job: Mapping[str, Any]) -> list[tuple[int, Mapping[str, str]]]:
    out = [(int(h["round"]), h.get("reviews", {})) for h in job.get("history", [])]
    if job.get("reviews"):
        out.append((int(job.get("round", len(out) + 1)), job["reviews"]))
    return out


def _cost(job: Mapping[str, Any]) -> Decimal:
    return Decimal(str(job.get("cost_usd", job.get("cost", "0")) or "0"))


def _money(d: Decimal) -> str:
    return f"${d.quantize(Decimal('0.01'))}"


def _minutes(job: Mapping[str, Any]) -> str:
    m = job.get("minutes")
    return "-" if m is None else f"{float(m):.0f}"


def parity_report(
    jobs: Sequence[Mapping[str, Any]], runs: Mapping[str, Sequence[Mapping[str, Any]]]
) -> str:
    head = (
        "| job | rounds | verdicts (sec/ux/ui/code) | decision | final stage | cost | minutes "
        "| fallbacks | v1 rounds |"
    )
    lines = [head, "|" + "---|" * 9]
    for j in jobs:
        rs = _rounds(j)
        verdicts = " · ".join(_round_cell(f"R{r}", rv) for r, rv in rs) or "-"
        nrounds = int(j.get("round") or len(rs))
        jruns = runs.get(str(j.get("job_id")), [])
        fallbacks = sum(
            1 for r in jruns if r.get("fallback") or r.get("selection_mode") == "fallback"
        )
        fallbacks += 1 if j.get("fallback") and not fallbacks else 0
        gate = f" ({j['gate_reason']})" if j.get("gate_reason") else ""
        seq = _seq(j)
        lines.append(
            f"| {seq} | {nrounds} | {verdicts} | {j.get('decision') or '-'} | "
            f"{j.get('stage', '-')}{gate} | {_money(_cost(j))} | {_minutes(j)} | {fallbacks} | "
            f"{V1_ROUNDS.get(seq, '-')} |"
        )
    note = (
        "\nNote: 004's feature text assumes v1's 003 code (`_check_origin`), so its outcome is "
        "reported, not asserted.\n"
    )
    return "\n".join(lines) + "\n" + note


def comparison_report(by_profile: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    profiles = list(by_profile)
    cols = ["job"] + [f"{p} rounds" for p in profiles] + [f"{p} final verdicts" for p in profiles]
    cols += [f"{p} decision" for p in profiles] + [f"{p} cost" for p in profiles]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    by_seq = {p: {_seq(j): j for j in js} for p, js in by_profile.items()}
    seqs = sorted({s for m in by_seq.values() for s in m})
    for s in seqs:
        row: list[str] = [s]
        jobs = [by_seq[p].get(s) for p in profiles]
        row += [str(int(j.get("round") or len(_rounds(j)))) if j else "-" for j in jobs]
        row += [
            (_round_cell("", (_rounds(j) or [(0, {})])[-1][1]).strip() if j else "-") for j in jobs
        ]
        row += [str(j.get("decision") or "-") if j else "-" for j in jobs]
        row += [_money(_cost(j)) if j else "-" for j in jobs]
        lines.append("| " + " | ".join(row) + " |")
    total = ["total"]
    total += [str(sum(int(j.get("round") or 0) for j in by_profile[p])) for p in profiles]
    total += ["" for _ in profiles] + ["" for _ in profiles]
    total += [_money(sum((_cost(j) for j in by_profile[p]), Decimal(0))) for p in profiles]
    lines.append("| " + " | ".join(total) + " |")
    return "\n".join(lines) + "\n"


def unattended_count(finals: Sequence[Mapping[str, Any]], human_actions: Mapping[str, int]) -> int:
    return sum(
        1 for f in finals if f.get("stage") == "merged" and human_actions.get(f["job_id"], 0) == 0
    )


def waiver_for(profile: str, text: str) -> str | None:
    for line in text.splitlines():
        m = re.match(rf"^\s*-\s*{re.escape(profile)}\s*:\s*(\S.*?)\s*$", line)
        if m:
            return m.group(1)
    return None


# -- live helpers --------------------------------------------------------------------------------


def wait_until_settled(
    api: ApiClient, job: Mapping[str, Any], timeout_s: float, poll_s: float = 30
) -> dict[str, Any]:
    """Poll the board row until its column is done or needs_human; return that row."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = board_row(api, job)
        if row["column"] in SETTLED:
            return row
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{job['ref']} did not settle in {timeout_s}s (stage {row['stage']})"
            )
        time.sleep(poll_s)


def board_row(api: ApiClient, job: Mapping[str, Any]) -> dict[str, Any]:
    shown = api.show_job(job["ref"])
    board = api._call("GET", "/api/board", params={"app": shown["app"]})  # noqa: SLF001
    for b in board["jobs"]:
        if b["job_id"] == shown["job_id"]:
            row = dict(b)
            row["minutes"] = _elapsed_minutes(shown)
            return row
    raise LookupError(f"{job['ref']} is not on the board")


def _elapsed_minutes(shown: Mapping[str, Any]) -> float | None:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(shown["created_at"])
        b = datetime.fromisoformat(shown["updated_at"])
    except (KeyError, ValueError):
        return None
    return (b - a).total_seconds() / 60


def human_actions(api: ApiClient, job_id_or_ref: str) -> int:
    events = api._call("GET", f"/api/jobs/{job_id_or_ref}/events", params={"limit": 1000})  # noqa: SLF001
    return sum(1 for e in events if e["kind"] == "human.action")


def total_cost(api: ApiClient, app: str) -> Decimal:
    return sum((Decimal(j["cost_usd"]) for j in api.list_jobs(app=app)), Decimal(0))


def missing_aliases(api: ApiClient, profile: str) -> bool:
    """True when the profile needs Claude aliases and none is available on the gateway."""
    if profile != "claude-vertex":
        return False
    claude = [m for m in api.list_models() if str(m["alias"]).startswith("claude")]
    return not any(m.get("available") for m in claude)


def e2e_app(profile: str) -> str:
    var = "FACTORY_E2E_APP_" + profile.upper().replace("-", "_")
    app = os.environ.get(var)
    if not app:
        pytest.skip(f"opt-in: set {var} to a fresh local app that uses profile {profile}")
    return app


def load_reports(profiles: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for p in profiles:
        f = ACCEPT / f"m7-{p}.json"
        if not f.exists():
            pytest.skip(f"opt-in: {f.name} is missing; run test_v1_parity for {p} first")
        out[p] = json.loads(f.read_text())
    return out


@pytest.fixture
def api() -> ApiClient:
    if not os.environ.get("FACTORY_E2E_APP_TEAM_GEMINI") and not os.environ.get(
        "FACTORY_E2E_APP_CLAUDE_VERTEX"
    ):
        pytest.skip("opt-in: no FACTORY_E2E_APP_* set; needs `make up` and fresh local apps")
    return ApiClient.from_settings(get_settings())


# -- tests ---------------------------------------------------------------------------------------

FAKE_JOBS = [
    {"id": "001-bootstrap", "job_id": "j1", "round": 1, "history": [],
     "reviews": {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"},
     "decision": "APPROVE", "stage": "merged", "cost_usd": "1.00", "minutes": 12},
    {"id": "003-todo-web", "job_id": "j3", "round": 2,
     "history": [{"round": 1, "reviews": {"security": "CHANGES", "ux": "PASS", "ui": "PASS", "code": "PASS"}}],  # noqa: E501
     "reviews": {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"},
     "decision": "APPROVE", "stage": "merged", "cost_usd": "2.10", "minutes": 30},
]  # fmt: skip
FAKE_RUNS = {"j1": [], "j3": [{"selection_mode": "fallback"}]}
FAKE_JOBS_CLAUDE = [
    {**FAKE_JOBS[1], "round": 1, "history": [], "cost_usd": "3.00"},
]


def test_parity_report_table():
    md = parity_report(FAKE_JOBS, FAKE_RUNS)
    assert "| 003 | 2 | R1 CHANGES/PASS/PASS/PASS · R2 PASS×4 | APPROVE | merged | $2.10 |" in md
    assert md.splitlines()[0].startswith("| job | rounds |")
    assert "| 001 | 1 | R1 PASS×4 |" in md
    assert "| 1 | 2 |" in md  # fallbacks 1, v1 rounds 2 for job 003


def test_comparison_report_side_by_side():
    md = comparison_report({"team-gemini": FAKE_JOBS, "claude-vertex": FAKE_JOBS_CLAUDE})
    assert md.splitlines()[0].startswith("| job | team-gemini rounds | claude-vertex rounds |")
    assert "| 003 | 2 | 1 |" in md and "| total |" in md
    assert "$5.10" not in md and "$3.00" in md


def test_report_unattended_count():
    finals = [
        {"job_id": j, "stage": s}
        for j, s in (("a", "merged"), ("b", "merged"), ("c", "needs_human"), ("d", "merged"))
    ]
    assert unattended_count(finals, {"a": 0, "b": 1, "c": 0, "d": 0}) == 2


def test_report_waiver_for():
    reason = "2 of 4 accepted for the local alpha, owner, 2026-12-01"
    text = f"# Acceptance waivers\n- team-gemini: {reason}\n"
    assert waiver_for("team-gemini", text) == reason
    assert waiver_for("claude-vertex", text) is None


@pytest.mark.e2e
@pytest.mark.parametrize("profile", PARITY_PROFILES)
def test_v1_parity(api, profile):
    """Needs `make up` and FACTORY_E2E_APP_<PROFILE>: a fresh local app, local_merge."""
    if profile == "claude-vertex" and missing_aliases(api, profile):
        pytest.skip("Claude aliases unavailable on Vertex")
    app = e2e_app(profile)
    assert api.list_jobs(app=app) == []  # fresh repo
    finals: list[dict[str, Any]] = []
    runs_by_job: dict[str, list[dict[str, Any]]] = {}
    bl = api.import_backlog(
        app=app,
        title="v1 parity",
        items=[
            {"id": str(i.line), "title": i.text[:80], "feature": i.text}
            for i in parse_backlog(FIXTURE.read_text())
        ],
    )
    for job in bl["jobs"]:  # strictly one at a time: 004 builds on 003's merge
        final = wait_until_settled(api, job, timeout_s=3 * 3600, poll_s=30)
        assert final["stage"] != "failed" and Decimal(final["cost_usd"]) <= Decimal("10")
        runs = api.job_runs(job["ref"])
        assert {r["engine"] for r in runs} & FORBIDDEN_ENGINES[profile] == set()  # G3
        assert {len(h["reviews"]) for h in [*final["history"], {"reviews": final["reviews"]}]} == {
            4
        }
        finals.append(final)
        runs_by_job[final["job_id"]] = runs
    assert len(finals) == 4 and total_cost(api, app) <= Decimal("50")
    n = unattended_count(finals, {f["job_id"]: human_actions(api, f["ref"]) for f in finals})
    waiver = waiver_for(profile, WAIVERS.read_text() if WAIVERS.exists() else "")
    ACCEPT.mkdir(parents=True, exist_ok=True)
    (ACCEPT / f"m7-{profile}.json").write_text(json.dumps(finals, indent=2, default=str))
    (ACCEPT / f"m7-{profile}.md").write_text(
        parity_report(finals, runs_by_job)
        + f"\nunattended {n} of 4"
        + (f" (waived: {waiver})" if waiver else "")
        + f"\ntotal cost ${total_cost(api, app)}\n"
    )
    assert n >= PARITY_MIN_UNATTENDED or waiver, (
        f"{profile}: {n} of 4 merged with no human action; need {PARITY_MIN_UNATTENDED} "
        f"or a waiver in {WAIVERS}"
    )


@pytest.mark.e2e
def test_write_comparison():
    reports = load_reports(PARITY_PROFILES)
    ACCEPT.mkdir(parents=True, exist_ok=True)
    (ACCEPT / "m7-comparison.md").write_text(comparison_report(reports))
