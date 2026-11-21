"""Public-data builder for Laya training rows: rules, hygiene, balancing, files (no network)."""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from swfactory.definition.models import AppDef
from swfactory.laya import public_data as pd
from swfactory.laya.import_rows import read_import_file
from swfactory.models.triage import TRIAGE_QUESTIONS, triage_state
from swfactory.roles.render import estimate_tokens

TRIAGE_KEYS = set(
    triage_state(
        SimpleNamespace(title="t", feature="f", triage={}),
        SimpleNamespace(repo_url="https://x/y", ui_globs=()),
    )
)


# ---------------------------------------------------------------- fixtures


def _patch(*files: tuple[str, int]) -> str:
    out = []
    for path, n in files:
        out.append(f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n")
        out.append("@@ -1,1 +1,1 @@\n" + "".join(f"+line {i}\n" for i in range(n)))
    return "".join(out)


def raibek_row(
    n: int, labels: list[str], title: str | None = None, body: str = "body"
) -> dict[str, Any]:
    return {
        "title": title or f"requests issue number {n} about topic {'x' * n}",
        "body": body,
        "repository_url": "https://api.github.com/repos/psf/requests",
        "labels": [{"name": x, "color": "fff"} for x in labels],
    }


def open_row(
    n: int, labels: list[str], pr: bool = False, title: str | None = None, body: str = "b"
) -> dict[str, Any]:
    return {
        "owner": "o",
        "repo": "widgets",
        "title": title or f"widget change {n} {'y' * n}",
        "body": body,
        "is_pull_request": pr,
        "labels": labels,
    }


def swe_row(n: int, patch: str, repo: str = "acme/shop") -> dict[str, Any]:
    return {
        "repo": repo,
        "patch": patch,
        "problem_statement": f"Problem {n} {'z' * n}\r\n\r\nSteps to reproduce {n}",
    }


def write_fixture(
    d: Path, dataset: str, rows: list[dict[str, Any]], licence: str | None = "cc-by-4.0"
) -> None:
    meta = {"licence": licence, "revision": f"rev-{dataset[:3]}"}
    (d / f"{dataset.replace('/', '__')}.json").write_text(json.dumps({"meta": meta, "rows": rows}))


@pytest.fixture
def fixtures(tmp_path: Path) -> Path:
    d = tmp_path / "fx"
    d.mkdir()
    kinds = ["Bug", "Feature Request", "Documentation", "refactor", "dependencies"]
    raibek = [raibek_row(i, [kinds[i % 5]]) for i in range(1, 11)]
    raibek += [
        raibek_row(11, []),  # no label
        raibek_row(12, ["Bug", "Feature Request"]),  # ambiguous
        raibek_row(13, ["Question/Not a bug"]),  # unknown label, not a bug
        raibek_row(14, ["Bug"], body="mail me at jane.doe@example.com"),  # email
        raibek_row(15, ["Bug"], body="token ghp_" + "a1B2c3D4" * 4),  # token
        raibek_row(1, ["Feature Request"]),  # same state as row 1 -> exact duplicate
    ]
    write_fixture(d, "Raibek/requests-github-issues", raibek, "cc-by-4.0")
    opens = [open_row(i, ["kind/bug"]) for i in range(1, 5)]
    opens += [open_row(i, ["type-feature"]) for i in range(5, 8)]
    opens += [open_row(8, ["size/XS"], pr=True), open_row(9, ["size/M"], pr=True)]
    opens += [open_row(10, ["size/XL"], pr=True), open_row(11, ["size/L"], pr=False)]
    write_fixture(d, "open-index/open-github-issues", opens, "odc-by")
    gym = [swe_row(1, _patch(("a.py", 3))), swe_row(2, _patch(("a.py", 3), ("b.py", 30)))]
    gym += [swe_row(3, _patch(*[(f"m{i}.py", 2) for i in range(7)]))]
    gym += [swe_row(4, _patch(("web/templates/x.html", 2))), swe_row(5, _patch(("a.py", 400)))]
    gym += [swe_row(6, _patch(("src/App.tsx", 4))), swe_row(7, "no diff here")]
    write_fixture(d, "SWE-Gym/SWE-Gym", gym, "mit")
    write_fixture(d, "SWE-bench/SWE-bench_Multilingual", [swe_row(8, _patch(("a.css", 1)))], "mit")
    write_fixture(d, "nebius/SWE-rebench", [swe_row(9, _patch(("a.py", 1)))], None)  # no licence
    write_fixture(d, "SWE-bench/SWE-smith", [swe_row(10, _patch(("a.py", 1)))], "mit")
    return d


# ---------------------------------------------------------------- label rules


@pytest.mark.parametrize(
    ("labels", "want"),
    [
        (["Bug"], ("bugfix", "ok")),
        (["kind/bug"], ("bugfix", "ok")),
        (["C-cleanup"], ("refactor", "ok")),
        (["type-feature"], ("feature", "ok")),
        (["pr-bugfix"], ("bugfix", "ok")),
        (["area: docs"], ("docs", "ok")),
        (["ci/cd"], ("infra", "ok")),
        (["github_actions"], ("infra", "ok")),
        (["bug", "kind/bug"], ("bugfix", "ok")),  # two labels, one kind
        (["bug", "enhancement"], (None, "ambiguous_label")),
        ([], (None, "no_label")),
        (["wontfix", "needs info"], (None, "no_label")),  # unknown labels are ignored
        (["Question/Not a bug"], (None, "no_label")),  # no fuzzy match on "bug"
        (["wontfix", "Documentation"], ("docs", "ok")),
    ],
)
def test_kind_from_labels(labels: list[str], want: tuple[str | None, str]) -> None:
    assert pd.kind_from_labels(labels) == want


def test_every_kind_label_exists_in_the_question() -> None:
    assert set(pd.KIND_PHRASES.values()) <= set(TRIAGE_QUESTIONS["kind"]["criteria"])


@pytest.mark.parametrize(
    ("labels", "want"),
    [
        (["size/XS"], "S"),
        (["size/S"], "S"),
        (["size/M"], "M"),
        (["size/L"], "L"),
        (["size/XXL"], "L"),
        (["lgtm", "size/m"], "M"),
    ],
)
def test_size_from_pr_labels(labels: list[str], want: str) -> None:
    assert pd.size_from_pr_labels(labels) == (want, "ok")


def test_size_from_pr_labels_edges() -> None:
    assert pd.size_from_pr_labels(["lgtm"]) == (None, "no_label")
    assert pd.size_from_pr_labels(["size/S", "size/L"]) == (None, "ambiguous_label")
    assert pd.size_from_pr_labels(["size/huge"]) == (None, "no_label")


def test_patch_stats_ignores_file_headers() -> None:
    st = pd.patch_stats(_patch(("a.py", 3), ("b.py", 2)))
    assert st.files == ("a.py", "b.py")
    assert st.lines == 5


@pytest.mark.parametrize(
    ("patch", "want"),
    [
        (_patch(("a.py", 1)), "S"),
        (_patch(("a.py", 20)), "S"),
        (_patch(("a.py", 21)), "M"),
        (_patch(("a.py", 1), ("b.py", 1)), "M"),
        (_patch(*[(f"{i}.py", 1) for i in range(5)]), "M"),
        (_patch(*[(f"{i}.py", 1) for i in range(6)]), "L"),
        (_patch(("a.py", 300)), "L"),
        ("not a diff", None),
        ("", None),
    ],
)
def test_size_from_patch(patch: str, want: str | None) -> None:
    assert pd.size_from_patch(patch) == want


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("web/app.css", True),
        ("src/App.tsx", True),
        ("pkg/templates/index.txt", True),
        ("pkg/static/logo.png", True),
        ("src/main.py", False),
        ("src/app.js", False),  # ambiguous (node or browser): not counted
        ("docs/_static/theme.css", False),
        ("tests/test_view.html", False),
    ],
)
def test_is_ui_path(path: str, want: bool) -> None:
    assert pd.is_ui_path(path) is want


def test_touches_ui_from_patch() -> None:
    assert pd.touches_ui_from_patch(_patch(("a.py", 1), ("b.vue", 1))) == "A"
    assert pd.touches_ui_from_patch(_patch(("a.py", 1))) == "B"
    assert pd.touches_ui_from_patch("nothing") is None


# ---------------------------------------------------------------- hygiene and state shape


@pytest.mark.parametrize(
    "text",
    [
        "write to bob@example.org please",
        "export GITHUB_TOKEN=ghp_" + "abcd1234" * 3,
        "Authorization: Bearer abcdef123456",
        "key sk-" + "a1b2c3d4" * 3,
        "AKIAABCDEFGHIJKLMNOP",
        'password = "hunter2hunter2hunter2"',
        "postgres://user:s3cretpass@db/x",
    ],
)
def test_contains_secret_true(text: str) -> None:
    assert pd.contains_secret(text)


def test_contains_secret_false_for_ordinary_text() -> None:
    assert not pd.contains_secret("Fix the parser: @decorator on x@1 breaks; see commit 1a2b3c4")


def test_clean_body_strips_comments_and_blank_runs() -> None:
    assert pd.clean_body("a\r\n<!-- template -->\n\n\n\nb") == "a\n\nb"


def test_state_has_the_keys_of_a_real_job_state() -> None:
    state = json.loads(pd.build_state("Title", "Body", "widgets"))
    assert set(state) == TRIAGE_KEYS
    assert state["app"] == "widgets"
    assert state["stack"] == "unknown"
    assert state["labels"] == []


def test_state_is_trimmed_to_the_token_budget_like_triage_state() -> None:
    text = pd.build_state("Title", "word " * 5000, "widgets")
    assert estimate_tokens(text) < pd.STATE_MAX_TOKENS
    assert json.loads(text)["feature"].endswith("[omitted]")


def test_default_ui_globs_match_the_app_definition() -> None:
    assert pd.DEFAULT_UI_GLOBS == AppDef.model_fields["ui_globs"].default


# ---------------------------------------------------------------- balancing


def _rows(counts: dict[str, int]) -> list[pd.Row]:
    return [pd.Row(f'{{"n": "{lb}{i}"}}', lb, "s") for lb, n in counts.items() for i in range(n)]


def test_balance_caps_majority_at_three_times_minority() -> None:
    out = pd.balance(_rows({"a": 100, "b": 20, "c": 50}), 3, random.Random(1))
    assert Counter(r.label for r in out) == {"a": 60, "b": 20, "c": 50}


def test_balance_ignores_classes_below_the_fit_minimum_when_sizing() -> None:
    out = pd.balance(_rows({"a": 100, "b": 20, "tiny": 3}), 3, random.Random(1))
    assert Counter(r.label for r in out) == {"a": 60, "b": 20, "tiny": 3}


def test_balance_off_and_deterministic() -> None:
    rows = _rows({"a": 100, "b": 5})
    assert len(pd.balance(rows, 0, random.Random(1))) == 105
    first = pd.balance(rows, 3, random.Random(1))
    assert first == pd.balance(list(reversed(rows)), 3, random.Random(1))


# ---------------------------------------------------------------- pipeline and files


def test_kind_pipeline_drops_and_counts(fixtures: Path) -> None:
    res = pd.build_question("kind", pd.FixtureSource(fixtures), seed=1, max_ratio=0)
    by = {r.dataset: r for r in res.reports}
    rb = by["Raibek/requests-github-issues"]
    assert rb.rows_read == 16
    assert rb.dropped == {
        "no_label": 2,  # no label, and "Question/Not a bug"
        "ambiguous_label": 1,
        "secret_or_email": 2,
        "duplicate": 1,
    }
    assert sum(rb.used.values()) == 10
    assert {r.dataset for r in res.reports} == {
        "Raibek/requests-github-issues",
        "open-index/open-github-issues",
    }
    assert rb.licence == "CC-BY-4.0" and rb.revision
    assert by["open-index/open-github-issues"].licence == "ODC-By-1.0"
    assert Counter(r.label for r in res.rows)["bugfix"] >= 4
    assert any("SWE-bench_Verified" in s["dataset"] for s in res.skipped)


def test_size_pipeline_skips_synthetic_unless_asked_and_missing_licence(fixtures: Path) -> None:
    src = pd.FixtureSource(fixtures)
    res = pd.build_question("size", src, seed=1, max_ratio=0)
    names = {r.dataset for r in res.reports}
    assert names == {
        "open-index/open-github-issues",
        "SWE-Gym/SWE-Gym",
        "SWE-bench/SWE-bench_Multilingual",
    }
    reasons = {s["dataset"]: s["reason"] for s in res.skipped}
    assert "no licence" in reasons["nebius/SWE-rebench"]
    assert "synthetic" in reasons["SWE-bench/SWE-smith"]
    open_rep = next(r for r in res.reports if r.dataset.startswith("open-index"))
    assert open_rep.dropped == {"not_a_pull_request": 8}  # 3 PRs kept, 8 issues refused
    assert dict(open_rep.used) == {"S": 1, "M": 1, "L": 1}
    gym = next(r for r in res.reports if r.dataset == "SWE-Gym/SWE-Gym")
    assert gym.dropped == {"no_patch_files": 1}
    assert dict(gym.used) == {"S": 3, "M": 1, "L": 2}
    with_syn = pd.build_question("size", src, seed=1, include_synthetic=True, max_ratio=0)
    assert "SWE-bench/SWE-smith" in {r.dataset for r in with_syn.reports}


def test_licence_outside_allow_list_is_skipped(tmp_path: Path, fixtures: Path) -> None:
    write_fixture(fixtures, "SWE-Gym/SWE-Gym", [swe_row(1, _patch(("a.py", 1)))], "gpl-3.0")
    res = pd.build_question("touches_ui", pd.FixtureSource(fixtures), max_ratio=0)
    reasons = {s["dataset"]: s["reason"] for s in res.skipped}
    assert "not in the allow-list" in reasons["SWE-Gym/SWE-Gym"]


def test_touches_ui_rows_come_from_patch_paths(fixtures: Path) -> None:
    res = pd.build_question("touches_ui", pd.FixtureSource(fixtures), max_ratio=0)
    counts = Counter(r.label for r in res.rows)
    assert counts == {"A": 3, "B": 4}  # templates html, tsx, css vs the python-only patches


def test_state_of_every_row_matches_triage_state_keys(fixtures: Path) -> None:
    res = pd.build_question("kind", pd.FixtureSource(fixtures), max_ratio=0)
    assert res.rows
    for r in res.rows:
        assert set(json.loads(r.state)) == TRIAGE_KEYS


def test_conflicting_labels_for_one_state_are_dropped(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir()
    same = {"title": "same title", "body": "same", "repository_url": "r/x"}
    rows = [
        {**same, "labels": [{"name": "Bug"}]},
        {**same, "labels": [{"name": "Documentation"}]},
        {**same, "labels": [{"name": "Documentation"}]},
        raibek_row(2, ["Bug"]),
    ]
    write_fixture(d, "Raibek/requests-github-issues", rows)
    res = pd.build_question("kind", pd.FixtureSource(d), max_ratio=0)
    assert len(res.rows) == 1
    assert "same title" not in res.rows[0].state


def test_near_duplicate_titles_are_capped(tmp_path: Path) -> None:
    d = tmp_path / "n"
    d.mkdir()
    rows = [
        raibek_row(i, ["dependencies"], title=f"Bump lib from 1.{i} to 2.{i}") for i in range(10)
    ]
    write_fixture(d, "Raibek/requests-github-issues", rows)
    res = pd.build_question("kind", pd.FixtureSource(d), max_ratio=0)
    assert len(res.rows) == pd.NEAR_DUPLICATE_CAP


@pytest.mark.parametrize("question", ["kind", "size", "touches_ui"])
def test_outputs_pass_the_importer_and_are_deterministic(
    fixtures: Path, tmp_path: Path, question: str
) -> None:
    def build(out: Path, seed: int) -> Path:
        res = pd.build_question(question, pd.FixtureSource(fixtures), seed=seed, max_ratio=1)
        return pd.write_outputs(res, out, "2026-11-20", {"seed": seed})

    a, b = tmp_path / "a", tmp_path / "b"
    prov_a, prov_b = build(a, 7), build(b, 7)
    names = sorted(p.name for p in a.iterdir())
    assert names == sorted(p.name for p in b.iterdir())
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes()
    labels = pd.question_labels(question)
    prov = json.loads(prov_a.read_text())
    assert prov["built_on"] == "2026-11-20"
    assert prov_b.exists()
    for f in prov["files"]:
        rows = read_import_file(a / f["file"], question, labels)
        assert len(rows) == f["rows"] == sum(f["rows_per_class"].values())
        assert Counter(lb for _, lb in rows) == f["rows_per_class"]
    combined = next(f for f in prov["files"] if f["file"] == f"{question}.csv")
    assert combined["rows"] == prov["rows"]
    per_source = sum(f["rows"] for f in prov["files"] if f["file"] != f"{question}.csv")
    assert per_source == prov["rows"]
    for ds in prov["datasets"]:
        assert ds["licence"] and ds["url"].startswith("https://huggingface.co/datasets/")
        assert ds["derivation_rule"]


def test_balanced_output_has_at_most_ratio_times_minority(fixtures: Path, tmp_path: Path) -> None:
    res = pd.build_question("touches_ui", pd.FixtureSource(fixtures), max_ratio=1)
    counts = Counter(r.label for r in res.rows)
    assert counts == {"A": 3, "B": 3}
    pd.write_outputs(res, tmp_path / "o", "2026-11-20", {})


def test_a_different_seed_can_pick_different_rows(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    rows = [raibek_row(i, ["Bug"]) for i in range(1, 41)]
    rows += [raibek_row(100 + i, ["Documentation"]) for i in range(1, 11)]
    write_fixture(d, "Raibek/requests-github-issues", rows)
    one = pd.build_question("kind", pd.FixtureSource(d), seed=1, max_ratio=1)
    two = pd.build_question("kind", pd.FixtureSource(d), seed=2, max_ratio=1)
    assert Counter(r.label for r in one.rows) == {"bugfix": 10, "docs": 10}
    assert [r.state for r in one.rows] != [r.state for r in two.rows]


# ---------------------------------------------------------------- command line


def test_cli_builds_from_fixtures(
    fixtures: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    argv = ["--offline-fixture", str(fixtures), "--out-dir", str(out), "--built-on", "2026-11-20"]
    assert pd.main([*argv, "--question", "kind", "--max-ratio", "0"]) == 0
    assert (out / "kind.csv").is_file() and (out / "kind.provenance.json").is_file()
    assert "kind:" in capsys.readouterr().out


def test_cli_refuses_questions_without_a_public_source(capsys: pytest.CaptureFixture[str]) -> None:
    for q in ("risk", "tier.builder"):
        assert pd.main(["--question", q, "--built-on", "2026-11-20"]) == 2
        assert "no honest public source" in capsys.readouterr().err


def test_cli_refuses_a_bad_date_and_unknown_question() -> None:
    assert pd.main(["--built-on", "yesterday"]) == 2
    assert pd.main(["--question", "nope", "--built-on", "2026-11-20"]) == 2


# ---------------------------------------------------------------- the hub client (mock transport)


def _hub(handler: Any) -> pd.HubSource:
    return pd.HubSource(httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0)


def test_hub_meta_reads_licence_and_revision() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"sha": "abc", "cardData": {"license": "odc-by"}})

    meta = _hub(handler).meta(pd.DATASETS[1])
    assert (meta.licence, meta.revision) == ("ODC-By-1.0", "abc")


def test_hub_meta_without_licence_gives_none() -> None:
    src = _hub(lambda req: httpx.Response(200, json={"sha": "abc", "cardData": {}, "tags": []}))
    assert src.meta(pd.DATASETS[2]).licence is None
    tagged = _hub(lambda req: httpx.Response(200, json={"sha": "a", "tags": ["license:mit"]}))
    assert tagged.meta(pd.DATASETS[2]).licence == "MIT"


def test_hub_rows_whole_split_and_seeded_windows() -> None:
    total = 95
    seen: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        off, n = int(q["offset"]), int(q["length"])
        seen.append(off)
        rows = [
            {"row_idx": i, "row": {"i": i}, "truncated_cells": ["patch"] if i == 3 else []}
            for i in range(off, min(off + n, total))
        ]
        return httpx.Response(200, json={"num_rows_total": total, "rows": rows})

    spec = pd.DATASETS[2]  # window 50
    src = _hub(handler)
    everything = list(src.rows(spec, 1000, 1))
    assert [r.row["i"] for r in everything] == list(range(total))
    assert everything[3].truncated == {"patch"}
    seen.clear()
    part = list(src.rows(spec, 50, 1))
    assert len(part) == 50
    assert [r.row["i"] for r in part] == [r.row["i"] for r in src.rows(spec, 50, 1)]
    assert seen[0] == 0 and len(seen) >= 2  # the size probe, then one window


def test_hub_retries_rate_limits_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"})

    with pytest.raises(RuntimeError, match="HTTP 429"):
        _hub(handler).meta(pd.DATASETS[0])
    assert len(calls) == 7


def test_rows_hide_the_source_repo_unless_asked(fixtures: Path) -> None:
    src = pd.FixtureSource(fixtures)
    neutral = pd.build_question("kind", src, max_ratio=0)
    assert {json.loads(r.state)["app"] for r in neutral.rows} == {"unknown"}
    kept = pd.build_question("kind", src, max_ratio=0, keep_repo_name=True)
    assert {json.loads(r.state)["app"] for r in kept.rows} == {"requests", "widgets"}
